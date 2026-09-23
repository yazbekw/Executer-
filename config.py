"""
Execution Bot — Configuration
All values read from environment variables.
"""

import os


def _env(key: str, default: str = '') -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, str(default)).lower() in ('true', '1', 'yes', 'on')


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except Exception:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except Exception:
        return default


# ======================================================================
# Database (separate Supabase project)
# ======================================================================
DATABASE_URL = _env('DATABASE_URL', '').strip()
USE_POSTGRES = DATABASE_URL.startswith(('postgres://', 'postgresql://'))

# ======================================================================
# Binance Futures credentials
# ======================================================================
BINANCE_API_KEY = _env('BINANCE_API_KEY', '')
BINANCE_API_SECRET = _env('BINANCE_API_SECRET', '')
BINANCE_TESTNET = _env_bool('BINANCE_TESTNET', False)

# ======================================================================
# Execution mode
# ======================================================================
PAPER_TRADING = _env_bool('PAPER_TRADING', True)
LEVERAGE = _env_int('LEVERAGE', 20)
POSITION_SIZE_USD = _env_float('POSITION_SIZE_USD', 5.0)
MAX_CONCURRENT_POSITIONS = _env_int('MAX_CONCURRENT_POSITIONS', 1)

# ======================================================================
# Webhook security
# ======================================================================
SHARED_SECRET = _env('EXECUTION_BOT_SECRET', '')

# ======================================================================
# Notifications
# ======================================================================
NTFY_TOPIC = _env('EXECUTION_NTFY_TOPIC', 'crypto_execution_alerts')
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

# ======================================================================
# Safety limits
# ======================================================================
MAX_DAILY_LOSS_USD = _env_float('MAX_DAILY_LOSS_USD', 20.0)
MAX_DAILY_TRADES = _env_int('MAX_DAILY_TRADES', 20)
MIN_CONFIDENCE = _env_float('MIN_CONFIDENCE', 30.0)

# ======================================================================
# SL/TP monitor
# ======================================================================
# Check every N seconds for SL/TP hits (paper + live)
MONITOR_INTERVAL_SEC = _env_int('MONITOR_INTERVAL_SEC', 30)

# Auto-close stale positions (safety net)
AUTO_CLOSE_ENABLED = _env_bool('AUTO_CLOSE_ENABLED', True)
MAX_HOLD_HOURS = _env_float('MAX_HOLD_HOURS', 4.0)
LOSS_CUT_HOURS = _env_float('LOSS_CUT_HOURS', 12.0)
PROFIT_KEEP_HOURS = _env_float('PROFIT_KEEP_HOURS', 24.0)

# Price source for monitor (public Binance, no keys needed)
MONITOR_USE_PUBLIC_PRICE = _env_bool('MONITOR_USE_PUBLIC_PRICE', True)

# ======================================================================
# Server
# ======================================================================
PORT = _env_int('PORT', 5001)
LOG_LEVEL = _env('LOG_LEVEL', 'INFO').upper()


def validate() -> list:
    """Return list of config errors (empty if OK)."""
    errors = []
    if not USE_POSTGRES:
        errors.append('DATABASE_URL is required (execution bot Supabase project)')
    if not PAPER_TRADING:
        if not BINANCE_API_KEY:
            errors.append('BINANCE_API_KEY is required for live trading')
        if not BINANCE_API_SECRET:
            errors.append('BINANCE_API_SECRET is required for live trading')
    if not SHARED_SECRET:
        errors.append('EXECUTION_BOT_SECRET is required for webhook authentication')
    return errors
