"""
Streamlit dashboard for the Polymarket WC copy-trading bot.

Run with:
    streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0 --server.headless true

Reads exclusively from whales.db — never writes market orders directly
(that path goes through executor.execute_manual_topup).
"""

import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Ensure project root is on path when launched from any CWD
sys.path.insert(0, str(Path(__file__).parent.resolve()))

import db
import executor
from config import (
    DRY_RUN, GAME_CAP_PCT, MIN_TRADE_SIZE_USD, MIN_TRADES_ELIGIBLE,
    PHASE_THRESHOLD, STARTING_CAPITAL, WIN_RATE_THRESHOLD,
)

_ENV_PATH = Path(__file__).parent / ".env"

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Polymarket WC Bot",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="collapsed",
)

from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=30_000, key="autorefresh")

db.init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_usd(v) -> str:
    if v is None:
        return "—"
    if v > 0:
        return f"+${v:,.2f}"
    elif v < 0:
        return f"-${abs(v):,.2f}"
    return "$0.00"


def fmt_pct(v) -> str:
    return f"{v:.1%}" if v is not None else "—"


def short_addr(a: str) -> str:
    if a and len(a) > 14:
        return f"{a[:8]}…{a[-6:]}"
    return a or "—"


def _pnl_cell_style(val: str) -> str:
    """Styler function: colour green/red based on the sign of a fmt_usd string."""
    s = str(val).replace("$", "").replace("+", "").replace(",", "").strip()
    try:
        num = float(s)
        if num > 0:
            return "color: #28a745; font-weight: bold"
        elif num < 0:
            return "color: #dc3545; font-weight: bold"
    except Exception:
        pass
    return ""


def _write_env_key(key: str, value: str) -> None:
    """Replace a KEY=value line in .env in-place; append if the key is absent."""
    text = _ENV_PATH.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(key)}=.*$"
    new_text = re.sub(pattern, f"{key}={value}", text, flags=re.MULTILINE)
    if f"{key}=" not in new_text:
        new_text += f"\n{key}={value}"
    _ENV_PATH.write_text(new_text, encoding="utf-8")


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

tab_port, tab_whales, tab_open, tab_closed, tab_override, tab_settings = st.tabs([
    "📊 Portfolio",
    "🐋 Whales",
    "📂 Open Positions",
    "✅ Closed Positions",
    "🎛️ Manual Override",
    "⚙️ Settings",
])


# ════════════════════════════════════════════════════════════════════════════════
# TAB 1 — Portfolio overview
# ════════════════════════════════════════════════════════════════════════════════
with tab_port:
    st.header("Portfolio Overview")
    st.info(f"{phase_label}  |  {games} / {PHASE_THRESHOLD} games completed")

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
    c4.metric("Total P&L", fmt_usd(total_pnl), delta=fmt_usd(total_pnl))

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

    st.divider()

    # Equity curve
    snapshots = db.get_portfolio_snapshots(500)
    if snapshots:
        snap_df = pd.DataFrame(snapshots)
        snap_df["snapshot_at"] = pd.to_datetime(snap_df["snapshot_at"], utc=True)
        snap_df = snap_df.sort_values("snapshot_at")

        st.subheader("Equity Curve")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=snap_df["snapshot_at"],
            y=snap_df["total_value"],
            mode="lines",
            name="Portfolio Value",
            line=dict(color="#1f77b4", width=2),
            fill="tozeroy",
            fillcolor="rgba(31,119,180,0.1)",
        ))
        fig.update_layout(
            xaxis_title="Time (UTC)",
            yaxis_title="Portfolio Value (USD)",
            height=350,
            margin=dict(l=0, r=0, t=20, b=0),
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True)

        # 24-hour sparkline
        cutoff = pd.Timestamp(datetime.now(timezone.utc) - timedelta(hours=24))
        snap_24h = snap_df[snap_df["snapshot_at"] >= cutoff]
        if len(snap_24h) > 1:
            st.subheader("Last 24 Hours")
            fig24 = go.Figure()
            fig24.add_trace(go.Scatter(
                x=snap_24h["snapshot_at"],
                y=snap_24h["total_value"],
                mode="lines",
                line=dict(color="#ff7f0e", width=2),
            ))
            fig24.update_layout(height=200, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig24, use_container_width=True)
    else:
        st.info("No portfolio history yet — snapshots are written every poll cycle.")

    st.caption(
        f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )


# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — Followed whales
# ════════════════════════════════════════════════════════════════════════════════
with tab_whales:
    st.header("Whale Tracker")
    st.info(f"{phase_label}  |  {games} / {PHASE_THRESHOLD} games completed")

    wallets = db.get_all_wallets()

    if not wallets:
        st.info("No whale wallets tracked yet — data collection in progress.")
    else:
        followed = [w for w in wallets if w["status"] == "followed"]
        watching = [w for w in wallets if w["status"] == "watching"]
        demoted = [w for w in wallets if w["status"] == "demoted"]

        ca, cb, cc, cd = st.columns(4)
        ca.metric("Total Wallets", len(wallets))
        cb.metric("Followed", len(followed), help="win rate ≥ threshold and ≥ min trades")
        cc.metric("Watching", len(watching))
        cd.metric("Demoted", len(demoted))

        st.divider()

        with st.expander("Filters & Sorting", expanded=True):
            fcol1, fcol2, fcol3, fcol4 = st.columns(4)
            with fcol1:
                filter_status = st.selectbox(
                    "Status", ["All", "followed", "watching", "demoted"], key="whale_status"
                )
            with fcol2:
                min_win_rate = st.slider(
                    "Min Win Rate", 0.0, 1.0, 0.0, step=0.05, key="whale_min_wr"
                )
            with fcol3:
                min_trade_count = st.number_input(
                    "Min Trades", min_value=0, max_value=100, value=0, key="whale_min_tc"
                )
            with fcol4:
                sort_by = st.selectbox(
                    "Sort By", ["Win Rate", "Trade Count", "ROI"], key="whale_sort"
                )

        display = wallets
        if filter_status != "All":
            display = [w for w in display if w["status"] == filter_status]
        display = [w for w in display if w["win_rate"] >= min_win_rate]
        display = [w for w in display if w["trade_count"] >= min_trade_count]
        sort_key = {"Win Rate": "win_rate", "Trade Count": "trade_count", "ROI": "roi"}[sort_by]
        display = sorted(display, key=lambda w: w.get(sort_key) or 0.0, reverse=True)

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
            styled = df.style.map(_pnl_cell_style, subset=["Total P&L"])
            st.dataframe(styled, use_container_width=True, hide_index=True)
            st.markdown(
                "> **FOLLOWED** = copying trades | "
                "**WATCHING** = monitoring | "
                "**DEMOTED** = dropped below threshold"
            )
        else:
            st.info("No wallets match the current filters.")


# ════════════════════════════════════════════════════════════════════════════════
# TAB 3 — Open positions
# ════════════════════════════════════════════════════════════════════════════════
with tab_open:
    st.header("Open Positions")
    st.info(f"{phase_label}  |  {games} / {PHASE_THRESHOLD} games completed")

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
        styled = df.style.map(_pnl_cell_style, subset=["Unrealised P&L"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

        total_unr = sum(
            p["token_amount"] * (p.get("current_price") or p["entry_price"]) - p["size_usd"]
            for p in open_pos
        )
        st.metric("Total Unrealised P&L", fmt_usd(total_unr))


# ════════════════════════════════════════════════════════════════════════════════
# TAB 4 — Closed positions
# ════════════════════════════════════════════════════════════════════════════════
with tab_closed:
    st.header("Closed Positions")
    st.info(f"{phase_label}  |  {games} / {PHASE_THRESHOLD} games completed")

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
        styled = df.style.map(_pnl_cell_style, subset=["Realised P&L"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

        tot = sum((p.get("realised_pnl") or 0.0) for p in closed_pos)
        wins = sum(1 for p in closed_pos if p["result"] == "win")
        losses = sum(1 for p in closed_pos if p["result"] == "loss")

        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("Realised P&L", fmt_usd(tot))
        sc2.metric("Wins", wins)
        sc3.metric("Losses", losses)


# ════════════════════════════════════════════════════════════════════════════════
# TAB 5 — Manual override
# ════════════════════════════════════════════════════════════════════════════════
with tab_override:
    st.header("Manual Position Top-Up")
    st.info(f"{phase_label}  |  {games} / {PHASE_THRESHOLD} games completed")
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


# ════════════════════════════════════════════════════════════════════════════════
# TAB 6 — Settings
# ════════════════════════════════════════════════════════════════════════════════
with tab_settings:
    st.header("Bot Settings")
    st.info(f"{phase_label}  |  {games} / {PHASE_THRESHOLD} games completed")
    st.warning(
        "Saving writes the new values to `.env` and restarts the `polymarket-bot` "
        "systemd service. Ensure the service is configured before using this."
    )

    s1, s2 = st.columns(2)
    with s1:
        new_win_rate = st.slider(
            "WIN_RATE_THRESHOLD",
            min_value=0.50, max_value=0.95, step=0.05,
            value=float(WIN_RATE_THRESHOLD), format="%.2f",
            help="Minimum win rate to follow a whale",
        )
        new_game_cap = st.slider(
            "GAME_CAP_PCT",
            min_value=0.01, max_value=0.15, step=0.01,
            value=float(GAME_CAP_PCT), format="%.2f",
            help="Max fraction of portfolio per game",
        )
    with s2:
        new_min_trades = st.number_input(
            "MIN_TRADES_ELIGIBLE",
            min_value=3, max_value=20, value=int(MIN_TRADES_ELIGIBLE),
            help="Min WC trades before a wallet is eligible",
        )
        new_min_size = st.number_input(
            "MIN_TRADE_SIZE_USD",
            min_value=1000, max_value=50000, step=500,
            value=int(MIN_TRADE_SIZE_USD),
            help="Only track whale trades >= this value (USD)",
        )
        new_phase_threshold = st.number_input(
            "PHASE_THRESHOLD",
            min_value=1, max_value=48, value=int(PHASE_THRESHOLD),
            help="Switch from collection to live trading after this many games",
        )

    st.divider()

    if st.button("💾 Save & Apply", type="primary"):
        try:
            _write_env_key("WIN_RATE_THRESHOLD", f"{new_win_rate:.2f}")
            _write_env_key("GAME_CAP_PCT", f"{new_game_cap:.2f}")
            _write_env_key("MIN_TRADES_ELIGIBLE", str(int(new_min_trades)))
            _write_env_key("MIN_TRADE_SIZE_USD", str(int(new_min_size)))
            _write_env_key("PHASE_THRESHOLD", str(int(new_phase_threshold)))

            result = subprocess.run(
                ["sudo", "systemctl", "restart", "polymarket-bot"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                st.success("✅ Settings saved and bot restarted")
            else:
                st.success("✅ Settings saved to .env")
                st.warning(
                    f"Bot restart exited with code {result.returncode}. "
                    f"Restart manually if needed. ({result.stderr.strip()})"
                )
        except Exception as exc:
            st.error(f"Error saving settings: {exc}")


# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"Polymarket WC Bot  •  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    f"  •  DB: {db.DB_PATH.name}"
)
