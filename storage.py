import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_orders (
    payment_id           TEXT PRIMARY KEY,
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
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def init_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Add your Supabase connection string to .env.")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(cur):
    """Convert a single fetchone() result to a dict using cursor.description."""
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _rows_to_dicts(cur):
    """Convert all fetchall() results to a list of dicts."""
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in rows]


def save_pending(payment_id, payload):
    """Persist a Printify order payload that should be submitted after payment."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pending_orders (payment_id, payload, status, created_at, updated_at)
                VALUES (%s, %s, 'pending', %s, %s)
                ON CONFLICT (payment_id) DO NOTHING
                """,
                (payment_id, json.dumps(payload), _now(), _now()),
            )


def get_order(payment_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pending_orders WHERE payment_id = %s",
                (payment_id,),
            )
            return _row_to_dict(cur)
    finally:
        conn.close()


def claim_order(payment_id):
    """Atomically transition pending -> submitting. Returns the payload if we claimed it,
    or None if already claimed/submitted/failed by another worker."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_orders
                   SET status = 'submitting', updated_at = %s
                 WHERE payment_id = %s AND status = 'pending'
                """,
                (_now(), payment_id),
            )
            if cur.rowcount == 0:
                return None
            cur.execute(
                "SELECT payload FROM pending_orders WHERE payment_id = %s",
                (payment_id,),
            )
            row = cur.fetchone()
    return json.loads(row[0]) if row else None


def mark_submitted(payment_id, printify_order_id):
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_orders
                   SET status = 'submitted', printify_order_id = %s, updated_at = %s
                 WHERE payment_id = %s
                """,
                (printify_order_id, _now(), payment_id),
            )


def mark_failed(payment_id, error):
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_orders
                   SET status = 'failed', error = %s, updated_at = %s
                 WHERE payment_id = %s
                """,
                (str(error)[:1000], _now(), payment_id),
            )


def list_orders(limit=200):
    """All orders, newest first. Used by the admin log."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payment_id, payload, status, printify_order_id, error,
                       created_at, updated_at
                  FROM pending_orders
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
            return _rows_to_dicts(cur)
    finally:
        conn.close()


def reset_to_pending(payment_id):
    """Used if a 'submitting' claim never finished (e.g. crashed worker)."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_orders
                   SET status = 'pending', updated_at = %s
                 WHERE payment_id = %s AND status = 'submitting'
                """,
                (_now(), payment_id),
            )


def force_reset_to_pending(payment_id):
    """Force any non-submitted row back to pending. Used by admin retry."""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_orders
                   SET status = 'pending', error = NULL, updated_at = %s
                 WHERE payment_id = %s AND status != 'submitted'
                """,
                (_now(), payment_id),
            )
