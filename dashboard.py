"""
Streamlit dashboard for the Polymarket WC copy-trading bot.

Run with:
    streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0 --server.headless true

Reads exclusively from whales.db — never writes market orders directly
(that path goes through executor.execute_manual_topup).
"""

import sys
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

# Ensure project root is on path when launched from any CWD
sys.path.insert(0, str(Path(__file__).parent.resolve()))

import db
import executor
from config import STARTING_CAPITAL, DRY_RUN, PHASE_THRESHOLD, GAME_CAP_PCT

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Polymarket WC Bot",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Optional auto-refresh (30 s) — silently skipped if package absent
try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=30_000, key="autorefresh")
except ImportError:
    pass

db.init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_usd(v) -> str:
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}${v:,.2f}" if v < 0 else f"${v:,.2f}"


def fmt_pct(v) -> str:
    return f"{v:.1%}" if v is not None else "—"


def short_addr(a: str) -> str:
    if a and len(a) > 14:
        return f"{a[:8]}…{a[-6:]}"
    return a or "—"


def pnl_color(v: float) -> str:
    return "green" if v >= 0 else "red"


# ── Header ────────────────────────────────────────────────────────────────────

st.markdown("# ⚽ Polymarket World Cup 2026 Bot")

games = db.get_games_completed()
phase = 1 if games < PHASE_THRESHOLD else 2
mode_label = "🧪 DRY RUN" if DRY_RUN else "🔴 LIVE"
phase_label = "🟡 PHASE 1 — DATA COLLECTION" if phase == 1 else "🟢 PHASE 2 — LIVE TRADING"

col_h1, col_h2, col_h3 = st.columns(3)
col_h1.info(phase_label)
col_h2.info(f"Mode: {mode_label}")
col_h3.info(f"Games completed: {games} / {PHASE_THRESHOLD}")

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_port, tab_whales, tab_open, tab_closed, tab_override = st.tabs([
    "📊 Portfolio",
    "🐋 Whales",
    "📂 Open Positions",
    "✅ Closed Positions",
    "🎛️ Manual Override",
])


# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — Portfolio overview
# ════════════════════════════════════════════════════════════════════════════
with tab_port:
    st.header("Portfolio Overview")

    portfolio = db.get_latest_portfolio()
    open_positions = db.get_open_positions()
    closed_positions = db.get_closed_positions()

    total_value = portfolio["total_value"] if portfolio else STARTING_CAPITAL
    invested = portfolio["invested_value"] if portfolio else 0.0
    free = portfolio["free_capital"] if portfolio else total_value
    total_pnl = portfolio["total_pnl"] if portfolio else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Portfolio Value", f"${total_value:,.2f}")
    c2.metric("Currently Invested", f"${invested:,.2f}")
    c3.metric("Free Capital", f"${free:,.2f}")
    delta_str = fmt_usd(total_pnl)
    c4.metric("Total P&L", f"${abs(total_pnl):,.2f}", delta=delta_str)

    st.divider()

    c5, c6, c7, c8 = st.columns(4)
    realised = sum((p.get("realised_pnl") or 0.0) for p in closed_positions)
    unrealised = sum(
        p["token_amount"] * p.get("current_price", p["entry_price"]) - p["size_usd"]
        for p in open_positions
    )
    c5.metric("Realised P&L", fmt_usd(realised))
    c6.metric("Unrealised P&L", fmt_usd(unrealised))
    c7.metric("Open Positions", len(open_positions))
    c8.metric("Closed Positions", len(closed_positions))

    st.caption(
        f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )


# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — Followed whales
# ════════════════════════════════════════════════════════════════════════════
with tab_whales:
    st.header("Whale Tracker")

    wallets = db.get_all_wallets()

    if not wallets:
        st.info("No whale wallets tracked yet — data collection in progress.")
    else:
        followed = [w for w in wallets if w["status"] == "followed"]
        watching = [w for w in wallets if w["status"] == "watching"]
        demoted = [w for w in wallets if w["status"] == "demoted"]

        ca, cb, cc, cd = st.columns(4)
        ca.metric("Total Wallets", len(wallets))
        cb.metric("Followed", len(followed), help="win rate ≥ 80% and ≥ 5 trades")
        cc.metric("Watching", len(watching))
        cd.metric("Demoted", len(demoted))

        st.divider()

        filter_status = st.selectbox(
            "Filter by status", ["All", "followed", "watching", "demoted"]
        )
        display = (
            wallets if filter_status == "All"
            else [w for w in wallets if w["status"] == filter_status]
        )

        rows = []
        for w in display:
            rows.append({
                "Address": short_addr(w["address"]),
                "Trades": w["trade_count"],
                "Resolved": w["resolved_trade_count"],
                "Wins": w["win_count"],
                "Win Rate": fmt_pct(w["win_rate"]),
                "Total P&L": fmt_usd(w["total_pnl"]),
                "ROI": fmt_pct(w["roi"]),
                "Status": w["status"].upper(),
                "Last Updated": (w["last_updated"] or "")[:19],
            })

        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)

            st.markdown(
                "> **FOLLOWED** = copying trades | "
                "**WATCHING** = monitoring | "
                "**DEMOTED** = dropped below 80% win rate"
            )


# ════════════════════════════════════════════════════════════════════════════
# TAB 3 — Open positions
# ════════════════════════════════════════════════════════════════════════════
with tab_open:
    st.header("Open Positions")

    open_pos = db.get_open_positions()

    if not open_pos:
        st.info("No open positions.")
    else:
        rows = []
        for p in open_pos:
            cur_price = p.get("current_price") or p["entry_price"]
            unrealised = p["token_amount"] * cur_price - p["size_usd"]
            rows.append({
                "ID": p["id"],
                "Market": (p["market_name"] or "")[:50],
                "Outcome": p["outcome"],
                "Size": f"${p['size_usd']:,.2f}",
                "Top-up": f"${p.get('manual_topup', 0.0):,.2f}",
                "Entry": f"{p['entry_price']:.4f}",
                "Current": f"{cur_price:.4f}",
                "Unrealised P&L": fmt_usd(unrealised),
                "Kelly": fmt_pct(p["kelly_fraction"]),
                "Whale": short_addr(p["whale_address"]),
                "Game #": p["game_number"],
                "Opened": (p["opened_at"] or "")[:19],
            })

        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        total_unr = sum(
            p["token_amount"] * (p.get("current_price") or p["entry_price"]) - p["size_usd"]
            for p in open_pos
        )
        st.metric("Total Unrealised P&L", fmt_usd(total_unr))


# ════════════════════════════════════════════════════════════════════════════
# TAB 4 — Closed positions
# ════════════════════════════════════════════════════════════════════════════
with tab_closed:
    st.header("Closed Positions")

    closed_pos = db.get_closed_positions()

    if not closed_pos:
        st.info("No closed positions yet.")
    else:
        rows = []
        for p in closed_pos:
            rows.append({
                "Market": (p["market_name"] or "")[:50],
                "Outcome": p["outcome"],
                "Size": f"${p['size_usd']:,.2f}",
                "Entry": f"{p['entry_price']:.4f}",
                "Result": (p["result"] or "—").upper(),
                "Realised P&L": fmt_usd(p.get("realised_pnl")),
                "Whale": short_addr(p["whale_address"]),
                "Closed": (p["closed_at"] or "")[:19],
            })

        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        tot = sum((p.get("realised_pnl") or 0.0) for p in closed_pos)
        wins = sum(1 for p in closed_pos if p["result"] == "win")
        losses = sum(1 for p in closed_pos if p["result"] == "loss")

        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("Realised P&L", fmt_usd(tot))
        sc2.metric("Wins", wins)
        sc3.metric("Losses", losses)


# ════════════════════════════════════════════════════════════════════════════
# TAB 5 — Manual override
# ════════════════════════════════════════════════════════════════════════════
with tab_override:
    st.header("Manual Position Top-Up")
    st.markdown(
        "Add extra capital to an open position. "
        "Bypasses Kelly sizing but still respects the "
        f"**{GAME_CAP_PCT:.0%} per-game cap**."
    )

    open_pos = db.get_open_positions()

    if not open_pos:
        st.info("No open positions to top up.")
    else:
        options = {
            f"[#{p['id']}]  {(p['market_name'] or '')[:45]}  —  {p['outcome']}  "
            f"(${p['size_usd']:,.0f} + ${p.get('manual_topup', 0.0):,.0f} topup)": p["id"]
            for p in open_pos
        }

        selected_label = st.selectbox("Select position", list(options.keys()))
        selected_id = options[selected_label]
        pos = next(p for p in open_pos if p["id"] == selected_id)

        # Compute remaining game cap
        portfolio = db.get_latest_portfolio()
        portfolio_value = portfolio["total_value"] if portfolio else STARTING_CAPITAL
        game_cap = portfolio_value * GAME_CAP_PCT
        exposure = db.get_open_exposure_for_game(pos["game_number"])
        remaining = max(0.0, game_cap - exposure)

        col_det, col_frm = st.columns(2)

        with col_det:
            st.markdown("**Position details**")
            st.write(f"**Market:** {pos['market_name']}")
            st.write(f"**Outcome:** {pos['outcome']}")
            st.write(f"**Entry price:** {pos['entry_price']:.4f}")
            st.write(f"**Current size:** ${pos['size_usd']:,.2f}")
            st.write(f"**Prior top-ups:** ${pos.get('manual_topup', 0.0):,.2f}")
            st.write(f"**Game #:** {pos['game_number']}")
            if remaining > 0:
                st.success(f"Remaining game cap: ${remaining:,.2f}")
            else:
                st.error("Game cap exhausted — cannot top up.")

        with col_frm:
            st.markdown("**Add capital**")

            max_topup = max(1.0, remaining)
            default_topup = min(100.0, remaining) if remaining > 1 else 1.0

            topup_amount = st.number_input(
                "Amount (USD)",
                min_value=1.0,
                max_value=float(max_topup),
                value=float(default_topup),
                step=10.0,
                disabled=remaining <= 0,
            )

            confirm = st.checkbox("I understand this bypasses Kelly sizing")

            if st.button(
                "Execute Top-Up",
                disabled=(not confirm or remaining <= 0),
                type="primary",
            ):
                ok = executor.execute_manual_topup(selected_id, topup_amount)
                if ok:
                    st.success(f"✅ Top-up of ${topup_amount:,.2f} applied to position #{selected_id}")
                    st.rerun()
                else:
                    st.error("Top-up failed — check bot.log for details")

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"Polymarket WC Bot  •  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    f"  •  DB: {db.DB_PATH.name}"
)
