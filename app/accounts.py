"""Customer API keys and usage records.

Every function takes the customer ID resolved by the server-side auth layer and scopes its
query to it. Secrets exist only in memory: create_key returns one once and stores its hash.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from app.db import pool

KEY_PREFIX = "uak_"          # "unified algorithm key"
PREFIX_DISPLAY_LEN = 12      # e.g. "uak_7hQx2LmP" -- shown in listings to identify a key
MAX_ACTIVE_KEYS = 50


def hash_key(secret: str) -> bytes:
    # Keys are 256-bit random values, so a fast hash is appropriate (no brute-force surface).
    return hashlib.sha256(secret.encode("utf-8")).digest()


def looks_like_customer_key(value: str) -> bool:
    return value.startswith(KEY_PREFIX) and 40 <= len(value) <= 100


@dataclass(frozen=True)
class KeyOwner:
    customer_id: str
    key_id: uuid.UUID


class TooManyKeys(Exception):
    pass


def _key_row(row) -> Dict[str, Any]:
    key_id, name, prefix, created_at, last_used_at, revoked_at = row
    return {"id": str(key_id), "name": name, "prefix": prefix, "created_at": created_at,
            "last_used_at": last_used_at, "revoked_at": revoked_at,
            "status": "revoked" if revoked_at else "active"}


# -- keys --------------------------------------------------------------------
def create_key(customer_id: str, name: str) -> Dict[str, Any]:
    secret = KEY_PREFIX + secrets.token_urlsafe(32)
    key_id = uuid.uuid4()
    with pool().connection() as conn:
        conn.execute("INSERT INTO customers (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (customer_id,))
        # Lock the customer row so concurrent creates can't exceed the active-key limit.
        conn.execute("SELECT 1 FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
        active = conn.execute("SELECT count(*) FROM api_keys WHERE customer_id = %s AND revoked_at IS NULL",
                              (customer_id,)).fetchone()[0]
        if active >= MAX_ACTIVE_KEYS:
            raise TooManyKeys(f"a customer can have at most {MAX_ACTIVE_KEYS} active keys")
        row = conn.execute(
            "INSERT INTO api_keys (id, customer_id, name, prefix, key_hash) VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id, name, prefix, created_at, last_used_at, revoked_at",
            (key_id, customer_id, name, secret[:PREFIX_DISPLAY_LEN], hash_key(secret))).fetchone()
    return {**_key_row(row), "secret": secret}


def list_keys(customer_id: str) -> List[Dict[str, Any]]:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, name, prefix, created_at, last_used_at, revoked_at FROM api_keys "
            "WHERE customer_id = %s ORDER BY created_at DESC", (customer_id,)).fetchall()
    return [_key_row(r) for r in rows]


def revoke_key(customer_id: str, key_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Revoke one of this customer's keys. Idempotent. None if the customer has no such key."""
    with pool().connection() as conn:
        row = conn.execute(
            "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()) "
            "WHERE id = %s AND customer_id = %s "
            "RETURNING id, name, prefix, created_at, last_used_at, revoked_at",
            (key_id, customer_id)).fetchone()
    return _key_row(row) if row else None


def authenticate_key(secret: str) -> Optional[KeyOwner]:
    """Resolve a presented customer key to its owner; None if unknown or revoked."""
    with pool().connection() as conn:
        row = conn.execute("SELECT customer_id, id FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
                           (hash_key(secret),)).fetchone()
    return KeyOwner(customer_id=row[0], key_id=row[1]) if row else None


# -- usage -------------------------------------------------------------------
def record_usage(owner: KeyOwner, duration_ms: float, results: Sequence[Dict[str, Any]]) -> uuid.UUID:
    """One usage_requests row plus one usage_operations row per task.

    `results` items carry only: operation (a registered name or "unknown"), status,
    error_type and duration_ms. Inputs, outputs, error messages and key material are never stored.
    """
    request_id = uuid.uuid4()
    ok = sum(1 for r in results if r["status"] == "ok")
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO usage_requests (id, customer_id, key_id, task_count, ok_count, error_count, duration_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (request_id, owner.customer_id, owner.key_id, len(results), ok, len(results) - ok, duration_ms))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO usage_operations (request_id, customer_id, key_id, operation, status, error_type, duration_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(request_id, owner.customer_id, owner.key_id, r["operation"], r["status"],
                  r.get("error_type"), r["duration_ms"]) for r in results])
        conn.execute("UPDATE api_keys SET last_used_at = now() WHERE id = %s", (owner.key_id,))
    return request_id


def usage_report(customer_id: str, start: datetime, end: datetime, key_id: Optional[uuid.UUID],
                 recent_limit: int) -> Dict[str, Any]:
    key_filter = "AND key_id = %(key_id)s" if key_id else ""
    p = {"c": customer_id, "start": start, "end": end, "key_id": key_id}
    window = f"customer_id = %(c)s AND created_at >= %(start)s AND created_at < %(end)s {key_filter}"
    with pool().connection() as conn:
        req_count = conn.execute(f"SELECT count(*) FROM usage_requests WHERE {window}", p).fetchone()[0]
        ops, ok, err = conn.execute(
            f"SELECT count(*), count(*) FILTER (WHERE status = 'ok'), count(*) FILTER (WHERE status = 'error') "
            f"FROM usage_operations WHERE {window}", p).fetchone()
        by_op = conn.execute(
            f"SELECT operation, count(*), count(*) FILTER (WHERE status = 'ok'), "
            f"count(*) FILTER (WHERE status = 'error'), avg(duration_ms) "
            f"FROM usage_operations WHERE {window} GROUP BY operation ORDER BY count(*) DESC, operation", p).fetchall()
        by_key = conn.execute(
            f"SELECT k.id, k.name, k.prefix, k.revoked_at, "
            f"  (SELECT count(*) FROM usage_requests r WHERE r.key_id = k.id AND r.created_at >= %(start)s AND r.created_at < %(end)s), "
            f"  (SELECT count(*) FROM usage_operations o WHERE o.key_id = k.id AND o.created_at >= %(start)s AND o.created_at < %(end)s) "
            f"FROM api_keys k WHERE k.customer_id = %(c)s {('AND k.id = %(key_id)s' if key_id else '')} "
            f"ORDER BY k.created_at DESC", p).fetchall()
        by_day = conn.execute(
            f"SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(DISTINCT request_id), count(*) "
            f"FROM usage_operations WHERE {window} GROUP BY day ORDER BY day", p).fetchall()
        recent = conn.execute(
            f"SELECT id, key_id, created_at, task_count, ok_count, error_count, duration_ms FROM usage_requests "
            f"WHERE {window} ORDER BY created_at DESC LIMIT %(lim)s", {**p, "lim": recent_limit}).fetchall()
        ops_by_req: Dict[uuid.UUID, List[Dict[str, Any]]] = {}
        if recent:
            for rid, op, status, etype, dur in conn.execute(
                    "SELECT request_id, operation, status, error_type, duration_ms FROM usage_operations "
                    "WHERE customer_id = %s AND request_id = ANY(%s) ORDER BY id",
                    (customer_id, [r[0] for r in recent])).fetchall():
                ops_by_req.setdefault(rid, []).append(
                    {"operation": op, "status": status, "error_type": etype, "duration_ms": round(dur, 1)})
    return {
        "start": start, "end": end, "key_id": str(key_id) if key_id else None,
        "totals": {"requests": req_count, "operations": ops, "operations_ok": ok, "operations_error": err},
        "by_operation": [{"operation": o, "count": c, "ok": k, "error": e, "avg_duration_ms": round(a or 0, 1)}
                         for o, c, k, e, a in by_op],
        "by_key": [{"key_id": str(i), "name": n, "prefix": pf, "status": "revoked" if rv else "active",
                    "requests": rq, "operations": oc} for i, n, pf, rv, rq, oc in by_key],
        "by_day": [{"date": d.isoformat(), "requests": rq, "operations": oc} for d, rq, oc in by_day],
        "recent_requests": [
            {"id": str(i), "key_id": str(k), "created_at": t, "task_count": tc, "ok_count": oc,
             "error_count": ec, "duration_ms": round(d, 1), "operations": ops_by_req.get(i, [])}
            for i, k, t, tc, oc, ec, d in recent],
    }
