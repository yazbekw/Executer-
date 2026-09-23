"""
NTFY notifier — English messages, no emojis.
"""

import logging
from datetime import datetime

import requests
from config import NTFY_URL

logger = logging.getLogger(__name__)


def _send(message: str, title: str,
          priority: str = '3', tags: str = 'robot') -> bool:
    try:
        headers = {
            'Title': title,
            'Priority': priority,
            'Tags': tags,
            'Content-Type': 'text/plain; charset=utf-8',
        }
        safe = message.encode('ascii', errors='replace').decode('ascii')
        r = requests.post(NTFY_URL, data=safe.encode('utf-8'),
                          headers=headers, timeout=8)
        return 200 <= r.status_code < 300
    except Exception as e:
        logger.error(f"NTFY error: {e}")
        return False


def notify_entry(symbol: str, side: str, entry_price: float,
                 quantity: float, notional_usd: float, margin_usd: float,
                 leverage: int, stop_loss: float, take_profit: float,
                 signal_type: str, confidence: float,
                 paper: bool = False, mode: str = 'ENTRY') -> bool:
    direction = 'LONG' if side == 'long' else 'SHORT'
    mode_str = 'PAPER' if paper else 'LIVE'
    priority = '4' if signal_type.startswith('STRONG') else '3'

    sl_pct = ((stop_loss - entry_price) / entry_price * 100.0) if stop_loss else 0.0
    tp_pct = ((take_profit - entry_price) / entry_price * 100.0) if take_profit else 0.0

    lines = [
        f"{mode} POSITION OPENED: {symbol}",
        f"Mode: {mode_str}",
        "",
        f"Direction: {direction}",
        f"Signal: {signal_type} (confidence {confidence:.1f}%)",
        "",
        f"Entry Price: {entry_price}",
        f"Quantity: {quantity}",
        f"Notional: ${notional_usd:.2f}",
        f"Margin: ${margin_usd:.2f} at {leverage}x",
        "",
        f"Stop Loss: {stop_loss} ({sl_pct:+.2f}%)",
        f"Take Profit: {take_profit} ({tp_pct:+.2f}%)",
        "",
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
    ]
    return _send('\n'.join(lines), f"{mode} {direction} {symbol}",
                 priority,
                 'heavy_check_mark' if side == 'long' else 'arrow_down')


def notify_exit(symbol: str, side: str, entry_price: float,
                exit_price: float, pnl_usd: float, pnl_pct: float,
                reason: str, paper: bool = False,
                duration_min: float = 0.0) -> bool:
    direction = 'LONG' if side == 'long' else 'SHORT'
    mode_str = 'PAPER' if paper else 'LIVE'
    status = 'PROFIT' if pnl_usd >= 0 else 'LOSS'
    priority = '4' if pnl_usd >= 0 else '3'
    tag = 'moneybag' if pnl_usd >= 0 else 'warning'

    lines = [
        f"{mode} POSITION CLOSED: {symbol}",
        f"Mode: {mode_str}",
        f"Status: {status}",
        "",
        f"Direction: {direction}",
        f"Entry: {entry_price}",
        f"Exit: {exit_price}",
        "",
        f"PnL: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)",
        f"Reason: {reason}",
        f"Duration: {duration_min:.1f} min",
        "",
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
    ]
    return _send('\n'.join(lines), f"{mode} {status} {symbol}", priority, tag)


def notify_rejected(symbol: str, reason: str, signal_type: str = '') -> bool:
    lines = [
        f"SIGNAL REJECTED: {symbol}",
        f"Signal: {signal_type}",
        f"Reason: {reason}",
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
    ]
    return _send('\n'.join(lines), f"REJECTED {symbol}", '2', 'no_entry')


def notify_system(message: str, title: str = 'Execution Bot') -> bool:
    return _send(message, title, '3', 'gear')
