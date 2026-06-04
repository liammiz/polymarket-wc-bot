"""
Main polling service.

Runs three recurring jobs via APScheduler:
  - poll_trades      every POLL_INTERVAL_SECONDS (default 30 s)
  - refresh_markets  every 10 minutes
  - check_resolutions every 5 minutes
  - daily_summary    at 00:00 UTC

Phase 1 (games_completed < PHASE_THRESHOLD): data collection only.
Phase 2 (games_completed >= PHASE_THRESHOLD): live/dry-run copy-trading.
"""

import logging
import logging.handlers
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import requests
from apscheduler.schedulers.background import BackgroundScheduler

from config import (
    CLOB_BASE_URL, POLL_INTERVAL_SECONDS, MIN_TRADE_SIZE_USD,
    WIN_RATE_THRESHOLD, WC_KEYWORDS, PHASE_THRESHOLD, LOG_PATH,
    DRY_RUN, STARTING_CAPITAL, MIN_TRADES_ELIGIBLE,
)
import db
import scorer
import alerter
import executor


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
    handlers: list = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            str(LOG_PATH), maxBytes=10_000_000, backupCount=3, encoding="utf-8"
        ),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)
    # Quiet noisy libraries
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_wc_market(question: str) -> bool:
    ql = question.lower()
    return any(kw.lower() in ql for kw in WC_KEYWORDS)


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _parse_trade(raw: dict) -> Optional[Dict]:
    """Normalise a raw CLOB trade dict into a canonical shape."""
    try:
        trade_id = raw.get("id") or raw.get("trade_id", "")
        market_id = raw.get("market") or raw.get("condition_id", "")
        if not trade_id or not market_id:
            return None

        # Prefer taker_address; fall back to owner (taker in REST responses) or maker
        wallet = (
            raw.get("taker_address")
            or raw.get("owner")
            or raw.get("maker_address", "")
        )

        price = float(raw.get("price", 0) or 0)
        size_tokens = float(raw.get("size", 0) or 0)
        side = (raw.get("side") or "BUY").upper()
        outcome = raw.get("outcome", "")

        # USD spent = price × tokens (for BUY; price is USDC/share)
        size_usd = price * size_tokens

        # Normalise timestamp
        ts_raw = raw.get("match_time") or raw.get("timestamp") or raw.get("transact_time", 0)
        try:
            ts_int = int(float(ts_raw))
            ts_iso = datetime.fromtimestamp(ts_int, tz=timezone.utc).isoformat()
        except Exception:
            ts_int = _now_ts()
            ts_iso = datetime.now(timezone.utc).isoformat()

        return {
            "trade_id": trade_id,
            "market_id": market_id,
            "wallet": wallet,
            "outcome": outcome,
            "price": price,
            "size_tokens": size_tokens,
            "size_usd": size_usd,
            "side": side,
            "timestamp": ts_iso,
            "timestamp_int": ts_int,
        }
    except Exception as exc:
        logger.debug("Trade parse error: %s | raw=%s", exc, raw)
        return None


# ── Market refresh ────────────────────────────────────────────────────────────

def refresh_wc_markets() -> None:
    """Pull all Polymarket markets; persist WC 2026 ones to DB."""
    logger.info("Refreshing WC market list…")
    next_cursor = ""
    found = 0

    for _ in range(200):  # safety page cap
        params: dict = {"limit": 500}
        if next_cursor:
            params["next_cursor"] = next_cursor

        try:
            resp = requests.get(f"{CLOB_BASE_URL}/markets", params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Market fetch failed: %s", exc)
            break

        for m in data.get("data", []):
            question = (
                m.get("question") or m.get("title") or m.get("description") or ""
            )
            if not _is_wc_market(question):
                continue

            market_id = m.get("condition_id") or m.get("id") or m.get("market_id", "")
            if not market_id:
                continue

            active = not (m.get("closed") or m.get("resolved"))
            resolved = bool(m.get("resolved"))
            winning_outcome = m.get("outcome") or m.get("winning_outcome")

            db.upsert_market(
                market_id=market_id,
                name=question[:200],
                question=question,
                active=active,
                resolved=resolved,
                winning_outcome=winning_outcome,
            )
            found += 1

        next_cursor = data.get("next_cursor", "")
        if not next_cursor or next_cursor in ("LTE=", ""):
            break

    active_count = len(db.get_active_wc_markets())
    logger.info("Market refresh done. Total WC found=%d  active=%d", found, active_count)


# ── Resolution checker ────────────────────────────────────────────────────────

def check_market_resolutions() -> None:
    """
    For each unresolved market, query the API for resolution status.
    When resolved: update trade outcomes, wallet scores, positions, game counter.
    """
    markets = db.get_all_markets()
    for m in markets:
        if m["resolved"]:
            continue

        try:
            resp = requests.get(
                f"{CLOB_BASE_URL}/markets/{m['market_id']}", timeout=15
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue

        is_resolved = bool(data.get("resolved"))
        winning_outcome = data.get("outcome") or data.get("winning_outcome")

        if not (is_resolved and winning_outcome):
            continue

        logger.info(
            "Market resolved: '%s'  winner=%s", m["market_name"], winning_outcome
        )

        db.mark_market_resolved(m["market_id"], winning_outcome)
        db.resolve_wallet_trades(m["market_id"], winning_outcome)

        # Re-score wallets and fire promotion/demotion alerts
        changes = scorer.recalculate_all_wallet_scores()
        for ch in changes:
            addr = ch["address"]
            if ch["old_status"] != "followed" and ch["new_status"] == "followed":
                alerter.alert_whale_promoted(addr, ch["win_rate"], ch["trade_count"])
            elif ch["old_status"] == "followed" and ch["new_status"] != "followed":
                alerter.alert_whale_demoted(addr, ch["win_rate"])

        # Close our copied positions
        for pos, result, pnl in executor.close_resolved_positions(
            m["market_id"], winning_outcome
        ):
            alerter.alert_position_closed(
                pos["market_name"], pos["outcome"], pos["size_usd"], result, pnl
            )

        games = db.increment_games_completed()
        logger.info("Games completed: %d", games)


# ── Main trade poll ───────────────────────────────────────────────────────────

def poll_trades() -> None:
    """
    Fetch new trades from the CLOB API.

    Uses the `after` query parameter (Unix timestamp) to avoid re-fetching
    known trades. Falls back to the seen_trades table for deduplication.
    """
    games_completed = db.get_games_completed()
    phase = 1 if games_completed < PHASE_THRESHOLD else 2
    logger.info(
        "PHASE %d — COLLECTION ONLY" if phase == 1 else "PHASE %d — LIVE TRADING",
        phase,
    )

    # Active WC markets for filtering
    active_markets = db.get_active_wc_markets()
    wc_market_ids: Set[str] = {m["market_id"] for m in active_markets}
    market_name_map: Dict[str, str] = {m["market_id"]: m["market_name"] for m in active_markets}

    if not wc_market_ids:
        logger.warning("No active WC markets — triggering refresh")
        refresh_wc_markets()
        return

    # Determine timestamp anchor
    last_ts_str = db.get_meta("last_poll_ts")
    after_ts: Optional[int] = int(last_ts_str) if last_ts_str else None

    params: dict = {"limit": 500}
    if after_ts:
        params["after"] = after_ts

    try:
        resp = requests.get(f"{CLOB_BASE_URL}/trades", params=params, timeout=20)
        resp.raise_for_status()
        raw_trades: list = resp.json().get("data", [])
    except Exception as exc:
        logger.error("Trade fetch failed: %s", exc)
        return

    # --- Process ---
    processed = 0
    new_max_ts = after_ts or 0
    portfolio = db.get_latest_portfolio()
    portfolio_value = portfolio["total_value"] if portfolio else STARTING_CAPITAL
    current_game = games_completed + 1

    for raw in raw_trades:
        trade = _parse_trade(raw)
        if not trade:
            continue

        new_max_ts = max(new_max_ts, trade["timestamp_int"])

        # ── WC 2026 gate (data ingestion) ────────────────────────────────────
        # wc_market_ids is built exclusively from markets written by
        # refresh_wc_markets(), which applies the "World Cup"/"FIFA"/"2026"
        # keyword filter.  Any trade whose market_id is absent is silently
        # dropped here — it is never stored, scored, or passed to the
        # executor, regardless of which wallet placed it.
        if trade["market_id"] not in wc_market_ids:
            continue
        # ─────────────────────────────────────────────────────────────────────

        # Filter 2: minimum USD size
        if trade["size_usd"] < MIN_TRADE_SIZE_USD:
            continue

        # Deduplication
        if db.is_trade_seen(trade["trade_id"]):
            continue
        db.mark_trade_seen(trade["trade_id"])

        wallet = trade["wallet"]
        if not wallet:
            continue

        market_name = market_name_map.get(trade["market_id"], trade["market_id"])

        # Ensure wallet row exists; detect if genuinely new
        is_new_wallet = db.upsert_wallet(wallet)

        db.insert_whale_trade(
            trade_id=trade["trade_id"],
            wallet=wallet,
            market_id=trade["market_id"],
            market_name=market_name,
            outcome=trade["outcome"],
            size_usd=trade["size_usd"],
            price=trade["price"],
            side=trade["side"],
            timestamp=trade["timestamp"],
            game_number=current_game,
        )

        score = scorer.recalculate_wallet_score(wallet)

        # Eligibility alert (first time wallet crosses MIN_TRADES_ELIGIBLE)
        if is_new_wallet and score["trade_count"] >= MIN_TRADES_ELIGIBLE:
            alerter.alert_new_whale(wallet, score["trade_count"])

        # Status-change alerts
        if score["old_status"] != score["new_status"]:
            if score["new_status"] == "followed":
                alerter.alert_whale_promoted(
                    wallet, score["win_rate"], score["trade_count"]
                )
            elif (
                score["new_status"] in ("demoted", "watching")
                and score["old_status"] == "followed"
            ):
                alerter.alert_whale_demoted(wallet, score["win_rate"])

        logger.info(
            "Whale: %s…  market=%s  %s  $%.0f @ %.4f  status=%s",
            wallet[:10], market_name[:35], trade["outcome"],
            trade["size_usd"], trade["price"], score["new_status"],
        )
        processed += 1

        # Phase 2: copy the trade if the wallet is followed
        if phase == 2 and score["new_status"] == "followed" and trade["side"] == "BUY":
            copy_payload = {
                **trade,
                "wallet_address": wallet,
                "market_name": market_name,
                "game_number": current_game,
            }
            result = executor.execute_copy_trade(copy_payload, portfolio_value)
            if result:
                alerter.alert_trade_executed(
                    market_name, trade["outcome"],
                    result["size_usd"], result["kelly_fraction"],
                    wallet, dry_run=DRY_RUN,
                )

    # Update timestamp anchor
    if new_max_ts:
        db.set_meta("last_poll_ts", str(new_max_ts + 1))

    if processed:
        logger.info("Processed %d new whale trade(s) this cycle", processed)

    # Update portfolio snapshot
    _update_portfolio_snapshot(portfolio_value)


def _update_portfolio_snapshot(base_value: float) -> None:
    open_pos = db.get_open_positions()
    invested = sum(p["size_usd"] + (p.get("manual_topup") or 0.0) for p in open_pos)
    closed_pos = db.get_closed_positions()
    realised = sum((p.get("realised_pnl") or 0.0) for p in closed_pos)
    unrealised = sum(
        p["token_amount"] * p.get("current_price", p["entry_price"]) - p["size_usd"]
        for p in open_pos
        if p.get("current_price")
    )
    total_pnl = realised + unrealised
    total_value = base_value + total_pnl
    db.save_portfolio_snapshot(total_value, invested, total_value - invested, total_pnl)


# ── Daily summary ─────────────────────────────────────────────────────────────

def send_daily_summary() -> None:
    portfolio = db.get_latest_portfolio()
    from config import STARTING_CAPITAL as SC
    total_value = portfolio["total_value"] if portfolio else SC
    invested = portfolio["invested_value"] if portfolio else 0.0
    total_pnl = portfolio["total_pnl"] if portfolio else 0.0

    open_pos = db.get_open_positions()
    closed_pos = db.get_closed_positions()
    followed = db.get_followed_wallets()
    top_whale = max(followed, key=lambda w: w["win_rate"]) if followed else None

    alerter.alert_daily_summary(
        total_value, invested, len(open_pos), len(closed_pos), total_pnl, top_whale
    )
    logger.info("Daily summary sent")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    logger.info("Starting Polymarket WC Bot  DRY_RUN=%s", DRY_RUN)

    db.init_db()

    # Seed timestamp so we only process trades from now onwards on first run
    if not db.get_meta("last_poll_ts"):
        db.set_meta("last_poll_ts", str(_now_ts()))
        logger.info("Initialised poll timestamp anchor to now")

    if not db.get_latest_portfolio():
        db.save_portfolio_snapshot(STARTING_CAPITAL, 0.0, STARTING_CAPITAL, 0.0)

    # Initial market sync before first poll
    refresh_wc_markets()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(poll_trades, "interval", seconds=POLL_INTERVAL_SECONDS, id="poll")
    scheduler.add_job(refresh_wc_markets, "interval", minutes=10, id="markets")
    scheduler.add_job(check_market_resolutions, "interval", minutes=5, id="resolutions")
    scheduler.add_job(send_daily_summary, "cron", hour=0, minute=0, id="summary")
    scheduler.start()
    logger.info("Scheduler running. Poll interval=%ds", POLL_INTERVAL_SECONDS)

    def _shutdown(signum, frame):
        logger.info("Signal %s received — shutting down", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while True:
            time.sleep(10)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()
