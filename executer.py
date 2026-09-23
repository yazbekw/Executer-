"""
Binance Futures Executor
Places real orders in LIVE mode; simulates in PAPER mode.
"""

import logging
import time
from typing import Optional, Dict, Tuple

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    PAPER_TRADING, LEVERAGE,
)

logger = logging.getLogger(__name__)

_exchange = None


def get_exchange():
    """Lazy-init authenticated Binance Futures client."""
    global _exchange
    if _exchange is None:
        import ccxt
        opts = {
            'apiKey': BINANCE_API_KEY,
            'secret': BINANCE_API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
            },
        }
        _exchange = ccxt.binanceusdm(opts)
        if BINANCE_TESTNET:
            _exchange.set_sandbox_mode(True)
            logger.info("Executor: Binance Futures TESTNET")
        else:
            logger.info("Executor: Binance Futures LIVE")
    return _exchange


def _to_symbol(symbol: str) -> str:
    return symbol.replace('/', '').upper()


# ======================================================================
# Price fetching (public — no keys, no ban risk)
# ======================================================================
_public = None


def _get_public():
    global _public
    if _public is None:
        import ccxt
        _public = ccxt.binanceusdm({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'},
        })
    return _public


def get_current_price(symbol: str) -> float:
    """Public mark price — safe to call frequently."""
    ex = _get_public()
    t = ex.fetch_ticker(_to_symbol(symbol))
    price = float(t.get('last') or t.get('close') or 0)
    if price <= 0:
        raise ValueError(f"Invalid price for {symbol}")
    return price


def get_market_info(symbol: str) -> Dict:
    ex = get_exchange() if not PAPER_TRADING else _get_public()
    markets = ex.load_markets()
    bs = _to_symbol(symbol)
    if bs in markets:
        return markets[bs]
    if symbol in markets:
        return markets[symbol]
    raise ValueError(f"Symbol {symbol} not found")


def _round_qty(qty: float, market: Dict) -> float:
    try:
        step = float(market.get('precision', {}).get('amount') or 0.001)
        if step <= 0:
            step = 0.001
        result = round(qty / step) * step
        s = f"{step:.10f}".rstrip('0')
        decimals = len(s.split('.')[1]) if '.' in s else 0
        return float(f"{result:.{decimals}f}")
    except Exception:
        return float(qty)


def _round_price(price: float, market: Dict) -> float:
    try:
        step = float(market.get('precision', {}).get('price') or 0.01)
        if step <= 0:
            step = 0.01
        result = round(price / step) * step
        s = f"{step:.10f}".rstrip('0')
        decimals = len(s.split('.')[1]) if '.' in s else 0
        return float(f"{result:.{decimals}f}")
    except Exception:
        return float(price)


def calculate_quantity(symbol: str, margin_usd: float,
                       leverage: int, entry_price: float) -> float:
    notional = margin_usd * leverage
    qty = notional / entry_price
    try:
        market = get_market_info(symbol)
        qty = _round_qty(qty, market)
        min_qty = market.get('limits', {}).get('amount', {}).get('min')
        if min_qty and qty < float(min_qty):
            raise ValueError(
                f"Quantity {qty} below exchange min {min_qty}. Increase POSITION_SIZE_USD."
            )
    except Exception as e:
        logger.warning(f"calculate_quantity fallback: {e}")
    return qty


def set_leverage(symbol: str, leverage: int) -> bool:
    if PAPER_TRADING:
        return True
    try:
        ex = get_exchange()
        ex.set_leverage(leverage, _to_symbol(symbol))
        return True
    except Exception as e:
        logger.warning(f"set_leverage failed {symbol}: {e}")
        return False


# ======================================================================
# Open position
# ======================================================================
def open_position(symbol: str, side: str, margin_usd: float,
                  leverage: int, stop_loss: Optional[float],
                  take_profit: Optional[float]) -> Tuple[Dict, float, float, float]:
    """Returns (order, entry_price, quantity, notional)."""
    entry_price = get_current_price(symbol)
    qty = calculate_quantity(symbol, margin_usd, leverage, entry_price)
    notional = qty * entry_price

    set_leverage(symbol, leverage)

    if PAPER_TRADING:
        logger.info(
            f"[PAPER] {side.upper()} {symbol}: qty={qty}, entry~{entry_price}, "
            f"notional=${notional:.2f}, SL={stop_loss}, TP={take_profit}"
        )
        return (
            {'id': f'paper_{int(time.time())}', 'status': 'filled', 'paper': True},
            entry_price, qty, notional,
        )

    ex = get_exchange()
    bs = _to_symbol(symbol)
    order_side = 'buy' if side == 'long' else 'sell'
    order = ex.create_order(bs, 'market', order_side, qty)
    filled_price = float(order.get('average') or order.get('price') or entry_price)

    sl_id = ''
    tp_id = ''
    market = get_market_info(symbol)

    if stop_loss and stop_loss > 0:
        try:
            sl_order = ex.create_order(
                bs, 'STOP_MARKET',
                'sell' if side == 'long' else 'buy',
                qty,
                params={
                    'stopPrice': _round_price(stop_loss, market),
                    'reduceOnly': True,
                    'workingType': 'MARK_PRICE',
                },
            )
            sl_id = sl_order.get('id', '')
        except Exception as e:
            logger.error(f"SL place failed: {e}")

    if take_profit and take_profit > 0:
        try:
            tp_order = ex.create_order(
                bs, 'TAKE_PROFIT_MARKET',
                'sell' if side == 'long' else 'buy',
                qty,
                params={
                    'stopPrice': _round_price(take_profit, market),
                    'reduceOnly': True,
                    'workingType': 'MARK_PRICE',
                },
            )
            tp_id = tp_order.get('id', '')
        except Exception as e:
            logger.error(f"TP place failed: {e}")

    order['sl_order_id'] = sl_id
    order['tp_order_id'] = tp_id
    return order, filled_price, qty, notional


# ======================================================================
# Close position
# ======================================================================
def close_position(symbol: str, side: str, quantity: float) -> Tuple[Dict, float]:
    exit_price = get_current_price(symbol)

    if PAPER_TRADING:
        logger.info(f"[PAPER] CLOSE {side.upper()} {symbol}: qty={quantity} @ {exit_price}")
        return ({'id': f'paper_close_{int(time.time())}', 'status': 'filled'}, exit_price)

    ex = get_exchange()
    bs = _to_symbol(symbol)

    # Cancel any open SL/TP orders first
    try:
        for o in ex.fetch_open_orders(bs):
            try:
                ex.cancel_order(o['id'], bs)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Cancel orders failed: {e}")

    order_side = 'sell' if side == 'long' else 'buy'
    order = ex.create_order(bs, 'market', order_side, quantity, params={'reduceOnly': True})
    filled = float(order.get('average') or order.get('price') or exit_price)
    return order, filled


def compute_pnl(side: str, entry: float, exit_price: float,
                quantity: float) -> Tuple[float, float]:
    if side == 'long':
        pnl_usd = (exit_price - entry) * quantity
    else:
        pnl_usd = (entry - exit_price) * quantity
    notional = entry * quantity
    pnl_pct = (pnl_usd / notional * 100.0) if notional > 0 else 0.0
    return pnl_usd, pnl_pct
