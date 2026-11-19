"""Conservative, source-aware updates for the user's global Agent Skills.

Only this module's fixed source bindings are eligible for updates.  Repository
content is read as data: no downloaded installer, setup script, hook or skill
instruction is executed.  Existing Claude plugin caches are changed only by the
official Claude CLI, after the portable copies have been committed.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable


_SHA = re.compile(r"^[0-9a-f]{40}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\r\n]+)\)")
_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.S)
_METADATA = {"source.json", "parity-source.json"}
_BOUND = {"built-in-browser", "chrome-browser", "computer-use", "docs", "import-memory"}
_SYNCED = {"docx", "pdf", "pptx", "xlsx", "deep-research", "google-workspace", "morning", "skill-creator"}
_BINDINGS = {
    "ecc": ("affaan-m/ECC", "release", "ECC"),
    "ponytail": ("DietrichGebert/ponytail", "release", "Ponytail"),
    "impeccable": ("pbakaus/impeccable", "skill-release", "Impeccable"),
    "hallmark": ("Nutlope/hallmark", "release", "Hallmark"),
    "superpowers": ("obra/superpowers", "release", "Selected Superpowers skills"),
    "awesome-design-md": ("voltagent/awesome-design-md", "head", "Awesome Design MD"),
    "anthropic-examples": ("anthropics/skills", "head", "Anthropic design and document skills"),
    "chrome-devtools": ("ChromeDevTools/chrome-devtools-mcp", "release", "Chrome DevTools skills"),
    "code-review": ("anthropics/claude-plugins-official", "head", "Portable code review"),
}
_REPO_KEYS = {v[0].lower(): k for k, v in _BINDINGS.items()}
_REPO_KEYS["affaan-m/everything-claude-code"] = "ecc"
_EXTRAS = {
    "frontend-design": "anthropic-examples", "doc-coauthoring": "anthropic-examples",
    "cookie-debugging": "chrome-devtools", "debug-optimize-lcp": "chrome-devtools",
}


class UpdateError(Exception):
    """An update cannot safely be performed."""


class Conflict(UpdateError):
    """Local content must be preserved rather than overwritten."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, default=None):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else ({} if default is None else default)
    except (OSError, ValueError, UnicodeError):
        return {} if default is None else default


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _relative(value: str) -> str:
    value = str(value).replace("\\", "/")
    p = PurePosixPath(value)
    if not value or value.startswith("/") or any(x in {"", ".", ".."} for x in value.split("/")):
        raise UpdateError("An upstream file has an unsafe path.")
    if any(re.search(r'[<>:"|?*\x00-\x1f]', x) for x in p.parts):
        raise UpdateError("An upstream file name is not portable.")
    if any(x.endswith((".", " ")) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", x) for x in p.parts):
        raise UpdateError("An upstream file name is reserved on Windows.")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{v}{n}" for v in ["COM", "LPT"] for n in range(1, 10)}
    if any(x.endswith((".", " ")) or x.split(".")[0].upper() in reserved for x in p.parts):
        raise UpdateError("An upstream file name is reserved by Windows.")
    return p.as_posix()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_link(path: Path) -> bool:
    """Include Windows junctions when running on Python 3.11."""
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _files(root: Path) -> dict[str, str]:
    result = {}
    if not root.is_dir():
        return result
    for folder, dirs, files in os.walk(root, followlinks=False):
        here = Path(folder)
        for name in dirs:
            if _is_link(here / name):
                raise Conflict("A managed skill contains a directory link; it was preserved.")
        for name in files:
            p = here / name
            if _is_link(p):
                raise Conflict("A managed skill contains a file link; it was preserved.")
            result[p.relative_to(root).as_posix()] = _hash(p.read_bytes())
    return result


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _name(content: str, fallback="") -> str:
    front = _FRONT.match(content)
    match = re.search(r"(?m)^name:\s*([^\r\n]+)", front[1] if front else "")
    name = match[1].strip().strip("\"'") if match else fallback
    if not _NAME.fullmatch(name):
        raise UpdateError("A skill has an invalid or missing name.")
    return name


def _validate_tree(path: Path, expected: str | None = None, resource=False) -> None:
    entries = list(path.rglob("SKILL.md"))
    if resource:
        if entries:
            raise UpdateError("A support directory would expose an unintended skill.")
        return
    if entries != [path / "SKILL.md"]:
        raise UpdateError("A skill must have exactly one discoverable entrypoint.")
    content = _text(path / "SKILL.md")
    if expected and _name(content) != expected:
        raise UpdateError("A staged skill name does not match its installed name.")
    front = _FRONT.match(content)
    if not front or not re.search(r"(?m)^description:\s*\S", front[1]):
        raise UpdateError("A staged skill has incomplete frontmatter.")
    _files(path)  # Also rejects linked files and directories.


def _semver(value: str):
    match = re.fullmatch(r"(?:v|skill-v)?(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else None


def _repo_key(url: str) -> str | None:
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/#?]+?)(?:\.git)?/?", str(url), re.I)
    return _REPO_KEYS.get(match[1].lower()) if match else None


class _CredentialSafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        if target.scheme != "https":
            raise UpdateError("A source redirected to an insecure endpoint.")
        authenticated = any(name.lower() == "authorization" for name, _ in request.header_items())
        if authenticated and (target.hostname != "api.github.com" or target.port not in {None, 443} or target.username or target.password):
            raise UpdateError("An authenticated metadata request redirected outside GitHub's API; it was stopped.")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class _Network:
    """Public, allowlisted HTTPS requests with conditional metadata caching."""

    def __init__(self, cache: Path):
        self.cache = cache
        self._opener = urllib.request.build_opener(_CredentialSafeRedirect())
        self._github_token = None
        self._github_token_loaded = False
        self._github_auth_rejected = False

    def _existing_github_token(self):
        """Read an existing CLI login into memory only after a public rate limit."""
        if self._github_token_loaded or self._github_auth_rejected:
            return self._github_token
        self._github_token_loaded = True
        command = shutil.which("gh")
        if not command or Path(command).suffix.lower() in {".cmd", ".bat"}:
            return None
        try:
            completed = subprocess.run([command, "auth", "token", "--hostname", "github.com"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       timeout=10, text=True, encoding="utf-8", errors="replace",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            token = completed.stdout.strip() if completed.returncode == 0 else ""
            if re.fullmatch(r"[A-Za-z0-9_]{20,255}", token):
                self._github_token = token
        except (OSError, subprocess.TimeoutExpired):
            pass
        return self._github_token

    def _open(self, url, headers=None):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "api.github.com", "raw.githubusercontent.com", "github.com",
        } or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise UpdateError("This source URL is not an approved public endpoint.")
        # Caller-supplied headers cannot attach credentials to downloads. Only
        # metadata requests to the exact GitHub API host receive the CLI login.
        clean_headers = {k: v for k, v in (headers or {}).items() if k.lower() != "authorization"}

        def send(token=None):
            values = {"User-Agent": "AgentInventoryManager/1.0", **clean_headers}
            if token and parsed.hostname == "api.github.com":
                values["Authorization"] = "Bearer " + token
            request = urllib.request.Request(url, headers=values)
            return self._opener.open(request, timeout=30)

        token = self._github_token if parsed.hostname == "api.github.com" and not self._github_auth_rejected else None
        try:
            return send(token)
        except urllib.error.HTTPError as exc:
            if token and exc.code == 401:
                exc.close()
                self._github_token, self._github_auth_rejected = None, True
                return send()
            limited = (parsed.hostname == "api.github.com" and not token and exc.code == 403
                       and exc.headers.get("X-RateLimit-Remaining") == "0")
            if not limited:
                raise
            token = self._existing_github_token()
            if not token:
                raise
            exc.close()
        try:
            return send(token)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            exc.close()
            self._github_token, self._github_auth_rejected = None, True
            return send()

    def bytes(self, url: str, limit=128 * 1024 * 1024) -> bytes:
        try:
            with self._open(url) as response:
                parts, length = [], 0
                while chunk := response.read(128 * 1024):
                    length += len(chunk)
                    if length > limit:
                        raise UpdateError("An upstream download exceeded its size limit.")
                    parts.append(chunk)
                return b"".join(parts)
        except urllib.error.HTTPError as exc:
            raise UpdateError(f"Public source request failed (HTTP {exc.code}).") from None
        except (OSError, urllib.error.URLError):
            raise UpdateError("The public source could not be reached.") from None

    def json(self, url: str):
        path = self.cache / "metadata" / (_hash(url.encode()) + ".json")
        cached = _read_json(path)
        headers = {"Accept": "application/vnd.github+json"}
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        try:
            with self._open(url, headers) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise UpdateError("Upstream metadata exceeded its size limit.")
                value = json.loads(raw)
                _write_json(path, {"url": url, "etag": response.headers.get("ETag"), "checkedAt": _now(), "value": value})
                return value
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and "value" in cached:
                cached["checkedAt"] = _now()
                _write_json(path, cached)
                return cached["value"]
            raise UpdateError(f"Version metadata unavailable (HTTP {exc.code}).") from None
        except (OSError, ValueError, urllib.error.URLError):
            raise UpdateError("Version metadata could not be read; no update was assumed.") from None


class _Snapshot:
    """A pinned Git tree downloaded on demand, not a repository installation."""

    def __init__(self, network: _Network, repo: str, commit: str, cache: Path, seeds=()):
        if not _SHA.fullmatch(commit):
            raise UpdateError("Upstream did not resolve to a full commit.")
        self.network, self.repo, self.commit, self.cache = network, repo, commit, cache
        tree = network.json(f"https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1")
        if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
            raise UpdateError("The publisher returned invalid tree metadata.")
        if tree.get("truncated"):
            raise UpdateError("The upstream tree is incomplete; update was not staged.")
        self.tree = {}
        folded = set()
        for entry in tree.get("tree", []):
            if entry.get("type") == "blob":
                relative = _relative(entry["path"])
                if relative.casefold() in folded:
                    raise UpdateError("Upstream contains file names that collide on Windows.")
                folded.add(relative.casefold())
                self.tree[relative] = entry
        self.seeds = [Path(p) for p in seeds if Path(p).is_dir()]

    def paths(self, prefix="") -> list[str]:
        return sorted(p for p in self.tree if not prefix or p.startswith(prefix.rstrip("/") + "/"))

    def has(self, path: str) -> bool:
        return path in self.tree

    @staticmethod
    def _blob(data: bytes) -> str:
        return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()

    def read(self, path: str) -> bytes:
        path = _relative(path)
        entry = self.tree.get(path)
        if not entry or entry.get("mode") not in {"100644", "100755"}:
            raise UpdateError("A required upstream file is missing or is a linked file.")
        if entry.get("size", 0) > 64 * 1024 * 1024:
            raise UpdateError("A source file exceeded its size limit.")
        blob = entry.get("sha", "")
        if not _SHA.fullmatch(blob):
            raise UpdateError("Invalid upstream content identity.")
        cached = self.cache / "objects" / blob[:2] / blob
        if cached.is_file():
            data = cached.read_bytes()
            if self._blob(data) == blob:
                return data
        for seed in self.seeds:
            p = seed / path
            if p.is_file() and _within(p, seed):
                data = p.read_bytes()
                if self._blob(data) == blob:
                    self._store(cached, data)
                    return data
        url = f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/{urllib.parse.quote(path, safe='/')}"
        data = self.network.bytes(url, limit=64 * 1024 * 1024)
        if self._blob(data) != blob:
            raise UpdateError("An upstream file did not match the pinned Git object.")
        self._store(cached, data)
        return data

    @staticmethod
    def _store(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass
class _Target:
    name: str
    path: Path
    metadata: dict = field(default_factory=dict)
    expected: dict[str, str] = field(default_factory=dict)
    apps: list[str] = field(default_factory=list)
    source_path: str = ""


@dataclass
class _Bundle:
    key: str
    targets: list[_Target] = field(default_factory=list)
    plugin: dict | None = None
    managed: bool = True

    @property
    def id(self):
        return "skill:" + self.key

    @property
    def repo(self):
        return _BINDINGS[self.key][0] if self.key in _BINDINGS else ""

    @property
    def title(self):
        return _BINDINGS[self.key][2] if self.key in _BINDINGS else {
            "claude-synced": "Claude-published portable skills", "codex-system": "Codex system skills",
            "installer-owned": "Agent-specific managed installations",
            "claude-native": "Claude-native skills", "local": "Other local skills",
        }.get(self.key, self.key)


class SkillUpdater:
    def __init__(self, home: Path, state_dir: Path, log=None):
        self.home, self.state_dir, self.log = Path(home).resolve(), Path(state_dir).resolve(), log
        self.shared = self.home / ".agents" / "skills"
        self.records = self.home / ".agents" / "install-records"
        self.state_file = self.state_dir / "skill-updates-state.json"
        self.cache = self.state_dir / "skill-cache"
        self.network = _Network(self.cache)
        self._lock = threading.RLock()
        self._rows_cache = None
        self._state = _read_json(self.state_file, {"format": 1, "bundles": {}})
        self._state.setdefault("bundles", {})

    def _emit(self, progress, message):
        # A disconnected UI must not interrupt an otherwise atomic update.
        with contextlib.suppress(Exception):
            if progress:
                progress(message)
        with contextlib.suppress(Exception):
            if callable(self.log):
                self.log(message)
            elif self.log and hasattr(self.log, "info"):
                self.log.info(message)

    def _save(self):
        _write_json(self.state_file, self._state)

    @staticmethod
    def _hash_entries(entries):
        if isinstance(entries, dict):
            entries = [{"path": k, "sha256": v} for k, v in entries.items()]
        result = {}
        for row in entries or []:
            if not isinstance(row, dict):
                continue
            value = row.get("sha256", "")
            try:
                path = _relative(row.get("path", ""))
            except UpdateError:
                continue
            if re.fullmatch(r"[0-9a-fA-F]{64}", str(value)):
                result[path] = value.lower()
        return result

    def _provenance(self):
        parity = _read_json(self.records / "skill-parity-2026-11-19.json")
        extras = {x.get("name"): x for x in parity.get("personalExtras", {}).get("skills", [])}
        records = {
            "hallmark": _read_json(self.records / "hallmark-2026-11-19.json"),
            "code-review": _read_json(self.records / "code-review-2026-11-19.json"),
            "awesome-design-md": _read_json(self.records / "awesome-design-md-2026-11-19.json"),
        }
        return parity, extras, records

    def _catalog(self):
        parity, extras, records = self._provenance()
        bundles: dict[str, _Bundle] = {}
        rows = []
        seen = set()

        def add(path, apps, native=False):
            entry = path / "SKILL.md"
            if not entry.is_file():
                return
            identity = str(path.resolve())
            if identity in seen:
                for existing in rows:
                    if existing.get("_identity") == identity:
                        existing["apps"] = sorted(set(existing["apps"] + apps))
                for bundle in bundles.values():
                    for target in bundle.targets:
                        if str(target.path.resolve()) == identity:
                            target.apps = sorted(set(target.apps + apps))
                return
            seen.add(identity)
            try:
                name = _name(_text(entry), path.name)
            except (OSError, UnicodeError, UpdateError):
                return
            metadata = _read_json(path / "source.json") or _read_json(path / "parity-source.json")
            record = records.get(name, {})
            key = _repo_key(metadata.get("source", metadata.get("repository", "")))
            if not key and name == "hallmark" and _repo_key(record.get("repository", "")) == "hallmark":
                key = "hallmark"
            if not key and name in _EXTRAS and name in extras:
                key = _repo_key(extras[name].get("repository", ""))
            if metadata.get("source_directory") and name in _SYNCED:
                key = "claude-synced"
            if native:
                key = "codex-system"
            if metadata.get("managedBy") == "mcp-and-skills":
                key = "installer-owned"
            if name in _BOUND and "Claude" in apps:
                key = "claude-native"
            key = key or "local"
            bundle = bundles.setdefault(key, _Bundle(key, managed=key in _BINDINGS or key == "claude-synced"))
            expected = self._hash_entries(metadata.get("installedFiles"))
            expected.update(self._hash_entries(metadata.get("upstream_files_sha256")))
            expected.update(self._hash_entries(record.get("files_sha256")))
            if record.get("skill_sha256"):
                expected["SKILL.md"] = record["skill_sha256"]
            if metadata.get("installed_entrypoint_sha256"):
                expected["SKILL.md"] = metadata["installed_entrypoint_sha256"]
            if name in extras:
                expected.update(self._hash_entries(extras[name].get("files_sha256")))
                for notice in parity.get("personalExtraNotices", []):
                    if notice.get("skill") == name and Path(notice.get("path", "")).parent.resolve() == path.resolve():
                        expected["LICENSE"] = notice["sha256"]
            if name == "code-review":
                if metadata.get("reference_sha256"):
                    expected["references/anthropic-code-review.md"] = metadata["reference_sha256"]
                if metadata.get("license_sha256"):
                    expected["LICENSE"] = metadata["license_sha256"]
            if not metadata.get("commit"):
                fallback = extras.get(name, record)
                if fallback.get("commit"):
                    metadata = {**fallback, **metadata, "commit": fallback["commit"]}
            source_path = metadata.get("upstreamPath", metadata.get("source_path", ""))
            if not source_path and key == "hallmark":
                source_path = record.get("source_path", "skills/hallmark")
            target = _Target(name, path, metadata, expected, list(apps), source_path)
            bundle.targets.append(target)
            version = str(metadata.get("version", metadata.get("commit", "")))
            rows.append(dict(name=name, sourceId=bundle.id, sourceName=bundle.title, version=version,
                             apps=list(apps), state="installed" if bundle.managed else "host-managed",
                             kind="skill", managed=bundle.managed, _identity=identity))

        for path in sorted(self.shared.glob("*/SKILL.md")):
            if not path.parent.name.startswith("."):
                add(path.parent, ["Codex", "Gemini"])
        for path in sorted((self.home / ".gemini" / "skills").glob("*/SKILL.md")):
            add(path.parent, ["Gemini"])
        for path in sorted((self.home / ".codex" / "skills").glob("*/SKILL.md")):
            if not path.parent.name.startswith("."):
                add(path.parent, ["Codex"])
        for path in sorted((self.home / ".claude" / "skills").glob("*/SKILL.md")):
            add(path.parent, ["Claude"])
        synced = self.home / ".claude" / "skills" / "synced"
        for path in sorted(synced.glob("*/*/SKILL.md")):
            if any(p in {".trash", ".staging"} for p in path.parts):
                continue
            # Publisher copies are shown, but never treated as writable targets.
            name = _name(_text(path), path.parent.name)
            key = "claude-synced" if name in _SYNCED else "claude-native"
            bundle = bundles.setdefault(key, _Bundle(key, managed=key == "claude-synced"))
            if key == "claude-native":
                bundle.targets.append(_Target(name, path.parent, apps=["Claude"]))
            rows.append(dict(name=name, sourceId=bundle.id, sourceName=bundle.title, version="publisher-managed",
                             apps=["Claude"], state="host-managed", kind="skill", managed=False))
        for path in sorted((self.home / ".codex" / "skills" / ".system").glob("*/SKILL.md")):
            add(path.parent, ["Codex"], native=True)
        registry = _read_json(self.home / ".claude" / "plugins" / "installed_plugins.json")
        enabled = _read_json(self.home / ".claude" / "settings.json").get("enabledPlugins", {})
        for plugin_id, key in [("ecc@ecc", "ecc"), ("ponytail@ponytail", "ponytail")]:
            if not enabled.get(plugin_id):
                continue
            for installed in registry.get("plugins", {}).get(plugin_id, []):
                if installed.get("scope", "user") != "user":
                    continue
                root = Path(installed.get("installPath", ""))
                if not _within(root, self.home / ".claude" / "plugins"):
                    continue
                bundle = bundles.setdefault(key, _Bundle(key))
                bundle.plugin = {"id": plugin_id, "version": installed.get("version", ""),
                                 "commit": installed.get("gitCommitSha", ""), "path": str(root)}
                for path in sorted((root / "skills").glob("*/SKILL.md")):
                    rows.append(dict(name=_name(_text(path), path.parent.name), sourceId=bundle.id,
                                     sourceName=bundle.title, version=str(installed.get("version", "")),
                                     apps=["Claude"], state="enabled", kind="skill", managed=True))
        for row in rows:
            row.pop("_identity", None)
        return bundles, rows

    def discover(self):
        # Version checks and package downloads may take minutes. Keep the UI's
        # last complete inventory readable while the update lock is held.
        if not self._lock.acquire(blocking=False):
            if self._rows_cache is not None:
                return copy.deepcopy(self._rows_cache)
            return self._catalog()[1]
        try:
            self._rows_cache = self._catalog()[1]
            return copy.deepcopy(self._rows_cache)
        finally:
            self._lock.release()

    def _resource_targets(self, bundle):
        paths = {}
        for target in bundle.targets:
            value = target.metadata.get("resourceRoot")
            if value:
                p = Path(value)
                if _within(p, self.shared) and p.name.startswith(".") and p.is_dir():
                    paths[str(p)] = p
        parity = _read_json(self.records / "skill-parity-2026-11-19.json")
        result = []
        for p in paths.values():
            expected = {}
            for row in parity.get("resources", []):
                source = Path(row.get("path", ""))
                if _within(source, p):
                    expected[source.relative_to(p).as_posix()] = row.get("installedSha256", "")
            result.append(_Target("@resources", p, expected=expected))
        return result

    def _record(self, bundle):
        return self._state["bundles"].setdefault(bundle.id, {})

    def _enroll(self, bundle):
        record = self._record(bundle)
        baselines = record.setdefault("baseline", {})
        for target in bundle.targets + self._resource_targets(bundle):
            key = str(target.path)
            if key in baselines:
                continue
            actual = _files(target.path)
            expected = dict(actual)
            known = dict(target.expected)
            # A synced publisher's unmodified supporting files can be enrolled.
            if bundle.key == "claude-synced":
                source = self._publisher(target)
                for relative, value in _files(source).items():
                    if actual.get(relative) == value:
                        known.setdefault(relative, value)
            expected.update(known)
            baselines[key] = {"files": expected, "managed": sorted(known), "enrolledAt": _now()}
        return record

    def _conflicts(self, bundle):
        record = self._enroll(bundle)
        changed = []
        for path, baseline in record["baseline"].items():
            actual = _files(Path(path))
            if actual != baseline["files"]:
                changed.append(path)
        return changed

    def _publisher(self, target):
        base = self.home / ".claude" / "skills" / "synced"
        source = Path(target.metadata.get("source_directory", ""))
        if _within(source, base) and (source / "SKILL.md").is_file():
            return source
        candidates = [p.parent for p in base.glob(f"*/{target.name}/SKILL.md")
                      if not any(x in {".trash", ".staging"} for x in p.parts)]
        if len(candidates) != 1:
            raise UpdateError("The local publisher source is missing or ambiguous.")
        return candidates[0]

    def _local_version(self, bundle):
        signatures = []
        for target in bundle.targets:
            source = self._publisher(target)
            signatures.append((target.name, _files(source)))
        return _hash(json.dumps(signatures, sort_keys=True).encode())

    def _initial_local_version(self, bundle):
        """Infer the installed publisher snapshot, not today's possibly newer one."""
        signatures = []
        for target in bundle.targets:
            source = self._publisher(target)
            hashes = {}
            for relative in _files(source):
                installed = target.path / relative
                if installed.is_file() and relative not in _METADATA:
                    hashes[relative] = _hash(installed.read_bytes())
            original = target.metadata.get("original_entrypoint_sha256")
            if original:
                hashes["SKILL.md"] = original
            signatures.append((target.name, hashes))
        return _hash(json.dumps(signatures, sort_keys=True).encode())

    def _latest(self, bundle):
        if bundle.key == "claude-synced":
            value = self._local_version(bundle)
            return {"commit": value, "version": "local-" + value[:12], "checkedAt": _now()}
        repo, policy, _ = _BINDINGS[bundle.key]
        prefix = f"https://api.github.com/repos/{repo}"
        tag = None
        if policy == "skill-release":
            refs = self.network.json(prefix + "/git/matching-refs/tags/skill-v")
            if not isinstance(refs, list):
                raise UpdateError("The publisher returned invalid release metadata.")
            tags = [x.get("ref", "").removeprefix("refs/tags/") for x in refs]
            tags = [x for x in tags if x.startswith("skill-v") and _semver(x)]
            if tags:
                tag = max(tags, key=_semver)
        elif policy == "release":
            releases = self.network.json(prefix + "/releases?per_page=100")
            if not isinstance(releases, list):
                raise UpdateError("The publisher returned invalid release metadata.")
            tags = [x.get("tag_name", "") for x in releases
                    if not x.get("draft") and not x.get("prerelease") and _semver(x.get("tag_name", ""))]
            if tags:
                tag = max(tags, key=_semver)
        revision = self.network.json(prefix + "/commits/" + urllib.parse.quote(tag or "HEAD", safe=""))
        if not isinstance(revision, dict):
            raise UpdateError("The publisher returned invalid commit metadata.")
        commit = revision.get("sha", "")
        if not _SHA.fullmatch(commit):
            raise UpdateError("The publisher did not return a valid commit.")
        # A previously installed marketplace HEAD can be newer than the latest
        # tagged release. Following release policy must never roll that back.
        current, current_version = self._installed(bundle)
        if tag and _SHA.fullmatch(current) and current != commit:
            comparison = self.network.json(prefix + "/compare/" + current + "..." + commit)
            relation = comparison.get("status") if isinstance(comparison, dict) else None
            if relation == "behind":
                return {"commit": current, "version": current_version or current[:12],
                        "checkedAt": _now(), "aheadOfRelease": tag}
            if relation not in {"ahead", "identical"}:
                raise Conflict("The installed revision and the latest release have diverged; the installed source was preserved.")
        return {"commit": commit, "version": tag or commit[:12], "checkedAt": _now()}

    def _installed(self, bundle):
        record = self._record(bundle)
        if record.get("installedCommit"):
            return record["installedCommit"], record.get("installedVersion", "")
        commits = {t.metadata.get("commit") for t in bundle.targets if t.metadata.get("commit")}
        versions = {str(t.metadata.get("version", "")) for t in bundle.targets if t.metadata.get("version")}
        if bundle.key == "claude-synced":
            old = record.setdefault("publisherBaseline", self._initial_local_version(bundle))
            return old, "local-" + old[:12]
        return (next(iter(commits)) if len(commits) == 1 else "mixed"), (next(iter(versions)) if len(versions) == 1 else (next(iter(commits))[:12] if len(commits) == 1 else "mixed"))

    def _unit(self, bundle, status, latest=None, detail=""):
        try:
            commit, version = self._installed(bundle) if bundle.managed else ("", "provider-managed")
        except (UpdateError, OSError, ValueError):
            commit, version = "", "unknown"
        return dict(id=bundle.id, name=bundle.title, kind="skill", installedVersion=version,
                    latestVersion=(latest or {}).get("version", "provider-managed" if not bundle.managed else "unknown"),
                    status=status, itemCount=len({t.name for t in bundle.targets}),
                    sourceUrl=("https://github.com/" + bundle.repo) if bundle.repo else "",
                    detail=detail, targetNames=sorted({t.name for t in bundle.targets}))

    def check_all(self, progress=None):
        with self._lock:
            bundles, _ = self._catalog()
            units = []
            for bundle in bundles.values():
                if not bundle.managed or not bundle.targets:
                    units.append(self._unit(bundle, "host-managed", detail="Updated by its publisher or agent application; no local replacement is attempted."))
                    continue
                self._emit(progress, "Checking " + bundle.title)
                try:
                    conflicts = self._conflicts(bundle)
                    latest = self._latest(bundle)
                    record = self._record(bundle)
                    record["latest"] = latest
                    current, _ = self._installed(bundle)
                    plugin_stale = self._plugin_stale(bundle, latest)
                    status = "local-changes" if conflicts else ("update-available" if current != latest["commit"] or plugin_stale else "up-to-date")
                    detail = ("Recorded local content has changed; nothing will be overwritten." if conflicts else
                              "A publisher update is available for this source bundle." if status == "update-available" else
                              "Installed source matches the latest publisher revision.")
                    if latest.get("aheadOfRelease") and not conflicts:
                        detail = "The installed publisher revision is newer than the latest tagged release; it was retained."
                    if bundle.key == "claude-synced":
                        detail = "Local publisher source checked; " + ("portable copies can be refreshed." if status == "update-available" else detail)
                    units.append(self._unit(bundle, status, latest, detail))
                except (UpdateError, OSError, ValueError) as exc:
                    units.append(self._unit(bundle, "error", detail=self._safe_error(exc)))
            self._state["checkedAt"] = _now()
            self._save()
            return units

    @staticmethod
    def _safe_error(exc):
        if isinstance(exc, UpdateError):
            return str(exc)[:400]
        return "The installed source could not be read safely; no update was performed."

    @staticmethod
    def _plugin_stale(bundle, latest):
        plugin = bundle.plugin
        if not plugin:
            return False
        wanted, current = _semver(latest.get("version", "")), _semver(str(plugin.get("version", "")))
        return current != wanted if wanted else plugin.get("commit") != latest.get("commit")

    def _snapshot(self, bundle, latest):
        seeds = []
        for target in bundle.targets:
            value = target.metadata.get("sourcePath")
            if value:
                p = Path(value)
                if _within(p, self.home / ".claude" / "plugins"):
                    seeds.append(p.parent.parent)
        if bundle.plugin:
            seeds.append(Path(bundle.plugin["path"]))
        return _Snapshot(self.network, bundle.repo, latest["commit"], self.cache, seeds)

    def _target_stage(self, target, stage_root, ordinal, bundle):
        path = stage_root / f"target-{ordinal}"
        if target.path.exists():
            shutil.copytree(target.path, path)
        else:
            path.mkdir()
        baseline = self._record(bundle).get("baseline", {}).get(str(target.path), {"files": {}, "managed": []})
        return _Stage(target, path, baseline)

    def _build(self, bundle, latest, stage_root, progress):
        stages = []
        snapshot = None if bundle.key == "claude-synced" else self._snapshot(bundle, latest)
        if bundle.key in {"ecc", "ponytail"}:
            return self._build_library(bundle, latest, snapshot, stage_root, progress)
        for target in bundle.targets:
            self._emit(progress, "Staging " + target.name)
            stage = self._target_stage(target, stage_root, len(stages), bundle)
            stages.append(stage)
            if bundle.key == "claude-synced":
                source = self._publisher(target)
                upstream = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
                upstream.pop("source.json", None)
                self._copy_upstream(stage, upstream, preserve_adapter=True)
            elif bundle.key == "awesome-design-md":
                self._build_design_library(stage, snapshot)
            elif bundle.key == "code-review":
                relative = target.source_path or "plugins/code-review/commands/code-review.md"
                stage.write("references/anthropic-code-review.md", snapshot.read(relative))
                license_path = "plugins/code-review/LICENSE"
                if not snapshot.has(license_path):
                    license_path = "LICENSE"
                if snapshot.has(license_path):
                    stage.write("LICENSE", snapshot.read(license_path))
            else:
                folder = target.source_path or (".agents/skills/impeccable" if bundle.key == "impeccable" else "skills/" + target.name)
                paths = snapshot.paths(folder)
                if not paths:
                    raise UpdateError("The installed skill's publisher folder is missing.")
                upstream = {p[len(folder) + 1:]: snapshot.read(p) for p in paths}
                self._copy_upstream(stage, upstream, preserve_adapter=bundle.key == "superpowers")
                if snapshot.has("LICENSE") and "LICENSE" not in upstream:
                    stage.write("LICENSE", snapshot.read("LICENSE"))
                if bundle.key == "superpowers":
                    self._build_superpowers_support(stage, snapshot)
                if bundle.key == "impeccable":
                    if snapshot.has("NOTICE.md"):
                        stage.write("NOTICE.md", snapshot.read("NOTICE.md"))
                    self._build_engine(stage)
            self._metadata(stage, bundle, latest)
            _validate_tree(stage.path, target.name)
        return stages

    def _copy_upstream(self, stage, upstream, preserve_adapter=False):
        if "SKILL.md" not in upstream:
            raise UpdateError("A publisher skill has no entrypoint.")
        if _name(upstream["SKILL.md"].decode("utf-8-sig")) != stage.target.name:
            raise UpdateError("The publisher changed the installed skill's name; review is required.")
        for relative, data in upstream.items():
            if relative in _METADATA:
                continue
            if preserve_adapter and relative == "SKILL.md":
                prefix = _adapter_prefix(_text(stage.target.path / "SKILL.md"))
                data = _insert_prefix(data.decode("utf-8-sig"), prefix).encode("utf-8")
            stage.write(relative, data)

    def _build_superpowers_support(self, stage, snapshot):
        mapping = {
            "skills/writing-plans/SKILL.md": "writing-plans.md",
            "skills/test-driven-development/SKILL.md": "test-driven-development.md",
            "skills/verification-before-completion/SKILL.md": "verification-before-completion.md",
        }
        for source, name in mapping.items():
            stage.write("references/superpowers/" + name, snapshot.read(source))
            parent = source.rsplit("/", 1)[0]
            for p in snapshot.paths(parent):
                if p.endswith("/SKILL.md"):
                    continue
                stage.write("references/superpowers/" + p[len(parent) + 1:], snapshot.read(p))
        source = stage.target.source_path or "skills/" + stage.target.name
        stage.write("references/upstream-skill.md", snapshot.read(source + "/SKILL.md"))

    def _build_engine(self, stage):
        version_file, manifest_file = stage.path / "scripts/VERSION", stage.path / "scripts/engine-checksums.txt"
        if not version_file.is_file() or not manifest_file.is_file():
            return
        version = version_file.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise UpdateError("The publisher engine version is invalid.")
        system = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(platform.system())
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        if not system:
            raise UpdateError("No supported engine asset is available for this platform.")
        asset = f"impeccable-{system}-{arch}" + (".exe" if system == "windows" else "")
        lines = manifest_file.read_text(encoding="utf-8").splitlines()
        digest = next((p[2] for line in lines if len(p := line.split()) == 3 and p[:2] == [version, asset]), "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateError("The engine asset lacks a publisher-pinned checksum.")
        suffix = ".exe" if system == "windows" else ""
        relative = f"scripts/bin/{system}-{arch}/impeccable{suffix}"
        current = stage.path / relative
        if current.is_file() and _hash(current.read_bytes()) == digest:
            return
        data = self.network.bytes(f"https://github.com/pbakaus/impeccable/releases/download/engine-v{version}/{asset}")
        if _hash(data) != digest:
            raise UpdateError("The downloaded engine failed checksum verification.")
        stage.write(relative, data)
        if system != "windows":
            os.chmod(stage.path / relative, 0o755)

    def _build_design_library(self, stage, snapshot):
        selected = [p for p in snapshot.paths("design-md") if p.endswith(("/DESIGN.md", "/README.md"))]
        designs = [p for p in selected if p.endswith("/DESIGN.md")]
        if not designs:
            raise UpdateError("The publisher design library is empty.")
        for p in selected:
            stage.write("references/" + p, snapshot.read(p))
        for source, relative in [("README.md", "references/upstream-README.md"), ("LICENSE", "LICENSE")]:
            if snapshot.has(source):
                stage.write(relative, snapshot.read(source))
        # Retain the custom wrapper, including its scope and tool instructions.
        wrapper = _text(stage.path / "SKILL.md")
        wrapper = re.sub(r"bundles \d+ upstream DESIGN\.md analyses", f"bundles {len(designs)} upstream DESIGN.md analyses", wrapper)
        stage.write("SKILL.md", wrapper.encode("utf-8"))
        catalog = "# Current publisher design references\n\n" + f"{len(designs)} references at {snapshot.commit}.\n\n"
        for p in designs:
            slug = p.split("/")[1]
            catalog += f"- [{slug}](design-md/{slug}/DESIGN.md)\n"
        begin, end = "<!-- AgentInventoryManager:catalog:start -->", "<!-- AgentInventoryManager:catalog:end -->"
        previous = _text(stage.path / "references/catalog.md") if (stage.path / "references/catalog.md").is_file() else ""
        if begin in previous and end in previous:
            previous = previous.split(end, 1)[1].lstrip("\r\n")
        # No unrecorded/custom catalog text is discarded at initial enrollment.
        combined = begin + "\n" + catalog + "\n" + end + "\n\n" + previous
        stage.write("references/catalog.md", combined.encode("utf-8"), preserve_local=True)

    def _build_library(self, bundle, latest, snapshot, stage_root, progress):
        """Rebuild passive package resources and all installed library skills."""
        shared = self.shared
        old_resources = self._resource_targets(bundle)
        resource_path = shared / ("." + bundle.key + "-resources-" + latest["commit"][:16])
        if resource_path.exists():
            raise Conflict("This source's versioned support directory already exists; it was preserved.")
        source_resource = old_resources[0] if old_resources else _Target("@resources", resource_path)
        resources = self._target_stage(source_resource, stage_root, 0, bundle)
        resources.target = _Target("@resources", resource_path)
        resources.expected = None
        stages = [resources]
        existing = {t.metadata.get("upstreamName", t.name): t for t in bundle.targets}
        occupied = {p.parent.name for p in shared.glob("*/SKILL.md")}
        occupied.update(p.parent.name for p in (self.home / ".codex/skills/.system").glob("*/SKILL.md"))
        occupied.update(_SYNCED)
        folders = sorted({p.split("/")[1] for p in snapshot.paths("skills") if p.count("/") == 2 and p.endswith("/SKILL.md")})
        if not folders:
            raise UpdateError("The publisher skill library has no skill entrypoints.")
        missing = set(existing) - set(folders)
        if missing:
            raise Conflict("The publisher removed installed skills; manual review is required before deleting them.")
        skill_destinations = {}
        for folder in folders:
            upstream_name = _name(snapshot.read(f"skills/{folder}/SKILL.md").decode("utf-8-sig"))
            if folder in existing:
                target = existing[folder]
            else:
                name = upstream_name
                if name in occupied or (bundle.key == "ecc" and name == "deep-research"):
                    name = bundle.key + "-" + name
                if name in occupied:
                    raise Conflict("A new publisher skill collides with an existing global skill.")
                target = _Target(name, shared / name, source_path="skills/" + folder)
                occupied.add(name)
            skill_destinations[folder] = target
        resource_sources = {}
        seeds = []
        if bundle.key == "ecc":
            prefixes = ("agents/", "scripts/", "contexts/", "commands/", "config/", "schemas/", "manifests/", "scaffolds/", "workflows/", "src/", "legacy-command-shims/", "rules/")
            seeds.extend(p for p in snapshot.paths() if p.startswith(prefixes))
            seeds.extend(p for p in ["LICENSE", "README.md", "package.json", "package-lock.json", "hooks/README.md"] if snapshot.has(p))
        else:
            seeds.extend(p for p in ["LICENSE", "README.md", "benchmarks/results/2026-10-07-agentic.md"] if snapshot.has(p))

        def support_allowed(p):
            parts = PurePosixPath(p).parts
            return bool(parts and not any(x.startswith(".") for x in parts) and parts[0] not in {"skills", "tests", "mcp-configs", "node_modules"}
                        and (parts[0] != "hooks" or p == "hooks/README.md") and not PurePosixPath(p).name.startswith(".env"))

        def support_name(p):
            relative = PurePosixPath(p)
            if relative.name in {"SKILL.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md"}:
                relative = relative.with_name(relative.stem + ".reference.md")
            return relative.as_posix()

        def collect_links(p, content, queue):
            for match in _LINK.finditer(content):
                value = match[1].strip().strip("<>")
                if re.match(r"^[a-zA-Z][\w+.-]*:|^[/#]", value):
                    continue
                candidate = _normalize_repo_path(str(PurePosixPath(p).parent / value.partition("#")[0]))
                if candidate and support_allowed(candidate):
                    if snapshot.has(candidate):
                        queue.append(candidate)
                    elif snapshot.has(candidate.rstrip("/") + "/README.md"):
                        queue.append(candidate.rstrip("/") + "/README.md")

        queue = deque(p for p in seeds if support_allowed(p))
        skill_files = {}
        for folder in folders:
            self._emit(progress, "Reading " + bundle.title + ": " + folder)
            prefix = "skills/" + folder
            skill_files[folder] = {p: snapshot.read(p) for p in snapshot.paths(prefix)}
            for p, content in skill_files[folder].items():
                if p.endswith(".md"):
                    collect_links(p, content.decode("utf-8-sig"), queue)
        while queue:
            p = queue.popleft()
            if p in resource_sources or not snapshot.has(p) or not support_allowed(p):
                continue
            content = snapshot.read(p)
            resource_sources[p] = (support_name(p), content)
            if p.endswith(".md"):
                collect_links(p, content.decode("utf-8-sig"), queue)
        aliases = {folder: target.name for folder, target in skill_destinations.items() if folder != target.name}

        def mapped_link(source, destination, match):
            raw = match[1]
            link = raw.strip().strip("<>")
            if re.match(r"^[a-zA-Z][\w+.-]*:|^[/#]", link):
                return match[0]
            part, mark, anchor = link.partition("#")
            p = _normalize_repo_path(str(PurePosixPath(source).parent / part))
            if not p:
                return match[0]
            parts = PurePosixPath(p).parts
            final = None
            if len(parts) >= 3 and parts[0] == "skills" and parts[1] in skill_destinations:
                final = skill_destinations[parts[1]].path.joinpath(*parts[2:])
            elif p in resource_sources:
                final = resource_path / resource_sources[p][0]
            elif any(x.startswith(p.rstrip("/") + "/") for x in resource_sources):
                final = resource_path / p
            if final is not None:
                replacement = os.path.relpath(final, destination.parent).replace("\\", "/") + (mark + anchor if mark else "")
            elif snapshot.has(p):
                replacement = f"https://github.com/{bundle.repo}/blob/{latest['commit']}/{p}" + (mark + anchor if mark else "")
            else:
                return match[0]  # Preserve upstream examples and optional external checkout links.
            offset = match.start(1) - match.start()
            return match[0][:offset] + replacement + match[0][offset + len(raw):]

        def adapt(p, destination, content):
            if not p.endswith(".md"):
                return content
            result = _LINK.sub(lambda m: mapped_link(p, destination, m), content.decode("utf-8-sig"))
            for old, new in aliases.items():
                result = re.sub(r"(?<![\w/.:@-])" + re.escape(old) + r"(?![\w/-])", new, result)
            return result.encode("utf-8")

        for source, (relative, content) in resource_sources.items():
            resources.write(relative, adapt(source, resource_path / relative, content))
        _validate_tree(resources.path, resource=True)
        for folder, target in skill_destinations.items():
            stage = self._target_stage(target, stage_root, len(stages), bundle)
            stages.append(stage)
            prefix = "skills/" + folder + "/"
            for source, content in skill_files[folder].items():
                relative = source[len(prefix):]
                if relative in _METADATA:
                    continue
                content = adapt(source, target.path / relative, content)
                if relative == "SKILL.md":
                    old_prefix = _adapter_prefix(_text(target.path / "SKILL.md")) if (target.path / "SKILL.md").is_file() else ""
                    if old_prefix:
                        for previous in old_resources:
                            old_prefix = old_prefix.replace(str(previous.path).replace("\\", "/"), str(resource_path).replace("\\", "/"))
                        old_prefix = old_prefix.replace(bundle.title + " " + str(target.metadata.get("version", "")), bundle.title + " " + latest["version"])
                    else:
                        old_prefix = "\n## Standalone shared installation (local adapter)\n\nPassive package resources: " + resource_path.as_posix() + ". Use available host tools for named agent roles. No native agents, hooks, settings or bootstrap were installed.\n\n"
                    content = _insert_prefix(content.decode("utf-8-sig"), old_prefix).encode("utf-8")
                stage.write(relative, content)
            if snapshot.has("LICENSE"):
                stage.write("LICENSE", snapshot.read("LICENSE"))
            self._metadata(stage, bundle, latest, upstream_name=folder, resource_root=resource_path)
            _validate_tree(stage.path, target.name)
        return stages

    def _metadata(self, stage, bundle, latest, upstream_name=None, resource_root=None):
        path = "parity-source.json" if (stage.path / "parity-source.json").exists() else "source.json"
        value = _read_json(stage.path / path)
        value.update(source="https://github.com/" + bundle.repo if bundle.repo else "Existing Anthropic-synced local skill",
                     version=latest["version"], commit=latest["commit"], name=stage.target.name,
                     updatedAt=_now(), updater="AgentInventoryManager")
        if upstream_name:
            value["upstreamName"] = upstream_name
            value["upstreamPath"] = "skills/" + upstream_name
        elif stage.target.source_path:
            value["upstreamPath"] = stage.target.source_path
        if resource_root:
            value["resourceRoot"] = str(resource_root)
        if bundle.key == "claude-synced":
            value["source_directory"] = str(self._publisher(stage.target))
            value["original_entrypoint_sha256"] = _hash((self._publisher(stage.target) / "SKILL.md").read_bytes())
            value["installed_entrypoint_sha256"] = _hash((stage.path / "SKILL.md").read_bytes())
        value["installedFiles"] = [{"path": p, "sha256": h} for p, h in _files(stage.path).items() if p not in _METADATA]
        stage.write(path, json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"), metadata=True)

    @contextlib.contextmanager
    def _commit(self, stages):
        transaction = self.state_dir / "skill-backups" / (_now().replace(":", "-") + "-" + uuid.uuid4().hex[:10])
        transaction.mkdir(parents=True)
        completed = []
        try:
            for index, stage in enumerate(stages):
                target = stage.target.path
                if not _within(target, self.home) or target == self.home:
                    raise UpdateError("A target escaped the authorized user directory.")
                backup = transaction / f"{index}-{target.name}"
                existed = target.exists()
                if (stage.expected is None and existed) or (stage.expected is not None and (not existed or _files(target) != stage.expected)):
                    raise Conflict("Local content changed before replacement; it was preserved.")
                if existed:
                    if _is_link(target):
                        raise Conflict("A canonical update target became a directory link.")
                    os.replace(target, backup)
                completed.append((target, backup, existed, False, None))
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage.path, target)
                completed[-1] = (target, backup, existed, True, _files(target))
            _write_json(transaction / "transaction.json", {"createdAt": _now(), "state": "installed", "targets": [str(s.target.path) for s in stages]})
            yield transaction
        except BaseException:
            rollback_errors = []
            for index, (target, backup, existed, installed, fingerprint) in reversed(list(enumerate(completed))):
                try:
                    if installed and target.exists():
                        if _is_link(target) or _files(target) != fingerprint:
                            rollback_errors.append(target.name)
                            continue
                        os.replace(target, transaction / f"failed-{index}-{target.name}")
                    if existed and backup.exists():
                        os.replace(backup, target)
                except OSError:
                    rollback_errors.append(target.name)
            _write_json(transaction / "transaction.json", {"createdAt": _now(), "state": "rollback-incomplete" if rollback_errors else "rolled-back", "unrestoredCount": len(rollback_errors)})
            if rollback_errors:
                raise UpdateError("An update failed and automatic restoration was incomplete; backups were retained.") from None
            raise

    def _update_claude_plugin(self, bundle, latest):
        if not self._plugin_stale(bundle, latest):
            return None
        command = shutil.which("claude")
        if not command:
            return "Portable skills updated; the Claude plugin still needs its official CLI update (CLI unavailable)."
        args = [command, "plugin", "update", bundle.plugin["id"], "--scope", "user", "--json"]
        if os.name == "nt" and Path(command).suffix.lower() in {".cmd", ".bat"}:
            if any(any(c in arg for c in "%!&|<>^") for arg in args):
                return "Portable skills updated; the official Claude CLI path needs a manual invocation."
            args = [os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "/d", "/s", "/c", subprocess.list2cmdline(args)]
        # Never pass --yes/--accept-command: a newly declared marketplace command
        # must not become an unattended downloaded setup-script execution.
        try:
            completed = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=180, text=True, encoding="utf-8", errors="replace")
            if completed.returncode:
                return "Portable skills updated; Claude's official plugin update did not complete. Any requested marketplace command was not automatically accepted."
        except (OSError, subprocess.TimeoutExpired):
            return "Portable skills updated; the official Claude plugin update could not be verified."
        return None

    def apply(self, unit_id, progress=None):
        with self._lock:
            bundles, _ = self._catalog()
            bundle = next((b for b in bundles.values() if b.id == unit_id), None)
            if not bundle:
                return {"id": "unknown", "status": "error", "detail": "Unknown installed skill source; nothing was changed."}
            if not bundle.managed or not bundle.targets:
                return self._unit(bundle, "host-managed", detail="This source is managed by its publisher or agent application.")
            stage_root = None
            try:
                if self._conflicts(bundle):
                    return self._unit(bundle, "local-changes", detail="Installed content differs from its recorded baseline; nothing was overwritten.")
                latest = self._latest(bundle)
                current, _ = self._installed(bundle)
                if current == latest["commit"] and not self._plugin_stale(bundle, latest):
                    self._save()
                    return self._unit(bundle, "up-to-date", latest, "This source is already current.")
                before = {str(t.path): _files(t.path) for t in bundle.targets + self._resource_targets(bundle)}
                old_state = json.loads(json.dumps(self._state))
                if current != latest["commit"]:
                    stage_root = self.state_dir / "skill-staging" / uuid.uuid4().hex
                    stage_root.mkdir(parents=True)
                    stages = self._build(bundle, latest, stage_root, progress)
                    for path, fingerprint in before.items():
                        if _files(Path(path)) != fingerprint:
                            raise Conflict("Local content changed while the update was staging; nothing was replaced.")
                    known_targets = set(before)
                    for stage in stages:
                        if str(stage.target.path) not in known_targets and stage.target.path.exists():
                            raise Conflict("A new destination appeared while staging; it was preserved.")
                    self._emit(progress, "Backing up and installing " + bundle.title)
                    try:
                        with self._commit(stages) as backup:
                            baseline = {str(s.target.path): {"files": _files(s.target.path), "managed": sorted(_files(s.target.path)), "enrolledAt": _now()} for s in stages}
                            self._record(bundle).update(baseline=baseline, installedCommit=latest["commit"],
                                                        installedVersion=latest["version"], latest=latest, backup=str(backup), updatedAt=_now())
                            self._save()
                    except BaseException:
                        self._state = old_state
                        raise
                partial = self._update_claude_plugin(bundle, latest)
                result = self._unit(bundle, "partial" if partial else "updated", latest, partial or "The complete source bundle was staged, validated, backed up and updated. Restart agent sessions to load it.")
                result["partial"] = bool(partial)
                self._emit(progress, bundle.title + (" updated (Claude plugin requires attention)." if partial else " updated."))
                return result
            except Conflict as exc:
                return self._unit(bundle, "local-changes", detail=self._safe_error(exc))
            except (UpdateError, OSError, ValueError, UnicodeError) as exc:
                return self._unit(bundle, "error", detail=self._safe_error(exc))
            finally:
                if stage_root and _within(stage_root, self.state_dir / "skill-staging"):
                    shutil.rmtree(stage_root, ignore_errors=True)


class _Stage:
    def __init__(self, target: _Target, path: Path, baseline):
        self.target, self.path, self.baseline = target, path, baseline
        self.expected = _files(target.path) if target.path.exists() else None

    def write(self, relative, data: bytes, metadata=False, preserve_local=False):
        relative = _relative(relative)
        path = self.path / relative
        if path.exists() and path.read_bytes() != data:
            managed = set(self.baseline.get("managed", []))
            if relative not in managed and not metadata and not preserve_local:
                raise Conflict("An upstream file collides with an unrecorded local asset; it was preserved.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _insert_prefix(upstream: str, prefix: str) -> str:
    front = _FRONT.match(upstream)
    if not front:
        raise UpdateError("The publisher entrypoint is missing its frontmatter.")
    return upstream[:front.end()] + prefix + upstream[front.end():]


def _adapter_prefix(current: str) -> str:
    front = _FRONT.match(current)
    if not front:
        return ""
    body = current[front.end():]
    start = re.search(r"\n?## Standalone (?:shared installation|installation note|Codex / Gemini installation note)", body)
    if not start:
        return ""
    ending = re.search(r"\n#{1,3} (?!Standalone )", body[start.end():])
    if ending:
        return body[:start.end() + ending.start()]
    # A custom adapter with no discernible end must not consume new source text.
    raise Conflict("The existing standalone adapter boundary needs manual review.")


def _normalize_repo_path(value):
    parts = []
    for part in value.replace("\\", "/").split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    try:
        return _relative("/".join(parts))
    except UpdateError:
        return None
