"""License detection for repositories (shared by customer imports and operator onboarding).

Text heuristics on the repo's own license files, used only to record attribution (the SPDX id,
copyright lines and license text shown on the open-source page). Nothing here accepts or rejects
a repository: its license never blocks import, testing or publishing.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LICENSES: List[Tuple[str, str]] = [  # (spdx, distinctive phrase) - most specific first; for attribution
    ("AGPL-3.0", "GNU AFFERO GENERAL PUBLIC LICENSE"),
    ("LGPL", "GNU LESSER GENERAL PUBLIC LICENSE"),
    ("GPL", "GNU GENERAL PUBLIC LICENSE"),
    ("SSPL", "Server Side Public License"),
    ("BUSL", "Business Source License"),
    ("CC-BY-NC", "NonCommercial"),
    ("EUPL", "European Union Public Licen"),
    ("MPL-2.0", "Mozilla Public License"),
    ("Apache-2.0", "Apache License"),
    ("MIT", "Permission is hereby granted, free of charge"),
    ("BSD-3-Clause", "Neither the name"),
    ("BSD-2-Clause", "Redistributions in binary form must reproduce"),
    ("ISC", "Permission to use, copy, modify, and/or distribute this software"),
    ("Unlicense", "This is free and unencumbered software"),
    ("Zlib", "This software is provided 'as-is'"),
    ("PSF-2.0", "PYTHON SOFTWARE FOUNDATION LICENSE"),
]
LICENSE_FILE_RE = re.compile(r"(?i)^(licen[cs]e|copying|notice|copyright)([.-].*)?$")


def classify(text: str) -> str:
    for spdx, phrase in LICENSES:
        if phrase.lower() in text.lower():
            return spdx
    return "unknown"


def copyright_lines(text: str) -> List[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"(?i)^copyright\b", s) and not re.search(r"\{|\[yyyy|owner\]|notice|license|holders", s, re.I):
            out.append(s[:300])
    return out[:10]


def license_files(repo: Path) -> List[Dict]:
    found = []
    for p in sorted(repo.iterdir()):
        if p.is_file() and LICENSE_FILE_RE.match(p.name) and p.stat().st_size < 512_000:
            text = p.read_text(encoding="utf-8", errors="replace")
            notice = p.name.upper().startswith("NOTICE")
            spdx = "NOTICE" if notice else classify(text)
            found.append({"file": p.name, "spdx_guess": spdx,
                          "copyright_lines": copyright_lines(text), "bytes": p.stat().st_size, "text": text})
    return found


def attribution(files: List[Dict]) -> Tuple[str, Optional[Dict]]:
    """(SPDX id or NOASSERTION, the license file or None), recorded for attribution only.

    A repository's license never blocks importing, testing or publishing it."""
    licenses = [f for f in files if f["spdx_guess"] != "NOTICE"]
    if not licenses:
        return "NOASSERTION", None
    f = licenses[0]
    return (f["spdx_guess"] if f["spdx_guess"] != "unknown" else "NOASSERTION"), f
