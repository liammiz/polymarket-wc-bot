#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh — One-shot setup for Polymarket WC Bot on a fresh Ubuntu 22.04 VPS
#
# Usage (after cloning the repo to /home/ubuntu/polymarket-wc-bot):
#   chmod +x setup.sh
#   ./setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJ_DIR="/home/ubuntu/polymarket-wc-bot"
VENV_DIR="${PROJ_DIR}/venv"
PYTHON="python3.11"

echo "════════════════════════════════════════"
echo "  Polymarket WC Bot — VPS Setup"
echo "════════════════════════════════════════"

# 1. System packages
echo "[1/6] Updating system packages…"
sudo apt-get update -qq
sudo apt-get upgrade -y -qq
sudo apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    python3-pip git curl build-essential libssl-dev

echo "[2/6] Verifying Python 3.11…"
${PYTHON} --version

# 2. Virtual environment
echo "[3/6] Creating virtual environment at ${VENV_DIR}…"
cd "${PROJ_DIR}"
${PYTHON} -m venv "${VENV_DIR}"

echo "[4/6] Installing Python dependencies…"
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install -r requirements.txt --quiet

# 3. .env check
if [ ! -f "${PROJ_DIR}/.env" ]; then
    echo ""
    echo "  ⚠️  No .env file found!"
    echo "  Copy and fill in your credentials:"
    echo "      cp ${PROJ_DIR}/.env.example ${PROJ_DIR}/.env"
    echo "      nano ${PROJ_DIR}/.env"
    echo ""
fi

# 4. systemd services
echo "[5/6] Installing systemd services…"
sudo cp "${PROJ_DIR}/polymarket-bot.service"       /etc/systemd/system/
sudo cp "${PROJ_DIR}/polymarket-dashboard.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot
sudo systemctl enable polymarket-dashboard

# 5. Start services
echo "[6/6] Starting services…"
sudo systemctl restart polymarket-bot
sudo systemctl restart polymarket-dashboard

# 6. Firewall: open dashboard port (ufw — skip if not active)
if sudo ufw status | grep -q "Status: active"; then
    echo "Opening port 8501 in ufw…"
    sudo ufw allow 8501/tcp
    sudo ufw reload
fi

# Done
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "<server-ip>")
echo ""
echo "════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Bot service:       sudo systemctl status polymarket-bot"
echo "  Dashboard service: sudo systemctl status polymarket-dashboard"
echo "  Live logs:         sudo journalctl -u polymarket-bot -f"
echo "                     tail -f ${PROJ_DIR}/bot.log"
echo ""
echo "  Dashboard URL:     http://${SERVER_IP}:8501"
echo "════════════════════════════════════════"
