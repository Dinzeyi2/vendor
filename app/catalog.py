"""Public operation names, compatibility aliases and attribution, derived from the manifests.

Light on purpose (no worker or library imports): app.accounts uses it to report usage under
public names. The public names describe what an operation does; the original library-based
names keep working as aliases but are never listed publicly. Attribution is served separately
by GET /v1/open-source.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app import manifests
from app.manifests import BuiltOn, Capability  # noqa: F401  (re-exported)

CREDITS_PER_OPERATION = 1

ENTRIES: List[Capability] = []
BY_NAME: Dict[str, Capability] = {}
ALIASES: Dict[str, str] = {}
# Output fields that name the underlying engine. Omitted when the public name is used;
# returned unchanged for the original names so existing integrations see identical output.
ENGINE_FIELDS: Dict[str, tuple] = {}


def add(cap: Capability) -> None:
    remove(cap)
    ENTRIES.append(cap)
    BY_NAME[cap.name] = cap
    for a in cap.aliases:
        ALIASES[a] = cap.name
    if cap.engine_fields:
        ENGINE_FIELDS[cap.name] = cap.engine_fields


def remove(cap: Capability) -> None:
    ENTRIES[:] = [e for e in ENTRIES if e.name != cap.name]
    BY_NAME.pop(cap.name, None)
    for a in [a for a, n in ALIASES.items() if n == cap.name]:
        del ALIASES[a]
    ENGINE_FIELDS.pop(cap.name, None)


def canonical(name: str) -> str:
    """The public name for `name` (itself if it is already public or unknown)."""
    return ALIASES.get(name, name)


def entry(name: str) -> Optional[Capability]:
    return BY_NAME.get(canonical(name))


if not ENTRIES:  # standalone use (e.g. app.accounts without the registry): read the manifests
    for _cap in manifests.load_dir():
        add(_cap)
