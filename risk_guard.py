"""
Risk Guard — decides whether to accept or reject a webhook signal.
"""

import logging
from config import (
    MAX_CONCURRENT_POSITIONS, MAX_DAILY_LOSS_USD,
    MAX_DAILY_TRADES, MIN_CONFIDENCE,
)
from storage import PositionStore

logger = logging.getLogger(__name__)


class RiskDecision:
    def __init__(self, allow: bool, reason: str = ''):
        self.allow = allow
        self.reason = reason


def evaluate(signal: dict) -> RiskDecision:
    confidence = abs(float(signal.get('percentage', 0.0)))
    if confidence < MIN_CONFIDENCE:
        return RiskDecision(False,
                            f"confidence {confidence:.1f}% below minimum {MIN_CONFIDENCE}%")

    open_count = PositionStore.get_open_count()
    if open_count >= MAX_CONCURRENT_POSITIONS:
        return RiskDecision(False,
                            f"already {open_count} open position(s), max is {MAX_CONCURRENT_POSITIONS}")

    symbol = signal.get('symbol', '')
    if not symbol:
        return RiskDecision(False, "symbol missing")

    if PositionStore.get_open_by_symbol(symbol):
        return RiskDecision(False, f"{symbol} already has an open position")

    stats = PositionStore.get_today_stats()
    if float(stats.get('realized_pnl') or 0) <= -MAX_DAILY_LOSS_USD:
        return RiskDecision(False,
                            f"daily loss limit reached ({stats['realized_pnl']:.2f} USD)")

    if int(stats.get('trades_opened') or 0) >= MAX_DAILY_TRADES:
        return RiskDecision(False,
                            f"daily trades limit reached ({stats['trades_opened']})")

    entry = float(signal.get('entry_price', 0.0))
    sl = float(signal.get('stop_loss', 0.0) or 0.0)
    tp = float(signal.get('take_profit', 0.0) or 0.0)
    direction = signal.get('direction', 'long')

    if entry <= 0:
        return RiskDecision(False, "entry_price missing or invalid")

    if sl <= 0:
        return RiskDecision(False, "stop_loss missing (required)")

    if direction == 'long':
        if sl >= entry:
            return RiskDecision(False, "invalid SL for long (SL >= entry)")
        if tp > 0 and tp <= entry:
            return RiskDecision(False, "invalid TP for long (TP <= entry)")
    else:
        if sl <= entry:
            return RiskDecision(False, "invalid SL for short (SL <= entry)")
        if tp > 0 and tp >= entry:
            return RiskDecision(False, "invalid TP for short (TP >= entry)")

    return RiskDecision(True, 'ok')
