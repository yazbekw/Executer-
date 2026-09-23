"""
Execution Bot — Flask application.
Receives webhook events from the Crypto Signal Analyzer, executes trades.
"""

import json
import hmac
import hashlib
import logging
import threading
from datetime import datetime

from flask import Flask, request, jsonify

from config import (
    SHARED_SECRET, PAPER_TRADING, PORT, LOG_LEVEL,
    LEVERAGE, POSITION_SIZE_USD, validate,
)
from storage import init_db, PositionStore, log_webhook
from position_manager import handle_entry, handle_exit, close_all
from notifier import notify_system
from position_monitor import register_dashboard, start_monitor

# ======================================================================
# Logging
# ======================================================================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ======================================================================
# Flask app
# ======================================================================
app = Flask(__name__)


# ======================================================================
# HMAC verification
# ======================================================================
def verify_signature(body: bytes, provided: str) -> bool:
    if not SHARED_SECRET:
        return False
    expected = hmac.new(
        SHARED_SECRET.encode('utf-8'), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided or '')


# ======================================================================
# Routes
# ======================================================================
@app.route('/')
def index():
    return jsonify({
        'service': 'Execution Bot',
        'version': '1.0',
        'status': 'running',
        'paper_trading': PAPER_TRADING,
        'endpoints': {
            'health': '/api/health',
            'positions': '/api/positions',
            'webhook': 'POST /webhook',
            'close_all': 'POST /api/close_all',
            'dashboard': '/dashboard/',
        },
    })


@app.route('/webhook', methods=['POST'])
def webhook():
    body = request.get_data()
    sig = request.headers.get('X-Signature', '')

    if not verify_signature(body, sig):
        logger.warning("Invalid signature — rejecting")
        return jsonify({'status': 'error', 'message': 'invalid signature'}), 401

    try:
        payload = json.loads(body.decode('utf-8'))
    except Exception as e:
        logger.error(f"Invalid JSON: {e}")
        return jsonify({'status': 'error', 'message': 'invalid json'}), 400

    event = payload.get('event', '')
    symbol = payload.get('symbol', '')
    logger.info(f"Webhook: event={event}, symbol={symbol}")

    if event == 'entry':
        def _bg():
            try:
                result = handle_entry(payload)
                log_webhook(event, symbol, payload,
                            result.get('status', 'unknown'),
                            result.get('reason', ''))
            except Exception as e:
                logger.exception(f"handle_entry failed: {e}")
                log_webhook(event, symbol, payload, 'error', str(e))
        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({'status': 'accepted'}), 202

    if event == 'state_change':
        def _bg():
            try:
                result = handle_exit(payload)
                log_webhook(event, symbol, payload,
                            result.get('status', 'unknown'),
                            result.get('reason', ''))
            except Exception as e:
                logger.exception(f"handle_exit failed: {e}")
                log_webhook(event, symbol, payload, 'error', str(e))
        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({'status': 'accepted'}), 202

    return jsonify({'status': 'ignored', 'event': event}), 200


@app.route('/api/health')
def health():
    return jsonify({
        'status': 'healthy',
        'paper_trading': PAPER_TRADING,
        'leverage': LEVERAGE,
        'position_size_usd': POSITION_SIZE_USD,
        'open_positions': PositionStore.get_open_count(),
        'today': PositionStore.get_today_stats(),
        'timestamp': datetime.now().isoformat(),
    })


@app.route('/api/positions')
def positions():
    return jsonify({
        'open': PositionStore.get_open(),
        'recent': PositionStore.get_recent(20),
        'today': PositionStore.get_today_stats(),
    })


@app.route('/api/close_all', methods=['POST'])
def api_close_all():
    return jsonify(close_all('manual'))


@app.route('/api/test_notify')
def test_notify():
    ok = notify_system('Test notification from execution bot', 'Execution Bot Test')
    return jsonify({'success': ok})


# ======================================================================
# Startup
# ======================================================================
def _startup():
    errors = validate()
    if errors:
        for e in errors:
            logger.error(f"Config error: {e}")
        logger.error("Startup aborted")
        return

    init_db()

    # Register dashboard
    register_dashboard(app)

    # Start SL/TP + auto-close monitor
    start_monitor()

    mode = 'PAPER TRADING' if PAPER_TRADING else 'LIVE TRADING'
    logger.info(f"Execution Bot starting in {mode}")
    logger.info(f"Leverage: {LEVERAGE}x | Position size: ${POSITION_SIZE_USD}")

    try:
        notify_system(
            f"Execution Bot started\n"
            f"Mode: {mode}\n"
            f"Leverage: {LEVERAGE}x\n"
            f"Position size: ${POSITION_SIZE_USD}\n"
            f"Max concurrent: 1",
            'Execution Bot Started'
        )
    except Exception as e:
        logger.error(f"Startup notify failed: {e}")


_startup()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
