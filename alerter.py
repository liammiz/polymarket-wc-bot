"""
Telegram alert module.

Uses direct HTTP requests to avoid asyncio/threading conflicts.
All functions are fire-and-forget; failures are logged but never raised.
"""
import logging
from datetime import datetime, timezone
from typing import Optional, Dict

import requests

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def _send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping alert")
        return
    try:
        requests.post(
            _BASE_URL,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def _short(address: str) -> str:
    if address and len(address) > 14:
        return f"`{address[:8]}…{address[-6:]}`"
    return f"`{address}`"


# ── Alert functions ───────────────────────────────────────────────────────────

def alert_new_whale(address: str, trade_count: int) -> None:
    _send(
        f"🐋 *New Whale Eligible*\n"
        f"Address: {_short(address)}\n"
        f"WC Trades: {trade_count}\n"
        f"Status: watching"
    )


def alert_whale_promoted(address: str, win_rate: float, trade_count: int) -> None:
    _send(
        f"🟢 *Whale PROMOTED → FOLLOWED*\n"
        f"Address: {_short(address)}\n"
        f"Win Rate: {win_rate:.1%}\n"
        f"Resolved Trades: {trade_count}"
    )


def alert_whale_demoted(address: str, win_rate: float) -> None:
    _send(
        f"🔴 *Whale DEMOTED*\n"
        f"Address: {_short(address)}\n"
        f"Win Rate fell to: {win_rate:.1%}"
    )


def alert_trade_executed(
    market_name: str,
    outcome: str,
    size_usd: float,
    kelly_fraction: float,
    whale_address: str,
    dry_run: bool = False,
) -> None:
    prefix = "🧪 [DRY RUN] " if dry_run else "✅ "
    _send(
        f"{prefix}*Trade Executed*\n"
        f"Market: {market_name[:60]}\n"
        f"Side: BUY {outcome}\n"
        f"Size: ${size_usd:,.2f}\n"
        f"Kelly: {kelly_fraction:.1%}\n"
        f"Copying: {_short(whale_address)}"
    )


def alert_position_closed(
    market_name: str,
    outcome: str,
    size_usd: float,
    result: str,
    pnl: float,
) -> None:
    icon = "🏆" if result == "win" else "💸"
    sign = "+" if pnl >= 0 else ""
    _send(
        f"{icon} *Position Closed*\n"
        f"Market: {market_name[:60]}\n"
        f"Outcome: {outcome}\n"
        f"Size: ${size_usd:,.2f}\n"
        f"Result: {result.upper()}\n"
        f"P&L: {sign}${pnl:,.2f}"
    )


def alert_daily_summary(
    total_value: float,
    invested: float,
    open_count: int,
    closed_count: int,
    total_pnl: float,
    top_whale: Optional[Dict] = None,
) -> None:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sign = "+" if total_pnl >= 0 else ""
    whale_line = ""
    if top_whale:
        whale_line = (
            f"\nTop Whale: {_short(top_whale['address'])}"
            f" ({top_whale['win_rate']:.1%} WR)"
        )
    _send(
        f"📊 *Daily Summary — {date_str} UTC*\n"
        f"Portfolio: ${total_value:,.2f}\n"
        f"Invested: ${invested:,.2f}\n"
        f"Open Positions: {open_count}\n"
        f"Closed Positions: {closed_count}\n"
        f"Total P&L: {sign}${total_pnl:,.2f}"
        f"{whale_line}"
    )
