# Polymarket World Cup 2026 Copy-Trading Bot

Automated two-phase copy-trading system for Polymarket World Cup 2026 markets.

---

## Architecture

```
poller.py      ← background service: polls CLOB API, scores wallets, executes orders
dashboard.py   ← Streamlit UI: reads whales.db, provides manual override
whales.db      ← shared SQLite database (WAL mode)
bot.log        ← rotating log file
```

### Phase 1 — Data collection (games 1–12)
Polls every 30 s, stores all whale trades ≥ $5,000. No orders placed.

### Phase 2 — Live trading (after game 12)
Mirrors followed-whale trades. Position sized by Kelly Criterion capped at 7% per game.

---

## Quick start (local / testing)

```bash
git clone <repo> polymarket-wc-bot
cd polymarket-wc-bot

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env
# edit .env — set credentials, keep DRY_RUN=true

python poller.py                  # terminal 1 — background poller
streamlit run dashboard.py        # terminal 2 — dashboard at http://localhost:8501
```

---

## VPS deployment (Hetzner Ubuntu 22.04)

### 1. Clone the repo

```bash
ssh ubuntu@<server-ip>
git clone <repo> /home/ubuntu/polymarket-wc-bot
cd /home/ubuntu/polymarket-wc-bot
cp .env.example .env
nano .env          # fill in all credentials
```

### 2. Run setup

```bash
chmod +x setup.sh
./setup.sh
```

This single script:
- Updates apt and installs Python 3.11
- Creates a virtualenv and installs `requirements.txt`
- Installs and enables both systemd services
- Starts the bot and dashboard automatically
- Opens port 8501 in ufw (if ufw is active)

### 3. Access the dashboard

```
http://<server-ip>:8501
```

---

## Opening port 8501 on the Hetzner firewall

Hetzner has two independent firewall layers. You need to open port 8501 in **both**:

### A — Hetzner Cloud Firewall (applies to the server in the cloud console)

1. Go to [console.hetzner.cloud](https://console.hetzner.cloud)
2. Select your project → **Firewalls** → select your firewall (or create one)
3. Click **Add rule** → **Inbound**
   - Protocol: **TCP**
   - Port: **8501**
   - Sources: `0.0.0.0/0, ::/0` (or restrict to your IP for security)
4. Apply the firewall to your server if not already assigned

### B — Host firewall (ufw, on the server itself)

`setup.sh` runs this automatically if ufw is active. To do it manually:

```bash
sudo ufw allow 8501/tcp
sudo ufw reload
sudo ufw status
```

> **Security tip**: restrict access to your own IP instead of `0.0.0.0/0`:
> ```bash
> sudo ufw allow from <your-ip> to any port 8501
> ```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | — | Your chat/group ID |
| `POLY_PRIVATE_KEY` | — | Polygon wallet private key |
| `POLY_API_KEY` | — | Polymarket API key |
| `POLY_API_SECRET` | — | Polymarket API secret |
| `POLY_API_PASSPHRASE` | — | Polymarket API passphrase |
| `STARTING_CAPITAL` | 10000 | USD bankroll for Kelly sizing |
| `MIN_TRADE_SIZE_USD` | 5000 | Minimum whale trade to track |
| `WIN_RATE_THRESHOLD` | 0.80 | Min win rate to follow a wallet |
| `GAME_CAP_PCT` | 0.07 | Max % of portfolio per game |
| `MIN_TRADES_ELIGIBLE` | 5 | Min WC trades before eligible |
| `POLL_INTERVAL_SECONDS` | 30 | API polling interval |
| `PHASE_THRESHOLD` | 12 | Games before Phase 2 activates |
| `DRY_RUN` | true | **false** = real orders; always start with true |

---

## Useful commands on the VPS

```bash
# Service management
sudo systemctl status polymarket-bot
sudo systemctl status polymarket-dashboard
sudo systemctl restart polymarket-bot

# Live logs
sudo journalctl -u polymarket-bot -f --no-pager
tail -f /home/ubuntu/polymarket-wc-bot/bot.log

# Check phase
sqlite3 /home/ubuntu/polymarket-wc-bot/whales.db \
    "SELECT games_completed FROM phase_tracking;"

# Manual games_completed bump (testing Phase 2)
sqlite3 /home/ubuntu/polymarket-wc-bot/whales.db \
    "UPDATE phase_tracking SET games_completed=12;"
```

---

## Wallet scoring rules

| Condition | Status |
|---|---|
| < 5 WC trades | `watching` |
| ≥ 5 trades, win rate ≥ 80% | `followed` — trades are copied |
| Was followed, win rate drops < 80% | `demoted` — copying stops immediately |

Scores are recalculated after every newly resolved market.

## Kelly Criterion implementation

Half-Kelly is used (50% of full Kelly) to reduce variance:

```
b  = (1 - price) / price      # net odds in USDC
f* = (b × win_rate − loss_rate) / b
position_size = portfolio_value × (f* × 0.5)
```

capped at 25% of bankroll per position and further capped by the 7% per-game exposure limit.
