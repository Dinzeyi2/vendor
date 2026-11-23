"""Public GitHub repositories: URL validation, commit pinning and size-limited download.

Trusted code. Talks only to github.com (git smart-HTTP ref advertisement, no API token or API
rate limit) and codeload.github.com (the tarball of one exact commit). Never runs repo code.
"""
from __future__ import annotations

import io
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
MAX_DOWNLOAD = 25 * 1024 * 1024        # compressed tarball
MAX_EXTRACTED = 100 * 1024 * 1024      # total size of extracted files
MAX_FILES = 5000
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
        raise ImportFailure("repo_too_large", f"The repository download is larger than {limit // (1024 * 1024)} MB.")
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


def resolve_commit(ref: RepoRef) -> str:
    """The exact commit SHA for the default branch (or the given branch/tag) of a public repo."""
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


def download(ref: RepoRef, commit: str, dest: Path) -> Path:
    """Fetch and safely extract one commit into dest (replaced). Returns dest."""
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
    total = count = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar:
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1] or not member.isreg():
                continue  # the top-level folder, directories (made below), links, devices
            count += 1
            total += member.size
            if count > MAX_FILES:
                raise ImportFailure("repo_too_large", f"The repository has more than {MAX_FILES} files.")
            if total > MAX_EXTRACTED:
                raise ImportFailure("repo_too_large", f"The repository is larger than {MAX_EXTRACTED // (1024 * 1024)} MB.")
            rel = Path(parts[1])
            if rel.is_absolute() or ".." in rel.parts:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is not None:
                with open(target, "wb") as fh:
                    shutil.copyfileobj(src, fh)
    return dest
