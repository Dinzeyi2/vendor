"""License detection: informational attribution only (it never blocks anything)."""
import json
from pathlib import Path

import pytest

from app.imports import licensing

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "import_repos"

GPL2 = """                    GNU GENERAL PUBLIC LICENSE
                       Version 2, June 1991

 Copyright (C) 1989, 1991 Free Software Foundation, Inc.,
 Everyone is permitted to copy and distribute verbatim copies

 ... the GNU Lesser General Public License instead ...
    Copyright (C) <year>  <name of author>
    Gnomovision version 69, Copyright (C) year name of author
"""


@pytest.mark.parametrize("text, spdx", [
    (GPL2, "GPL-2.0"),                                           # the title wins over a later LGPL mention
    ("GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n... GNU Lesser General Public License ...", "GPL-3.0"),
    ("GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1, February 1999\n... GNU General Public License ...", "LGPL-2.1"),
    ("GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007", "LGPL-3.0"),
    ("GNU LIBRARY GENERAL PUBLIC LICENSE\nVersion 2, June 1991", "LGPL-2.0"),
    ("GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007\n... GNU General Public License ...", "AGPL-3.0"),
    ("Apache License\nVersion 2.0, January 2004", "Apache-2.0"),
    ("Mozilla Public License Version 2.0", "MPL-2.0"),
    ("MIT License\n\nCopyright (c) 2020 X\n\nPermission is hereby granted, free of charge", "MIT"),
    ("Copyright (c) 2020 X\n\nPermission is hereby granted, free of charge, to any person", "MIT"),  # untitled
    ("Some text. See the GNU General Public License, and the GNU Lesser General Public License.", "GPL"),
    ("All rights reserved by nobody in particular.", "unknown"),
])
def test_license_is_identified_from_its_title(text, spdx):
    assert licensing.classify(text) == spdx


def test_template_placeholders_and_the_license_documents_own_copyright_are_skipped():
    assert licensing.copyright_lines(GPL2, "GPL-2.0") == []
    apache = "Copyright [yyyy] [name of copyright owner]\nCopyright {yyyy} {name}\nCopyright 2020 Real Person"
    assert licensing.copyright_lines(apache, "Apache-2.0") == ["Copyright 2020 Real Person"]
    mit = ("Copyright (c) 2015 Mapado\n\nThe above copyright notice and this permission notice shall be included")
    assert licensing.copyright_lines(mit, "MIT") == ["Copyright (c) 2015 Mapado"]
    assert licensing.copyright_lines("Copyright 2001, A B <ab@example.org>, all rights reserved.", "GPL-2.0") == [
        "Copyright 2001, A B <ab@example.org>, all rights reserved."]           # e-mail in <> is not a placeholder


def test_attribution_falls_back_to_the_readme_and_reads_or_later_from_the_project():
    repo = FIXTURES / "gplreadme"
    spdx, lic, holders = licensing.attribution(licensing.license_files(repo), repo, repo / "gplreadme")
    assert spdx == "GPL-2.0-or-later" and lic["file"] == "LICENSE"
    assert holders == ["Copyright 2001, Ada Example <ada@example.org>, all rights reserved.",
                       "Copyright 2026, Tomaž Example <tomaz@example.org>"]
    # the "any later version" sentence inside the GPL text itself doesn't count as the project's choice
    spdx, _, _ = licensing.attribution(licensing.license_files(repo))
    assert spdx == "GPL-2.0"


def test_built_in_license_files_match_their_manifests():
    for f in sorted((ROOT / "app" / "capabilities").glob("*.json")):
        b = json.loads(f.read_text(encoding="utf-8"))["built_on"]
        assert licensing.classify((ROOT / b["license_file"]).read_text(encoding="utf-8")) == b["license"], f.name


def test_detection_is_informational_only():
    assert not hasattr(licensing, "verdict")
    assert all(len(t) == 3 for t in licensing.TITLES) and all(len(p) == 2 for p in licensing.PHRASES)
