import logging
from typing import Optional, Dict, Generator, Tuple

from config import (
    DRY_RUN,
    POLY_PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET,
    POLY_API_PASSPHRASE, POLY_CHAIN_ID, CLOB_BASE_URL,
    GAME_CAP_PCT,
)
import db
from scorer import size_position

logger = logging.getLogger(__name__)

_client = None  # lazy-initialised CLOB client


def _get_client():
    global _client
    if _client is None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        creds = ApiCreds(
            api_key=POLY_API_KEY,
            api_secret=POLY_API_SECRET,
            api_passphrase=POLY_API_PASSPHRASE,
        )
        _client = ClobClient(
            host=CLOB_BASE_URL,
            key=POLY_PRIVATE_KEY,
            chain_id=POLY_CHAIN_ID,
            creds=creds,
        )
        logger.info("CLOB client initialised")
    return _client


def _get_token_id(market_id: str, outcome: str) -> Optional[str]:
    try:
        client = _get_client()
        market = client.get_market(market_id)
        if not market:
            return None
        for token in market.tokens:
            if token.outcome.lower() == outcome.lower():
                return token.token_id
    except Exception as e:
        logger.error("get_token_id failed for %s/%s: %s", market_id, outcome, e)
    return None


def _place_order(token_id: str, price: float, size_tokens: float) -> Optional[str]:
    """Submit a GTC limit order. Returns order_id string."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    client = _get_client()
    order = client.create_order(OrderArgs(
        token_id=token_id,
        price=round(price, 4),
        size=round(size_tokens, 2),
        side=BUY,
    ))
    resp = client.post_order(order, OrderType.GTC)
    return resp.get("orderID") or resp.get("order_id", "unknown")


# ── Public API ────────────────────────────────────────────────────────────────

def execute_copy_trade(whale_trade: Dict, portfolio_value: float) -> Optional[Dict]:
    """
    Mirror a whale's BUY trade.
    Applies Kelly sizing + 7 % game cap.
    Returns position metadata dict on success, None if skipped.
    """
    market_id = whale_trade["market_id"]
    market_name = whale_trade.get("market_name", market_id)
    outcome = whale_trade["outcome"]
    price = float(whale_trade["price"])
    side = whale_trade.get("side", "BUY").upper()
    game_number = whale_trade["game_number"]
    whale_address = whale_trade["wallet_address"]

    # ── WC 2026 guard ────────────────────────────────────────────────────────
    # Only markets written by refresh_wc_markets() (keyword-filtered for
    # "World Cup", "FIFA", "2026") exist in the markets table.  If this
    # market_id is absent the trade is NOT a WC market and must be dropped
    # unconditionally — no execution, no logging as a copy trade.
    if not db.get_market(market_id):
        logger.warning(
            "IGNORED: market %s is not a tracked WC 2026 market — "
            "whale %s trade will not be copied",
            market_id, whale_address,
        )
        return None
    # ─────────────────────────────────────────────────────────────────────────

    if side != "BUY":
        logger.debug("Skipping non-BUY trade from %s", whale_address)
        return None

    wallet = db.get_wallet(whale_address)
    if not wallet or wallet["status"] != "followed":
        logger.debug("Wallet %s not followed, skipping", whale_address)
        return None

    position_size, kelly_fraction = size_position(
        portfolio_value, wallet["win_rate"], price, game_number
    )

    if position_size < 5.0:
        logger.info("Position size $%.2f too small for %s, skipping", position_size, market_name)
        return None

    token_amount = position_size / price if price > 0 else 0.0

    if DRY_RUN:
        order_id = f"dry_{market_id[:8]}_{outcome}"
        logger.info(
            "[DRY RUN] BUY %.2f %s tokens in '%s' @ %.4f  size=$%.2f  kelly=%.3f",
            token_amount, outcome, market_name, price, position_size, kelly_fraction,
        )
    else:
        try:
            token_id = _get_token_id(market_id, outcome)
            if not token_id:
                logger.error("No token_id for %s/%s", market_id, outcome)
                return None
            order_id = _place_order(token_id, price, token_amount)
            logger.info("Order %s placed: BUY %.2f %s @ %.4f", order_id, token_amount, outcome, price)
        except Exception as e:
            logger.error("Order placement failed: %s", e)
            return None

    position_id = db.insert_position(
        market_id=market_id,
        market_name=market_name,
        outcome=outcome,
        side="BUY",
        size_usd=position_size,
        token_amount=token_amount,
        entry_price=price,
        kelly_fraction=kelly_fraction,
        whale_address=whale_address,
        game_number=game_number,
        order_id=order_id,
    )

    return {
        "position_id": position_id,
        "market_id": market_id,
        "market_name": market_name,
        "outcome": outcome,
        "size_usd": position_size,
        "entry_price": price,
        "kelly_fraction": kelly_fraction,
        "order_id": order_id,
        "dry_run": DRY_RUN,
    }


def execute_manual_topup(position_id: int, amount_usd: float) -> bool:
    """
    Add extra capital to an open position.
    Bypasses Kelly but enforces the 7 % game cap.
    """
    open_pos = db.get_open_positions()
    pos = next((p for p in open_pos if p["id"] == position_id), None)
    if not pos:
        logger.error("Position %d not found or not open", position_id)
        return False

    portfolio = db.get_latest_portfolio()
    from config import STARTING_CAPITAL
    portfolio_value = portfolio["total_value"] if portfolio else STARTING_CAPITAL

    game_cap = portfolio_value * GAME_CAP_PCT
    current_exposure = db.get_open_exposure_for_game(pos["game_number"])
    remaining_cap = max(0.0, game_cap - current_exposure)

    actual = min(amount_usd, remaining_cap)
    if actual < 1.0:
        logger.warning("Game cap reached for game %d, cannot top up", pos["game_number"])
        return False

    if DRY_RUN:
        logger.info("[DRY RUN] Top-up $%.2f on position %d", actual, position_id)
    else:
        try:
            token_id = _get_token_id(pos["market_id"], pos["outcome"])
            if not token_id:
                return False
            token_amount = actual / pos["entry_price"] if pos["entry_price"] > 0 else 0.0
            _place_order(token_id, pos["entry_price"], token_amount)
        except Exception as e:
            logger.error("Top-up order failed: %s", e)
            return False

    db.add_manual_topup(position_id, actual)
    logger.info("Top-up $%.2f added to position %d", actual, position_id)
    return True


def close_resolved_positions(
    market_id: str,
    winning_outcome: str,
) -> Generator[Tuple[Dict, str, float], None, None]:
    """
    Close every open position for a resolved market.
    Yields (position_dict, result, realised_pnl) for each closed position.
    """
    for pos in db.get_open_positions():
        if pos["market_id"] != market_id:
            continue

        total_size = pos["size_usd"] + (pos.get("manual_topup") or 0.0)
        token_amount = pos.get("token_amount") or 0.0
        entry_price = pos["entry_price"]

        is_win = (pos["outcome"] == winning_outcome and pos["side"] == "BUY")

        if is_win:
            result = "win"
            # Tokens pay out at $1 each; we paid entry_price per token
            realised_pnl = token_amount * (1.0 - entry_price)
        else:
            result = "loss"
            realised_pnl = -total_size

        db.close_position(pos["id"], result, realised_pnl)
        logger.info(
            "Closed position %d (%s): %s  P&L=$%.2f",
            pos["id"], pos["market_name"], result, realised_pnl,
        )
        yield pos, result, realised_pnl
