import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.resolve()
load_dotenv(BASE_DIR / ".env")

# Telegram
TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# Polymarket CLOB credentials
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")
POLY_CHAIN_ID: int = int(os.getenv("POLY_CHAIN_ID", "137"))  # Polygon mainnet

# Bot behaviour
STARTING_CAPITAL: float = float(os.getenv("STARTING_CAPITAL", "10000"))
MIN_TRADE_SIZE_USD: float = float(os.getenv("MIN_TRADE_SIZE_USD", "5000"))
WIN_RATE_THRESHOLD: float = float(os.getenv("WIN_RATE_THRESHOLD", "0.80"))
GAME_CAP_PCT: float = float(os.getenv("GAME_CAP_PCT", "0.07"))
MIN_TRADES_ELIGIBLE: int = int(os.getenv("MIN_TRADES_ELIGIBLE", "5"))
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
PHASE_THRESHOLD: int = int(os.getenv("PHASE_THRESHOLD", "12"))

# Safety
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

# File paths (relative to project root)
DB_PATH = BASE_DIR / "whales.db"
LOG_PATH = BASE_DIR / "bot.log"

# API
CLOB_BASE_URL: str = os.getenv("CLOB_BASE_URL", "https://clob.polymarket.com")
WC_KEYWORDS: list = ["World Cup", "FIFA", "2026"]
