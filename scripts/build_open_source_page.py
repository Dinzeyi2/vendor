"""Regenerate docs/open-source-acknowledgements.md and THIRD_PARTY_NOTICES.md from the capability
manifests (app/capabilities/*.json) and the license texts in licenses/ (copied unmodified from
each project's source).

Run after adding or renaming an operation:  python scripts/build_open_source_page.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import catalog  # noqa: E402  (built from app/capabilities/*.json)

APACHE_TEXT = "licenses/Kornia-Apache-2.0.txt"  # the standard terms, without the appendix template


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8").strip("\n")


def entries():
    return sorted(catalog.ENTRIES, key=lambda e: e.name)


def projects():
    """One entry per project (several operations can share one)."""
    seen = {}
    for e in entries():
        seen.setdefault(e.built_on.project, e.built_on)
    return list(seen.values())


def page() -> str:
    rows = "\n".join(f"| `{e.name}` | [{e.built_on.project}]({e.built_on.source}) | {e.built_on.license} | "
                     f"{e.built_on.copyright} |" for e in entries())
    mit = "\n\n".join(f"### {b.project}\n\n```text\n{text(b.license_file)}\n```"
                      for b in projects() if b.license == "MIT")
    apache = ", ".join(b.project for b in projects() if b.license == "Apache-2.0")
    others = [b for b in projects() if b.license not in ("MIT", "Apache-2.0")]
    other = "\n\n".join(f"## {b.project} ({b.license})\n\n```text\n{text(b.license_file)}\n```" for b in others)
    return f"""# Open-source acknowledgements

Cognex is built with the open-source projects below. We thank their authors and contributors.
Each project is used under its license, and the notices below are reproduced as the licenses
require. Cognex operation names are our own and don't imply endorsement by these projects or
their authors.

| Cognex operation | Project | License | Copyright |
|---|---|---|---|
{rows}

These projects are provided by their authors "as is", without warranties or conditions of any kind.
The same list is available as JSON from `GET /v1/open-source`.

## MIT License notices

{mit}

## Apache License 2.0

Used by: {apache}.

```text
{text(APACHE_TEXT)}
```
{("" if not other else chr(10) + other + chr(10))}"""


def notices() -> str:
    rows = "\n".join(
        f"| `{e.name}` | {', '.join(f'`{a}`' for a in e.aliases) or '(none)'} | [{e.built_on.project}]({e.built_on.source}) | "
        f"{e.built_on.license} | {e.built_on.copyright} | `{e.built_on.dependency['name']}` "
        f"{e.built_on.dependency['version']} | [{e.built_on.license_file}]({e.built_on.license_file}) |"
        for e in entries())
    return f"""# Third-party notices

The Cognex API runs the following open-source software. Each project is used under its license,
reproduced in full in [`licenses/`](licenses/) (copied unmodified from each project's source).
None of these projects ships a NOTICE file in its repository root; when an onboarded project
does, its NOTICE is copied to `licenses/` next to its license and reproduced on the page.

| Cognex operation | Original names (aliases) | Project | License | Copyright | Pinned dependency | License text |
|---|---|---|---|---|---|---|
{rows}

Public attribution is kept separate from the operation catalog: `GET /v1/open-source` and the
page [docs/open-source-acknowledgements.md](docs/open-source-acknowledgements.md) (publish it as its
own page next to the API guide). Both are generated from the capability manifests: regenerate
with `python scripts/build_open_source_page.py` whenever a capability is added or renamed.
"""


if __name__ == "__main__":
    (ROOT / "docs" / "open-source-acknowledgements.md").write_text(page(), encoding="utf-8", newline="\n")
    (ROOT / "THIRD_PARTY_NOTICES.md").write_text(notices(), encoding="utf-8", newline="\n")
    print("wrote docs/open-source-acknowledgements.md and THIRD_PARTY_NOTICES.md")
