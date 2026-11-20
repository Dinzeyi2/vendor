"""Regenerate docs/open-source-acknowledgements.md and THIRD_PARTY_NOTICES.md from app/catalog.py
and the license texts in licenses/ (copied unmodified from each project's source).

Run after adding or renaming an operation:  python scripts/build_open_source_page.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import catalog  # noqa: E402

LICENSE_FILES = {
    "RapidFuzz": "RapidFuzz-MIT.txt", "hnswlib": "hnswlib-Apache-2.0.txt", "XGBoost": "XGBoost-Apache-2.0.txt",
    "SCS": "SCS-MIT.txt", "DoWhy": "DoWhy-MIT.txt", "Kornia": "Kornia-Apache-2.0.txt",
    "PaddleOCR and its pretrained OCR models": "PaddleOCR-Apache-2.0.txt", "PySR": "PySR-Apache-2.0.txt",
    "Timefold Solver": "TimefoldSolver-Apache-2.0.txt", "Rapier": "Rapier-Apache-2.0.txt",
}
APACHE_TEXT = "Kornia-Apache-2.0.txt"  # the standard terms, without the appendix template


def text(name: str) -> str:
    return (ROOT / "licenses" / name).read_text(encoding="utf-8").strip("\n")


def page() -> str:
    rows = "\n".join(f"| `{e.name}` | [{e.built_on.project}]({e.built_on.source}) | {e.built_on.license} | "
                     f"{e.built_on.copyright} |" for e in catalog.ENTRIES)
    mit = "\n\n".join(f"### {e.built_on.project}\n\n```text\n{text(LICENSE_FILES[e.built_on.project])}\n```"
                      for e in catalog.ENTRIES if e.built_on.license == "MIT")
    apache = ", ".join(e.built_on.project for e in catalog.ENTRIES if e.built_on.license == "Apache-2.0")
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
"""


def notices() -> str:
    rows = "\n".join(f"| `{e.name}` | `{a}` | [{e.built_on.project}]({e.built_on.source}) | {e.built_on.license} | "
                     f"{e.built_on.copyright} | [licenses/{LICENSE_FILES[e.built_on.project]}]"
                     f"(licenses/{LICENSE_FILES[e.built_on.project]}) |"
                     for e in catalog.ENTRIES for a in [e.alias])
    return f"""# Third-party notices

The Cognex API runs the following open-source software. Each project is used under its license,
reproduced in full in [`licenses/`](licenses/) (copied unmodified from each project's source).
None of these projects ships a NOTICE file in its repository root.

| Cognex operation | Original name (alias) | Project | License | Copyright | License text |
|---|---|---|---|---|---|
{rows}

Public attribution is kept separate from the operation catalog: `GET /v1/open-source` and the
page [docs/open-source-acknowledgements.md](docs/open-source-acknowledgements.md) (publish it as its
own page next to the API guide). Regenerate both with `python scripts/build_open_source_page.py`
and keep them, and `licenses/`, when operations are renamed or added.
"""


if __name__ == "__main__":
    (ROOT / "docs" / "open-source-acknowledgements.md").write_text(page(), encoding="utf-8", newline="\n")
    (ROOT / "THIRD_PARTY_NOTICES.md").write_text(notices(), encoding="utf-8", newline="\n")
    print("wrote docs/open-source-acknowledgements.md and THIRD_PARTY_NOTICES.md")
