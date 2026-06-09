import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, List, Dict

from config import DB_PATH

logger = logging.getLogger(__name__)


@contextmanager
def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS markets (
                market_id       TEXT PRIMARY KEY,
                market_name     TEXT,
                question        TEXT,
                active          INTEGER DEFAULT 1,
                resolved        INTEGER DEFAULT 0,
                winning_outcome TEXT,
                end_date        TEXT,
                created_at      TEXT,
                last_checked    TEXT
            );

            CREATE TABLE IF NOT EXISTS seen_trades (
                trade_id     TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS whale_trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id       TEXT UNIQUE NOT NULL,
                wallet_address TEXT NOT NULL,
                market_id      TEXT NOT NULL,
                market_name    TEXT,
                outcome        TEXT,
                size_usd       REAL,
                price          REAL,
                side           TEXT,
                timestamp      TEXT,
                game_number    INTEGER,
                trade_result   TEXT,   -- 'win' | 'loss' | 'neutral'
                pnl            REAL
            );

            CREATE INDEX IF NOT EXISTS idx_wt_wallet ON whale_trades(wallet_address);
            CREATE INDEX IF NOT EXISTS idx_wt_market ON whale_trades(market_id);

            CREATE TABLE IF NOT EXISTS wallets (
                address              TEXT PRIMARY KEY,
                trade_count          INTEGER DEFAULT 0,
                resolved_trade_count INTEGER DEFAULT 0,
                win_count            INTEGER DEFAULT 0,
                win_rate             REAL    DEFAULT 0.0,
                total_pnl            REAL    DEFAULT 0.0,
                total_invested       REAL    DEFAULT 0.0,
                roi                  REAL    DEFAULT 0.0,
                status               TEXT    DEFAULT 'watching',
                first_seen           TEXT,
                last_updated         TEXT
            );

            CREATE TABLE IF NOT EXISTS positions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id     TEXT,
                market_name   TEXT,
                outcome       TEXT,
                side          TEXT,
                size_usd      REAL,
                token_amount  REAL,
                entry_price   REAL,
                current_price REAL,
                kelly_fraction REAL,
                whale_address TEXT,
                game_number   INTEGER,
                status        TEXT DEFAULT 'open',
                result        TEXT,
                realised_pnl  REAL,
                opened_at     TEXT,
                closed_at     TEXT,
                order_id      TEXT,
                manual_topup  REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                total_value    REAL,
                invested_value REAL,
                free_capital   REAL,
                total_pnl      REAL,
                snapshot_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS phase_tracking (
                id              INTEGER PRIMARY KEY DEFAULT 1,
                games_completed INTEGER DEFAULT 0,
                last_updated    TEXT
            );

            INSERT OR IGNORE INTO phase_tracking (id, games_completed, last_updated)
            VALUES (1, 0, datetime('now'));
        """)
        # Migration: add end_date to existing DBs that predate this column
        try:
            conn.execute("ALTER TABLE markets ADD COLUMN end_date TEXT")
        except Exception:
            pass  # column already exists
    logger.info("Database initialised at %s", DB_PATH)


# ── Metadata ──────────────────────────────────────────────────────────────────

def get_meta(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?,?)",
            (key, value),
        )


# ── Markets ───────────────────────────────────────────────────────────────────

def upsert_market(
    market_id: str, name: str, question: str,
    active: bool = True, resolved: bool = False,
    winning_outcome: Optional[str] = None,
    end_date: Optional[str] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO markets (market_id, market_name, question, active, resolved,
                                 winning_outcome, end_date, created_at, last_checked)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(market_id) DO UPDATE SET
                active          = excluded.active,
                resolved        = excluded.resolved,
                winning_outcome = COALESCE(excluded.winning_outcome, winning_outcome),
                end_date        = COALESCE(excluded.end_date, end_date),
                last_checked    = excluded.last_checked
            """,
            (market_id, name[:200], question[:500],
             int(active), int(resolved), winning_outcome, end_date, now, now),
        )


def get_all_markets() -> List[Dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM markets").fetchall()]


def get_market(market_id: str) -> Optional[Dict]:
    """Return the market row if it exists in the WC markets table, else None.

    Because only WC 2026 markets are written to this table (via upsert_market,
    which is only called from refresh_wc_markets after keyword filtering), a
    non-None return value is a reliable signal that the market is a WC market.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM markets WHERE market_id=?", (market_id,)
        ).fetchone()
        return dict(row) if row else None


def get_active_wc_markets() -> List[Dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM markets WHERE active=1 AND resolved=0"
            ).fetchall()
        ]


def mark_market_resolved(market_id: str, winning_outcome: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE markets SET resolved=1, active=0, winning_outcome=? WHERE market_id=?",
            (winning_outcome, market_id),
        )


# ── Seen trades ───────────────────────────────────────────────────────────────

def is_trade_seen(trade_id: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM seen_trades WHERE trade_id=?", (trade_id,)
        ).fetchone() is not None


def mark_trade_seen(trade_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_trades (trade_id, processed_at) VALUES (?,?)",
            (trade_id, now),
        )


# ── Whale trades ──────────────────────────────────────────────────────────────

def insert_whale_trade(
    trade_id: str, wallet: str, market_id: str, market_name: str,
    outcome: str, size_usd: float, price: float, side: str,
    timestamp: str, game_number: int,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO whale_trades
                (trade_id, wallet_address, market_id, market_name,
                 outcome, size_usd, price, side, timestamp, game_number)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (trade_id, wallet, market_id, market_name,
             outcome, size_usd, price, side, timestamp, game_number),
        )


def get_trades_by_wallet(wallet: str) -> List[Dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM whale_trades WHERE wallet_address=? ORDER BY timestamp DESC",
                (wallet,),
            ).fetchall()
        ]


def get_recent_whale_trades(limit: int = 200) -> List[Dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM whale_trades ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        ]


def resolve_wallet_trades(market_id: str, winning_outcome: str) -> None:
    """Score all unresolved trades for a market and write win/loss + P&L."""
    with get_conn() as conn:
        trades = conn.execute(
            "SELECT * FROM whale_trades WHERE market_id=? AND trade_result IS NULL",
            (market_id,),
        ).fetchall()

        for t in trades:
            t = dict(t)
            if t["side"] != "BUY":
                result, pnl = "neutral", 0.0
            elif t["outcome"] == winning_outcome:
                result = "win"
                # Profit = tokens * (1 - price) where tokens = size_usd / price
                pnl = t["size_usd"] * (1 - t["price"]) / t["price"] if t["price"] else 0.0
            else:
                result = "loss"
                pnl = -t["size_usd"]

            conn.execute(
                "UPDATE whale_trades SET trade_result=?, pnl=? WHERE id=?",
                (result, pnl, t["id"]),
            )


# ── Wallets ───────────────────────────────────────────────────────────────────

def upsert_wallet(address: str) -> bool:
    """Insert wallet if new. Returns True if it was inserted (new)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO wallets (address, first_seen, last_updated) VALUES (?,?,?)",
            (address, now, now),
        )
        return cur.rowcount > 0


def get_wallet(address: str) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()
        return dict(row) if row else None


def get_all_wallets() -> List[Dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM wallets ORDER BY win_rate DESC, trade_count DESC"
            ).fetchall()
        ]


def get_followed_wallets() -> List[Dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM wallets WHERE status='followed'"
            ).fetchall()
        ]


def update_wallet_score(
    address: str, trade_count: int, resolved_count: int, win_count: int,
    win_rate: float, total_pnl: float, total_invested: float, roi: float,
    status: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE wallets SET
                trade_count=?, resolved_trade_count=?, win_count=?,
                win_rate=?, total_pnl=?, total_invested=?, roi=?,
                status=?, last_updated=?
            WHERE address=?
            """,
            (trade_count, resolved_count, win_count,
             win_rate, total_pnl, total_invested, roi,
             status, now, address),
        )


# ── Positions ─────────────────────────────────────────────────────────────────

def insert_position(
    market_id: str, market_name: str, outcome: str, side: str,
    size_usd: float, token_amount: float, entry_price: float,
    kelly_fraction: float, whale_address: str, game_number: int,
    order_id: Optional[str] = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO positions
                (market_id, market_name, outcome, side, size_usd, token_amount,
                 entry_price, current_price, kelly_fraction, whale_address,
                 game_number, opened_at, order_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (market_id, market_name, outcome, side, size_usd, token_amount,
             entry_price, entry_price, kelly_fraction, whale_address,
             game_number, now, order_id),
        )
        return cur.lastrowid


def get_open_positions() -> List[Dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
            ).fetchall()
        ]


def get_closed_positions() -> List[Dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC"
            ).fetchall()
        ]


def get_open_exposure_for_game(game_number: int) -> float:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(size_usd + manual_topup), 0.0) AS total
               FROM positions
               WHERE game_number=? AND status='open'""",
            (game_number,),
        ).fetchone()
        return float(row["total"]) if row else 0.0


def close_position(position_id: int, result: str, realised_pnl: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE positions SET status='closed', result=?, realised_pnl=?, closed_at=? WHERE id=?",
            (result, realised_pnl, now, position_id),
        )


def update_position_price(position_id: int, current_price: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE positions SET current_price=? WHERE id=?",
            (current_price, position_id),
        )


def add_manual_topup(position_id: int, amount: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE positions SET manual_topup = manual_topup + ? WHERE id=?",
            (amount, position_id),
        )


# ── Portfolio snapshots ───────────────────────────────────────────────────────

def save_portfolio_snapshot(
    total_value: float, invested: float, free: float, pnl: float
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO portfolio_snapshots
               (total_value, invested_value, free_capital, total_pnl, snapshot_at)
               VALUES (?,?,?,?,?)""",
            (total_value, invested, free, pnl, now),
        )


def get_latest_portfolio() -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY snapshot_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_portfolio_snapshots(limit: int = 500) -> List[Dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY snapshot_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        ]


# ── Phase tracking ────────────────────────────────────────────────────────────

def get_games_completed() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT games_completed FROM phase_tracking WHERE id=1"
        ).fetchone()
        return int(row["games_completed"]) if row else 0


def increment_games_completed() -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE phase_tracking SET games_completed=games_completed+1, last_updated=? WHERE id=1",
            (now,),
        )
    return get_games_completed()
