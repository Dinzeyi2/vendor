"""Step 1 of onboarding a GitHub repo: fetch it for reading and summarize what's there.

Usage:  python scripts/onboard/inspect_repo.py https://github.com/OWNER/REPO [--ref TAG_OR_BRANCH]

Clones into .onboarding/OWNER__REPO (gitignored) and writes .onboarding/OWNER__REPO.report.json:
the exact commit, license files and their likely SPDX id (copyleft is flagged), the published
package name and versions (PyPI / npm), docs and examples to read, and the public Python/JS API
surface found by parsing source text. It never imports or runs the repo's code: everything in
the clone is untrusted data.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.imports import licensing  # noqa: E402  (shared with customer imports)
WORK = ROOT / ".onboarding"
URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")

DOC_DIRS = ("docs", "doc", "examples", "example", "tutorials", "notebooks", "demo", "samples")
SKIP_DIRS = {".git", "node_modules", "build", "dist", "__pycache__", ".github", "vendor", "third_party", "tests", "test"}


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def fetch(owner: str, repo: str, ref: str | None) -> Path:
    dest = WORK / f"{owner}__{repo}"
    url = f"https://github.com/{owner}/{repo}.git"
    if dest.exists():
        git("fetch", "--depth", "1", "origin", ref or "HEAD", cwd=dest)
        git("checkout", "--force", "FETCH_HEAD", cwd=dest)
    else:
        WORK.mkdir(exist_ok=True)
        git("clone", "--depth", "1", *(["--branch", ref] if ref else []), url, str(dest))
    return dest


def licenses(repo: Path):
    return [{k: v for k, v in f.items() if k != "text"} for f in licensing.license_files(repo)]


def http_json(url: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cognex-onboarding"}),
                                    timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return None


def packaging(repo: Path):
    info = {}
    py = repo / "pyproject.toml"
    if py.is_file():
        try:
            data = tomllib.loads(py.read_text(encoding="utf-8"))
            proj = data.get("project", {})
            info["python"] = {"name": proj.get("name") or data.get("tool", {}).get("poetry", {}).get("name"),
                              "version": proj.get("version"), "requires_python": proj.get("requires-python"),
                              "dependencies": proj.get("dependencies", [])}
        except tomllib.TOMLDecodeError:
            info["python"] = {"error": "pyproject.toml did not parse"}
    if "python" not in info and (repo / "setup.py").is_file():
        m = re.search(r"name\s*=\s*['\"]([^'\"]+)", (repo / "setup.py").read_text(encoding="utf-8", errors="replace"))
        info["python"] = {"name": m.group(1) if m else None, "source": "setup.py"}
    pkg = repo / "package.json"
    if pkg.is_file():
        data = json.loads(pkg.read_text(encoding="utf-8"))
        info["node"] = {"name": data.get("name"), "version": data.get("version"), "main": data.get("main"),
                        "exports": data.get("exports"), "dependencies": data.get("dependencies", {})}
    if (repo / "Cargo.toml").is_file():
        info["rust"] = {"note": "Rust crate: needs a published Python/JS binding to be callable from Cognex"}
    name = (info.get("python") or {}).get("name")
    if name:
        meta = http_json(f"https://pypi.org/pypi/{name}/json")
        if meta:
            releases = sorted(meta.get("releases", {}), key=lambda v: [int(x) if x.isdigit() else x
                                                                        for x in re.split(r"[.+-]", v)], reverse=False)
            info["pypi"] = {"name": meta["info"]["name"], "latest": meta["info"]["version"],
                            "recent_releases": releases[-8:], "license": meta["info"].get("license")}
    name = (info.get("node") or {}).get("name")
    if name:
        meta = http_json(f"https://registry.npmjs.org/{name}")
        if meta:
            info["npm"] = {"name": name, "latest": meta.get("dist-tags", {}).get("latest")}
    return info


def docs_and_examples(repo: Path, limit: int = 200):
    files = []
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo)
        if not p.is_file() or set(rel.parts[:-1]) & SKIP_DIRS:
            continue
        top_readme = len(rel.parts) == 1 and re.match(r"(?i)^readme", p.name)
        in_docs = rel.parts[0].lower() in DOC_DIRS
        if (top_readme or in_docs) and p.suffix.lower() in {".md", ".rst", ".txt", ".py", ".ipynb", ".js", ".mjs", ".ts", ""}:
            files.append({"path": str(rel).replace("\\", "/"), "bytes": p.stat().st_size})
    return files[:limit]


def python_api(repo: Path, package: str | None):
    """Public functions/classes of the top-level package(s), from source text (no imports)."""
    roots = [d for d in (repo, repo / "src") if d.is_dir()]
    pkgs = []
    for r in roots:
        for d in sorted(r.iterdir()):
            if d.is_dir() and (d / "__init__.py").is_file() and d.name not in SKIP_DIRS and not d.name.startswith("."):
                pkgs.append(d)
    if package:
        wanted = package.replace("-", "_").lower()
        pkgs = [d for d in pkgs if d.name.lower() == wanted] or pkgs
    api = {}
    for d in pkgs[:3]:
        for f in sorted(d.rglob("*.py"))[:400]:
            if any(part.startswith("_") and part != "__init__.py" for part in f.relative_to(d).parts):
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            names = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
                    doc = (ast.get_docstring(node) or "").strip().split("\n")[0][:120]
                    args = [a.arg for a in node.args.args][:8] if isinstance(node, ast.FunctionDef) else []
                    names.append({"name": node.name, "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                                  "args": args, "doc": doc})
                if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__all__" for t in node.targets):
                    try:
                        names.append({"__all__": ast.literal_eval(node.value)})
                    except ValueError:
                        pass
            if names:
                api[str(f.relative_to(repo)).replace("\\", "/")] = names
    return api


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--ref", help="tag or branch to inspect (default: the default branch)")
    args = ap.parse_args()
    m = URL_RE.match(args.url.strip())
    if not m:
        sys.exit("expected https://github.com/OWNER/REPO")
    owner, repo = m.groups()
    path = fetch(owner, repo, args.ref)
    pkg = packaging(path)
    report = {
        "url": f"https://github.com/{owner}/{repo}", "clone": str(path.relative_to(ROOT)).replace("\\", "/"),
        "commit": git("rev-parse", "HEAD", cwd=path), "ref": args.ref or "default branch",
        "licenses": licenses(path), "packaging": pkg,
        "docs_and_examples": docs_and_examples(path),
        "python_api": python_api(path, (pkg.get("python") or {}).get("name")),
    }
    lic = report["licenses"]
    report["license_verdict"] = (
        "MISSING: no license file; do not onboard without the owner's permission" if not lic else
        "STOP: copyleft or restrictive license; ask the user before onboarding"
        if any(not l["permissive"] for l in lic) else "OK: permissive")
    out = WORK / f"{owner}__{repo}.report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"repo      {report['url']} @ {report['commit']}")
    print(f"license   {report['license_verdict']}: " + ", ".join(f"{l['file']} ({l['spdx_guess']})" for l in lic))
    print(f"packages  {json.dumps({k: v for k, v in pkg.items() if k in ('pypi', 'npm', 'python', 'node')})[:600]}")
    print(f"docs      {len(report['docs_and_examples'])} files, e.g. "
          + ", ".join(d['path'] for d in report['docs_and_examples'][:8]))
    print(f"api       {sum(len(v) for v in report['python_api'].values())} public names in {len(report['python_api'])} files")
    print(f"report    {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
