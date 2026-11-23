"""License detection for repositories (shared by customer imports and operator onboarding).

Text heuristics on the repo's own license files. Only clearly permissive licenses pass; copyleft,
source-available, non-commercial, unknown or missing licenses are refused with a reason.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LICENSES: List[Tuple[str, bool, str]] = [  # (spdx, permissive?, distinctive phrase) - most specific first
    ("AGPL-3.0", False, "GNU AFFERO GENERAL PUBLIC LICENSE"),
    ("LGPL", False, "GNU LESSER GENERAL PUBLIC LICENSE"),
    ("GPL", False, "GNU GENERAL PUBLIC LICENSE"),
    ("SSPL", False, "Server Side Public License"),
    ("BUSL", False, "Business Source License"),
    ("CC-BY-NC", False, "NonCommercial"),
    ("EUPL", False, "European Union Public Licen"),
    ("MPL-2.0", True, "Mozilla Public License"),
    ("Apache-2.0", True, "Apache License"),
    ("MIT", True, "Permission is hereby granted, free of charge"),
    ("BSD-3-Clause", True, "Neither the name"),
    ("BSD-2-Clause", True, "Redistributions in binary form must reproduce"),
    ("ISC", True, "Permission to use, copy, modify, and/or distribute this software"),
    ("Unlicense", True, "This is free and unencumbered software"),
    ("Zlib", True, "This software is provided 'as-is'"),
    ("PSF-2.0", True, "PYTHON SOFTWARE FOUNDATION LICENSE"),
]
LICENSE_FILE_RE = re.compile(r"(?i)^(licen[cs]e|copying|notice|copyright)([.-].*)?$")


def classify(text: str) -> Tuple[str, bool]:
    for spdx, permissive, phrase in LICENSES:
        if phrase.lower() in text.lower():
            return spdx, permissive
    return "unknown", False


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
            spdx, permissive = ("NOTICE", True) if notice else classify(text)
            found.append({"file": p.name, "spdx_guess": spdx, "permissive": permissive,
                          "copyright_lines": copyright_lines(text), "bytes": p.stat().st_size, "text": text})
    return found


def verdict(files: List[Dict]) -> Tuple[bool, Optional[str], Optional[str]]:
    """(ok, spdx, reason-if-refused) for a repo's license files."""
    licenses = [f for f in files if f["spdx_guess"] != "NOTICE"]
    if not licenses:
        return False, None, ("The repository has no license file, so it can't be used without the "
                             "owner's permission. Add a permissive license (e.g. MIT or Apache-2.0).")
    bad = [f for f in licenses if not f["permissive"]]
    if bad:
        f = bad[0]
        kind = "an unrecognized license" if f["spdx_guess"] == "unknown" else f"{f['spdx_guess']}"
        return False, f["spdx_guess"], (f"{f['file']} is {kind}. Cognex only imports permissively licensed "
                                         f"code (MIT, BSD, Apache-2.0, ISC, MPL-2.0, Unlicense, Zlib, PSF).")
    return True, licenses[0]["spdx_guess"], None
