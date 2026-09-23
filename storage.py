"""
Execution Bot — Storage (PostgreSQL via Supabase)
Handles positions, daily stats, webhook log.
"""

import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from threading import Lock
from contextlib import contextmanager

from config import DATABASE_URL, USE_POSTGRES

logger = logging.getLogger(__name__)


# ======================================================================
# PostgreSQL setup
# ======================================================================
_psycopg2 = None
_pool = None
_pool_lock = Lock()


if USE_POSTGRES:
    try:
        import psycopg2
        import psycopg2.extras
        from psycopg2 import pool as pg_pool
        _psycopg2 = psycopg2
        logger.info("Storage: PostgreSQL (Supabase)")
    except BaseException as e:
        logger.error(f"psycopg2 import failed: {e}")
        raise


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = pg_pool.SimpleConnectionPool(
                    minconn=1, maxconn=5,
                    dsn=DATABASE_URL, connect_timeout=10,
                )
                logger.info("Storage: PostgreSQL pool created")
    return _pool


class _PgConn:
    """Wrapper to make psycopg2 look like sqlite3 for our patterns."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()):
        sql = sql.replace('?', '%s')
        cur = self._conn.cursor(cursor_factory=_psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def executescript(self, script: str):
        cur = self._conn.cursor()
        for stmt in script.split(';'):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass


@contextmanager
def _conn():
    p = _get_pool()
    conn = p.getconn()
    try:
        yield _PgConn(conn)
        try:
            conn.commit()
        except Exception:
            pass
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            p.putconn(conn)
        except Exception:
            pass


# ======================================================================
# DDL
# ======================================================================
def init_db():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_price DOUBLE PRECISION NOT NULL,
                quantity DOUBLE PRECISION NOT NULL,
                notional_usd DOUBLE PRECISION NOT NULL,
                leverage INTEGER NOT NULL,
                margin_usd DOUBLE PRECISION NOT NULL,
                stop_loss DOUBLE PRECISION,
                take_profit DOUBLE PRECISION,
                signal_type TEXT,
                signal_score DOUBLE PRECISION,
                signal_confidence DOUBLE PRECISION,
                signal_id INTEGER,
                exit_price DOUBLE PRECISION,
                exit_reason TEXT,
                pnl_usd DOUBLE PRECISION,
                pnl_pct DOUBLE PRECISION,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                exchange_order_id TEXT,
                sl_order_id TEXT,
                tp_order_id TEXT,
                paper INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                trades_opened INTEGER DEFAULT 0,
                trades_closed INTEGER DEFAULT 0,
                realized_pnl DOUBLE PRECISION DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS webhook_log (
                id SERIAL PRIMARY KEY,
                received_at TEXT NOT NULL,
                event TEXT,
                symbol TEXT,
                payload TEXT,
                action TEXT,
                reason TEXT
            );
        """)
    logger.info("Storage initialized")


# ======================================================================
# PositionStore
# ======================================================================
class PositionStore:
    _lock = Lock()

    @staticmethod
    def open_position(symbol: str, side: str, entry_price: float,
                      quantity: float, notional_usd: float,
                      leverage: int, margin_usd: float,
                      stop_loss: Optional[float],
                      take_profit: Optional[float],
                      signal_type: str, signal_score: float,
                      signal_confidence: float,
                      signal_id: Optional[int],
                      exchange_order_id: str = '',
                      sl_order_id: str = '',
                      tp_order_id: str = '',
                      paper: bool = True) -> int:
        with PositionStore._lock, _conn() as c:
            cur = c.execute("""
                INSERT INTO positions
                    (symbol, side, status, entry_price, quantity, notional_usd,
                     leverage, margin_usd, stop_loss, take_profit,
                     signal_type, signal_score, signal_confidence, signal_id,
                     opened_at, exchange_order_id, sl_order_id, tp_order_id, paper)
                VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
            """, (
                symbol, side, entry_price, quantity, notional_usd,
                leverage, margin_usd, stop_loss, take_profit,
                signal_type, signal_score, signal_confidence, signal_id,
                datetime.now().isoformat(),
                exchange_order_id, sl_order_id, tp_order_id,
                1 if paper else 0,
            ))
            row = cur.fetchone()
            return int(row['id'])

    @staticmethod
    def close_position(position_id: int, exit_price: float,
                       exit_reason: str, pnl_usd: float, pnl_pct: float):
        with PositionStore._lock, _conn() as c:
            c.execute("""
                UPDATE positions
                SET status='closed', exit_price=?, exit_reason=?,
                    pnl_usd=?, pnl_pct=?, closed_at=?
                WHERE id=?
            """, (exit_price, exit_reason, pnl_usd, pnl_pct,
                  datetime.now().isoformat(), position_id))

    @staticmethod
    def get_open() -> List[Dict]:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def get_open_count() -> int:
        with _conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS cnt FROM positions WHERE status='open'"
            ).fetchone()
            return int(row['cnt'] or 0)

    @staticmethod
    def get_open_by_symbol(symbol: str) -> Optional[Dict]:
        with _conn() as c:
            row = c.execute(
                "SELECT * FROM positions WHERE status='open' AND symbol=? LIMIT 1",
                (symbol,)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def get_recent(limit: int = 100) -> List[Dict]:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM positions ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def get_today_stats() -> Dict:
        today = datetime.now().strftime('%Y-%m-%d')
        with _conn() as c:
            row = c.execute(
                "SELECT * FROM daily_stats WHERE date=?", (today,)
            ).fetchone()
            if not row:
                return {
                    'date': today, 'trades_opened': 0, 'trades_closed': 0,
                    'realized_pnl': 0.0, 'wins': 0, 'losses': 0,
                }
            return dict(row)

    @staticmethod
    def increment_daily(opened: int = 0, closed: int = 0,
                        pnl_delta: float = 0.0,
                        is_win: Optional[bool] = None):
        today = datetime.now().strftime('%Y-%m-%d')
        with PositionStore._lock, _conn() as c:
            c.execute("""
                INSERT INTO daily_stats
                    (date, trades_opened, trades_closed, realized_pnl, wins, losses)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    trades_opened = daily_stats.trades_opened + EXCLUDED.trades_opened,
                    trades_closed = daily_stats.trades_closed + EXCLUDED.trades_closed,
                    realized_pnl = daily_stats.realized_pnl + EXCLUDED.realized_pnl,
                    wins = daily_stats.wins + EXCLUDED.wins,
                    losses = daily_stats.losses + EXCLUDED.losses
            """, (
                today, opened, closed, pnl_delta,
                1 if is_win is True else 0,
                1 if is_win is False else 0,
            ))


# ======================================================================
# Webhook log
# ======================================================================
def log_webhook(event: str, symbol: str, payload: dict,
                action: str, reason: str = ''):
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO webhook_log
                    (received_at, event, symbol, payload, action, reason)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(), event, symbol,
                json.dumps(payload, default=str), action, reason,
            ))
    except Exception as e:
        logger.debug(f"log_webhook failed: {e}")
