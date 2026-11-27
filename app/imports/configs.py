"""Saved repo configurations (operator): the standard way to fix a repo Cognex can't fully detect.

A configuration has the build-plan format (app/imports/plan.py CONFIG_SCHEMA). It is keyed by
OWNER/REPO, applies to every later import of that repo by any customer, and a failed import is
re-queued automatically when its repo's configuration changes.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app import db
from app.imports import plan


def _key(repo: str) -> str:
    return repo.strip().strip("/").lower()


def get(repo: str) -> Optional[Dict[str, Any]]:
    with db.pool().connection() as conn:
        r = conn.execute("SELECT config FROM repo_configs WHERE repo = %s", (_key(repo),)).fetchone()
    return r[0] if r else None


def view(repo: str) -> Optional[Dict[str, Any]]:
    with db.pool().connection() as conn:
        r = conn.execute("SELECT repo, config, config_hash, created_at, updated_at FROM repo_configs WHERE repo = %s",
                         (_key(repo),)).fetchone()
    return dict(zip(("repo", "config", "config_hash", "created_at", "updated_at"), r)) if r else None


def list_all() -> List[Dict[str, Any]]:
    with db.pool().connection() as conn:
        rows = conn.execute("SELECT repo, config, config_hash, created_at, updated_at FROM repo_configs "
                            "ORDER BY updated_at DESC LIMIT 500").fetchall()
    return [dict(zip(("repo", "config", "config_hash", "created_at", "updated_at"), r)) for r in rows]


def save(repo: str, config: Dict[str, Any]) -> Dict[str, Any]:
    plan.validate_config(config)  # ValueError with the exact problem
    h = plan.config_hash(config)
    with db.pool().connection() as conn:
        conn.execute("INSERT INTO repo_configs (repo, config, config_hash) VALUES (%s, %s, %s) ON CONFLICT (repo) DO "
                     "UPDATE SET config = EXCLUDED.config, config_hash = EXCLUDED.config_hash, updated_at = now()",
                     (_key(repo), json.dumps(config), h))
    return view(repo)


def delete(repo: str) -> bool:
    with db.pool().connection() as conn:
        return conn.execute("DELETE FROM repo_configs WHERE repo = %s", (_key(repo),)).rowcount > 0


def current_hash(repo: str) -> str:
    return plan.config_hash(get(repo))
