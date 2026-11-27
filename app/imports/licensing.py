"""License detection for repositories (shared by customer imports and operator onboarding).

Informational only: it records attribution (SPDX id, the project's copyright lines, the license
text) for the import record and the open-source page. Nothing here accepts or rejects a
repository: its license never blocks import, testing or publishing.

- The license is identified from its title (the first lines of the file, e.g. "GNU GENERAL
  PUBLIC LICENSE / Version 2, June 1991"), so a GPL text that mentions the Lesser GPL further
  down is still GPL. Files without a recognizable title fall back to the distinctive phrase that
  appears first in the text.
- Copyright lines are the project's own: license-template placeholders ("<year> <name of
  author>", "[yyyy] [name of copyright owner]") and the license document's own copyright (e.g.
  the Free Software Foundation's on the GPL text) are skipped. If the license file names no
  copyright holder, the README / NOTICE / AUTHORS files and the package's source headers are used.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# (SPDX base, title pattern, versioned): matched against the first lines of a license file.
TITLES: List[Tuple[str, str, bool]] = [
    ("AGPL", r"GNU AFFERO GENERAL PUBLIC LICEN[CS]E", True),
    ("LGPL", r"GNU (LESSER|LIBRARY) GENERAL PUBLIC LICEN[CS]E", True),
    ("GPL", r"GNU GENERAL PUBLIC LICEN[CS]E", True),
    ("SSPL", r"SERVER SIDE PUBLIC LICEN[CS]E", True),
    ("BUSL", r"BUSINESS SOURCE LICEN[CS]E", True),
    ("EUPL", r"EUROPEAN UNION PUBLIC LICEN[CS]E", True),
    ("MPL", r"MOZILLA PUBLIC LICEN[CS]E", True),
    ("Apache", r"APACHE LICEN[CS]E", True),
    ("BSL-1.0", r"BOOST SOFTWARE LICEN[CS]E", False),
    ("BSD-3-Clause", r"BSD 3-CLAUSE", False),
    ("BSD-2-Clause", r"BSD 2-CLAUSE", False),
    ("ISC", r"\bISC LICEN[CS]E", False),
    ("MIT", r"\bMIT LICEN[CS]E", False),
    ("Unlicense", r"THE UNLICENSE", False),
    ("PSF-2.0", r"PYTHON SOFTWARE FOUNDATION LICEN[CS]E", False),
]
DEFAULT_VERSION = {"AGPL": "3.0", "SSPL": "1.0", "BUSL": "1.1", "MPL": "2.0", "Apache": "2.0"}
GNU = ("GPL", "LGPL", "AGPL")

# Body phrases for files without a recognizable title; the one appearing first in the text wins.
PHRASES: List[Tuple[str, str]] = [
    ("AGPL-3.0", "GNU Affero General Public License"),
    ("LGPL", "GNU Lesser General Public License"),
    ("GPL", "GNU General Public License"),
    ("SSPL-1.0", "Server Side Public License"),
    ("BUSL-1.1", "Business Source License"),
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
NOTICE_FILE_RE = re.compile(r"(?i)^(readme|notice|authors|copyright|credits)([.-].*)?$")

PLACEHOLDER_RE = re.compile(  # template fields in license texts: <year>, [yyyy], {name of author}, ...
    r"(?i)[<\[{]\s*(yyyy|year|name|owner|author|copyright|holder|fullname|organi[sz]ation)[^>\]}]*[>\]}]"
    r"|\byear\s+name of author\b|\bname of (the )?(author|copyright owner)\b")
HOLDER_RE = re.compile(r"(?i)^(?:copyright|\(c\)|©)\b.*?(\d{4}|\(c\)|©)")


def title(text: str, lines: int = 6) -> str:
    """The first non-empty lines of a license file: where its name and version are."""
    return "\n".join([s.strip() for s in text.splitlines() if s.strip()][:lines])


def classify(text: str) -> str:
    """SPDX id (best effort) for a license text, or 'unknown'. Informational only."""
    head = title(text)
    found = [(m.start(), i, m) for i, (_, pattern, _) in enumerate(TITLES)
             if (m := re.search(pattern, head, re.I))]
    if found:  # the license named first is the title (a GPL title can be followed by "Lesser GPL")
        _, i, m = min(found, key=lambda x: (x[0], x[1]))
        base, _, versioned = TITLES[i]
        if not versioned:
            return base
        v = re.search(r"(?i)\bversion\s+(\d+(?:\.\d+)?)", head[m.start():])
        version = v.group(1) if v else DEFAULT_VERSION.get(base)
        if version and "." not in version:
            version += ".0"
        return f"{base}-{version}" if version else base
    low = text.lower()
    hits = [(low.find(p.lower()), spdx) for spdx, p in PHRASES if p.lower() in low]
    return min(hits)[1] if hits else "unknown"


def copyright_lines(text: str, spdx: str = "") -> List[str]:
    """The copyright holders named in a text, without template placeholders or the license
    document's own copyright."""
    out = []
    for line in text.splitlines():
        s = line.strip().lstrip("#*/;-").strip()
        if not HOLDER_RE.match(s) or PLACEHOLDER_RE.search(s):
            continue
        if re.search(r"(?i)\b(notice|this license|permission notice)\b", s):
            continue
        if spdx.split("-")[0] in GNU and "free software foundation" in s.lower():
            continue  # the FSF's copyright on the GPL/LGPL/AGPL document, not the project's
        if s not in out:
            out.append(s[:300])
    return out[:10]


def license_files(repo: Path) -> List[Dict]:
    found = []
    for p in sorted(repo.iterdir()):
        if p.is_file() and LICENSE_FILE_RE.match(p.name) and p.stat().st_size < 512_000:
            text = p.read_text(encoding="utf-8", errors="replace")
            spdx = "NOTICE" if p.name.upper().startswith("NOTICE") else classify(text)
            found.append({"file": p.name, "spdx_guess": spdx,
                          "copyright_lines": copyright_lines(text, spdx), "bytes": p.stat().st_size, "text": text})
    return found


def _project_texts(repo: Path, package_dir: Optional[Path] = None) -> List[str]:
    """README / NOTICE / AUTHORS files and the package's source headers (data, never executed)."""
    texts = []
    for p in sorted(repo.iterdir()):
        if p.is_file() and NOTICE_FILE_RE.match(p.name) and p.stat().st_size < 512_000:
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
    if package_dir is not None and package_dir.is_dir():
        for f in sorted(package_dir.glob("*.py"))[:50]:
            texts.append("\n".join(f.read_text(encoding="utf-8", errors="replace").splitlines()[:40]))
    return texts


def attribution(files: List[Dict], repo: Optional[Path] = None,
                package_dir: Optional[Path] = None) -> Tuple[str, Optional[Dict], List[str]]:
    """(SPDX id or NOASSERTION, the license file or None, the project's copyright lines).

    Recorded for attribution only: a repository's license never blocks importing, testing or
    publishing it."""
    licenses = [f for f in files if f["spdx_guess"] != "NOTICE"]
    lic = licenses[0] if licenses else None
    spdx = lic["spdx_guess"] if lic and lic["spdx_guess"] != "unknown" else "NOASSERTION"
    holders = list(lic["copyright_lines"]) if lic else []
    texts = _project_texts(repo, package_dir) if repo is not None else []
    if not holders:
        for f in files:  # a NOTICE file next to the license
            holders += [h for h in f["copyright_lines"] if h not in holders]
        for t in texts:
            holders += [h for h in copyright_lines(t, spdx) if h not in holders]
    if spdx.split("-")[0] in GNU and re.fullmatch(r"[A-Z]+-\d\.\d", spdx):
        # "-or-later" only when the project itself says so (the GPL text always contains the phrase)
        if any(re.search(r"(?i)\bor\s*\(at your option\)\s*any later version", t) or
               re.search(rf"(?i)\b{spdx.split('-')[0]}v?{spdx[-3]}\+|{spdx.split('-')[0]}v{spdx[-3]} or later", t)
               for t in texts):
            spdx += "-or-later"
    return spdx, lic, holders[:10]
