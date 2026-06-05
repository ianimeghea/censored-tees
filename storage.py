import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "store.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_orders (
    stripe_session_id    TEXT PRIMARY KEY,
    payload              TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('pending','submitting','submitted','failed')),
    printify_order_id    TEXT,
    error                TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_orders_status ON pending_orders(status);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def transaction():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def _now():
    return datetime.utcnow().isoformat(timespec="seconds")


def save_pending(stripe_session_id, payload):
    """Persist a Printify order payload that should be submitted after Stripe payment."""
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO pending_orders (stripe_session_id, payload, status, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            ON CONFLICT(stripe_session_id) DO NOTHING
            """,
            (stripe_session_id, json.dumps(payload), _now(), _now()),
        )


def get_order(stripe_session_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_orders WHERE stripe_session_id = ?",
            (stripe_session_id,),
        ).fetchone()
    return dict(row) if row else None


def claim_order(stripe_session_id):
    """Atomically transition pending → submitting. Returns the payload if we claimed it,
    or None if already claimed/submitted/failed by another worker. Caller must follow up
    with mark_submitted() or mark_failed()."""
    with transaction() as conn:
        cur = conn.execute(
            """
            UPDATE pending_orders
               SET status = 'submitting', updated_at = ?
             WHERE stripe_session_id = ? AND status = 'pending'
            """,
            (_now(), stripe_session_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT payload FROM pending_orders WHERE stripe_session_id = ?",
            (stripe_session_id,),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def mark_submitted(stripe_session_id, printify_order_id):
    with transaction() as conn:
        conn.execute(
            """
            UPDATE pending_orders
               SET status = 'submitted', printify_order_id = ?, updated_at = ?
             WHERE stripe_session_id = ?
            """,
            (printify_order_id, _now(), stripe_session_id),
        )


def mark_failed(stripe_session_id, error):
    with transaction() as conn:
        conn.execute(
            """
            UPDATE pending_orders
               SET status = 'failed', error = ?, updated_at = ?
             WHERE stripe_session_id = ?
            """,
            (str(error)[:1000], _now(), stripe_session_id),
        )


def list_orders(limit=200):
    """All orders, newest first. Used by the admin log."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT stripe_session_id, payload, status, printify_order_id, error,
                   created_at, updated_at
              FROM pending_orders
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def reset_to_pending(stripe_session_id):
    """Used if a 'submitting' claim never finished (e.g. crashed worker).
    Allows a retry on the next webhook redelivery or admin action."""
    with transaction() as conn:
        conn.execute(
            """
            UPDATE pending_orders
               SET status = 'pending', updated_at = ?
             WHERE stripe_session_id = ? AND status = 'submitting'
            """,
            (_now(), stripe_session_id),
        )


def force_reset_to_pending(stripe_session_id):
    """Force any non-submitted row back to pending. Used by admin retry."""
    with transaction() as conn:
        conn.execute(
            """
            UPDATE pending_orders
               SET status = 'pending', error = NULL, updated_at = ?
             WHERE stripe_session_id = ? AND status != 'submitted'
            """,
            (_now(), stripe_session_id),
        )
