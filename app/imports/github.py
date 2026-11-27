"""Public GitHub repositories: URL validation, commit pinning and size-limited download.

Trusted code. Talks only to github.com (git smart-HTTP ref advertisement, no API token or API
rate limit), codeload.github.com (the tarball of one exact commit) and, for git submodules only,
api.github.com (which commit each submodule is pinned to). Never runs repo code.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

OWNER_RE = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
REPO_RE = r"[A-Za-z0-9._-]{1,100}"
REF_RE = r"[A-Za-z0-9._/-]{1,200}"
URL_RE = re.compile(rf"^https://github\.com/({OWNER_RE})/({REPO_RE}?)(?:\.git)?(?:/tree/({REF_RE}))?/?$")
from app.imports.profiles import CEILING  # noqa: E402

MAX_DOWNLOAD = CEILING["download_mb"] * 1024 * 1024    # compressed tarball (largest worker profile)
MAX_EXTRACTED = CEILING["extracted_mb"] * 1024 * 1024  # total size of extracted files
MAX_FILES = CEILING["files"]
USER_AGENT = "Cognex-Importer/1.0"


class ImportFailure(Exception):
    """A specific, customer-facing reason an import can't proceed."""

    def __init__(self, code: str, reason: str, **details):
        super().__init__(reason)
        self.code, self.reason, self.details = code, reason, details


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str
    ref: Optional[str]  # branch or tag from a /tree/<ref> URL; None = default branch

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"


def parse_url(raw: str) -> RepoRef:
    """Accept https://github.com/OWNER/REPO (optionally .git, /tree/REF); refuse anything else."""
    url = (raw or "").strip()
    if url.startswith("github.com/"):
        url = "https://" + url
    if url.startswith("http://github.com/"):
        url = "https://" + url[len("http://"):]
    url = url.split("#", 1)[0]
    if len(url) > 300 or "?" in url or "@" in url or " " in url:
        raise ImportFailure("invalid_url", "Use a public GitHub repository URL like https://github.com/OWNER/REPO.")
    m = URL_RE.match(url)
    if not m or m.group(2) in (".", "..") or (m.group(3) and ".." in m.group(3)):
        raise ImportFailure("invalid_url", "Use a public GitHub repository URL like https://github.com/OWNER/REPO.")
    return RepoRef(m.group(1), m.group(2), m.group(3))


def _get(url: str, limit: int, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https hosts only)
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ImportFailure("repo_too_large", f"The repository download is larger than {limit // (1024 * 1024)} MB, the "
                                              f"limit of the largest worker profile.")
    return data


def _pkt_lines(data: bytes):
    i = 0
    while i + 4 <= len(data):
        n = int(data[i:i + 4], 16)
        if n == 0:
            i += 4
            yield None
            continue
        yield data[i + 4:i + n]
        i += n


def _refs(ref: RepoRef):
    url = f"https://github.com/{ref.full_name}.git/info/refs?service=git-upload-pack"
    try:
        data = _get(url, 8 * 1024 * 1024, timeout=20)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise ImportFailure("repo_not_found", f"{ref.url} doesn't exist or isn't public.") from None
        raise ImportFailure("github_unavailable", f"GitHub answered HTTP {exc.code}; try again shortly.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ImportFailure("github_unavailable", "GitHub couldn't be reached; try again shortly.") from None
    refs, head = {}, None
    for line in _pkt_lines(data):
        if not line or line.startswith(b"#"):
            continue
        text = line.rstrip(b"\n").decode("utf-8", "replace")
        sha_name, _, _caps = text.partition("\x00")
        sha, _, name = sha_name.partition(" ")
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            refs[name] = sha
            if name == "HEAD":
                head = sha
    return refs, head


def resolve_commit(ref: RepoRef) -> str:
    """The exact commit SHA for the default branch (or the given branch/tag) of a public repo."""
    refs, head = _refs(ref)
    if ref.ref is None:
        if not head:
            raise ImportFailure("repo_empty", f"{ref.url} has no commits.")
        return head
    for name in (f"refs/tags/{ref.ref}^{{}}", f"refs/tags/{ref.ref}", f"refs/heads/{ref.ref}"):
        if name in refs:
            return refs[name]
    if re.fullmatch(r"[0-9a-f]{40}", ref.ref):
        return ref.ref
    raise ImportFailure("ref_not_found", f"{ref.url} has no branch or tag named '{ref.ref}'.")


def version_hint(ref: RepoRef, commit: str) -> Optional[str]:
    """The release version a commit is tagged with (v1.2.3 -> 1.2.3), for builds whose version comes
    from git metadata that a tarball doesn't have. None if no tag points at the commit."""
    try:
        refs, _ = _refs(ref)
    except ImportFailure:
        return None
    for name, sha in refs.items():
        if sha == commit and name.startswith("refs/tags/"):
            tag = name[len("refs/tags/"):].removesuffix("^{}")
            m = re.search(r"(\d+(?:\.\d+)+(?:[.-]?(?:a|b|rc|post|dev)\d*)?)$", tag)
            if m:
                return m.group(1)
    return None


DOWNLOADED: dict = {}  # dest folder -> compressed bytes fetched (input to the worker-profile choice)


def downloaded_bytes(dest: Path) -> int:
    return DOWNLOADED.pop(str(dest), 0)


def download(ref: RepoRef, commit: str, dest: Path, submodules: bool = True) -> Path:
    """Fetch and safely extract one commit into dest (replaced), plus its git submodules that
    live on GitHub (tarballs leave them out). Returns dest."""
    _extract(ref, commit, dest)
    if submodules and (dest / ".gitmodules").is_file():
        _submodules(ref, commit, dest, depth=0, budget=[MAX_EXTRACTED * 3])
    return dest


def _submodules(ref: RepoRef, commit: str, dest: Path, depth: int, budget: list) -> None:
    text = (dest / ".gitmodules").read_text(encoding="utf-8", errors="replace")
    for section in re.split(r"(?m)^\s*\[submodule\b", text)[1:]:
        path = re.search(r"(?m)^\s*path\s*=\s*(\S+)", section)
        url = re.search(r"(?m)^\s*url\s*=\s*(\S+)", section)
        if not path or not url:
            continue
        path, url = path.group(1), url.group(1)
        rel = Path(path)
        if rel.is_absolute() or ".." in rel.parts:
            continue
        if url.startswith("../"):  # relative to the parent repo's owner
            url = f"https://github.com/{ref.owner}/{url[3:]}"
        m = re.match(rf"^(?:https://|git@|git://)github\.com[:/]({OWNER_RE})/({REPO_RE}?)(?:\.git)?/?$", url)
        if not m:
            raise ImportFailure("unsupported_dependency", f"Submodule '{path}' isn't hosted on GitHub ({url[:120]}); "
                                                          f"Cognex can only fetch GitHub submodules.", submodule=path)
        try:
            meta = json.loads(_get(f"https://api.github.com/repos/{ref.full_name}/contents/{path}?ref={commit}",
                                   1024 * 1024, timeout=30))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ImportFailure):
            raise ImportFailure("github_unavailable", f"GitHub didn't say which commit submodule '{path}' is "
                                                      f"pinned to (the API may be rate limited); try again later.",
                                submodule=path) from None
        sha = meta.get("sha") if isinstance(meta, dict) and meta.get("type") == "submodule" else None
        if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        sub = RepoRef(m.group(1), m.group(2), None)
        target = dest / rel
        _extract(sub, sha, target, budget)
        if depth < 2 and (target / ".gitmodules").is_file():
            _submodules(sub, sha, target, depth + 1, budget)


def _extract(ref: RepoRef, commit: str, dest: Path, budget: Optional[list] = None) -> None:
    try:
        blob = _get(f"https://codeload.github.com/{ref.full_name}/tar.gz/{commit}", MAX_DOWNLOAD, timeout=120)
    except urllib.error.HTTPError as exc:
        raise ImportFailure("repo_not_found", f"Commit {commit[:12]} of {ref.url} couldn't be downloaded "
                                              f"(HTTP {exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ImportFailure("github_unavailable", "GitHub couldn't be reached; try again shortly.") from None
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    DOWNLOADED[str(dest)] = DOWNLOADED.get(str(dest), 0) + len(blob) if budget is not None else len(blob)
    total = count = 0
    limit = MAX_EXTRACTED if budget is None else min(MAX_EXTRACTED, budget[0])
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar:
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1] or not member.isreg():
                continue  # the top-level folder, directories (made below), links, devices
            count += 1
            total += member.size
            if count > MAX_FILES:
                raise ImportFailure("repo_too_large", f"The repository has more than {MAX_FILES} files.")
            if total > limit:
                raise ImportFailure("repo_too_large", f"The repository (with its submodules) is larger than "
                                                      f"{MAX_EXTRACTED // (1024 * 1024)} MB.")
            rel = Path(parts[1])
            if rel.is_absolute() or ".." in rel.parts:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is not None:
                with open(target, "wb") as fh:
                    shutil.copyfileobj(src, fh)
                try:  # keep the executable bit (configure scripts, build helpers); nothing else
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
                except OSError:
                    pass
    if budget is not None:
        budget[0] -= total
