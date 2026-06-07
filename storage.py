import json
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

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
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Add your Supabase connection string to .env.")
    with _connect() as conn:
        conn.execute(SCHEMA)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_pending(payment_id, payload):
    """Persist a Printify order payload that should be submitted after payment."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_orders (payment_id, payload, status, created_at, updated_at)
            VALUES (%s, %s, 'pending', %s, %s)
            ON CONFLICT (payment_id) DO NOTHING
            """,
            (payment_id, json.dumps(payload), _now(), _now()),
        )


def get_order(payment_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_orders WHERE payment_id = %s",
            (payment_id,),
        ).fetchone()
    return dict(row) if row else None


def claim_order(payment_id):
    """Atomically transition pending -> submitting. Returns the payload if we claimed it,
    or None if already claimed/submitted/failed by another worker."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE pending_orders
               SET status = 'submitting', updated_at = %s
             WHERE payment_id = %s AND status = 'pending'
            """,
            (_now(), payment_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT payload FROM pending_orders WHERE payment_id = %s",
            (payment_id,),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def mark_submitted(payment_id, printify_order_id):
    with _connect() as conn:
        conn.execute(
            """
            UPDATE pending_orders
               SET status = 'submitted', printify_order_id = %s, updated_at = %s
             WHERE payment_id = %s
            """,
            (printify_order_id, _now(), payment_id),
        )


def mark_failed(payment_id, error):
    with _connect() as conn:
        conn.execute(
            """
            UPDATE pending_orders
               SET status = 'failed', error = %s, updated_at = %s
             WHERE payment_id = %s
            """,
            (str(error)[:1000], _now(), payment_id),
        )


def list_orders(limit=200):
    """All orders, newest first. Used by the admin log."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT payment_id, payload, status, printify_order_id, error,
                   created_at, updated_at
              FROM pending_orders
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def reset_to_pending(payment_id):
    """Used if a 'submitting' claim never finished (e.g. crashed worker)."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE pending_orders
               SET status = 'pending', updated_at = %s
             WHERE payment_id = %s AND status = 'submitting'
            """,
            (_now(), payment_id),
        )


def force_reset_to_pending(payment_id):
    """Force any non-submitted row back to pending. Used by admin retry."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE pending_orders
               SET status = 'pending', error = NULL, updated_at = %s
             WHERE payment_id = %s AND status != 'submitted'
            """,
            (_now(), payment_id),
        )
