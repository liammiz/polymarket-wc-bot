import logging
from typing import Dict, Tuple

from config import WIN_RATE_THRESHOLD, MIN_TRADES_ELIGIBLE, GAME_CAP_PCT
import db

logger = logging.getLogger(__name__)

def calculate_kelly(win_rate: float, price: float) -> float:
    """
    Kelly Criterion adapted for binary prediction markets.

    In a Polymarket binary market:
      - You pay `price` USDC per share
      - A winning share pays 1 USDC → net odds b = (1-price) / price
      - f* = (b·p - q) / b   where p=win_rate, q=1-win_rate

    Returns the raw Kelly fraction floored at 0.
    The only external constraint is the 7% per-game exposure cap in size_position().
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    b = (1.0 - price) / price
    if b <= 0.0:
        return 0.0
    f_star = (b * win_rate - (1.0 - win_rate)) / b
    return max(0.0, f_star)


def recalculate_wallet_score(address: str) -> Dict:
    """
    Recalculate a wallet's stats from its trade history and persist the result.
    Returns a dict including old and new status so the caller can detect changes.
    """
    trades = db.get_trades_by_wallet(address)

    trade_count = len(trades)
    resolved_trades = [t for t in trades if t["trade_result"] is not None]
    resolved_count = len(resolved_trades)
    win_trades = [t for t in resolved_trades if t["trade_result"] == "win"]
    win_count = len(win_trades)

    win_rate = win_count / resolved_count if resolved_count > 0 else 0.0

    buy_resolved = [t for t in resolved_trades if t["side"] == "BUY"]
    total_invested = sum(t["size_usd"] for t in buy_resolved)
    total_pnl = sum(t["pnl"] for t in buy_resolved if t["pnl"] is not None)
    roi = total_pnl / total_invested if total_invested > 0 else 0.0

    wallet = db.get_wallet(address)
    old_status = wallet["status"] if wallet else "watching"

    if trade_count < MIN_TRADES_ELIGIBLE:
        new_status = "watching"
    elif win_rate >= WIN_RATE_THRESHOLD:
        new_status = "followed"
    elif old_status == "followed":
        new_status = "demoted"
    else:
        new_status = "watching"

    db.update_wallet_score(
        address, trade_count, resolved_count, win_count,
        win_rate, total_pnl, total_invested, roi, new_status,
    )

    return {
        "address": address,
        "trade_count": trade_count,
        "resolved_count": resolved_count,
        "win_count": win_count,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "total_invested": total_invested,
        "roi": roi,
        "old_status": old_status,
        "new_status": new_status,
    }


def recalculate_all_wallet_scores() -> list:
    """
    Recalculate every known wallet. Returns only wallets whose status changed.
    Called after market resolution events.
    """
    changes = []
    for w in db.get_all_wallets():
        result = recalculate_wallet_score(w["address"])
        if result["old_status"] != result["new_status"]:
            changes.append(result)
    return changes


def size_position(
    portfolio_value: float,
    win_rate: float,
    price: float,
    game_number: int,
) -> Tuple[float, float]:
    """
    Compute position size (USD).

    Constraints (in priority order):
      1. Raw Kelly Criterion fraction of portfolio_value.
      2. 7% per-game hard cap — if Kelly exceeds remaining game capacity,
         scale down proportionally to fit within the cap.

    No other upper bound exists. Kelly can recommend any fraction > 0.

    Returns (position_size_usd, kelly_fraction).
    """
    kelly = calculate_kelly(win_rate, price)
    kelly_size = portfolio_value * kelly

    current_exposure = db.get_open_exposure_for_game(game_number)
    game_cap = portfolio_value * GAME_CAP_PCT
    remaining_cap = max(0.0, game_cap - current_exposure)

    position_size = min(kelly_size, remaining_cap)
    return position_size, kelly
