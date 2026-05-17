"""Runtime loader embedded (compiled + marshalled) in every zuv-built .py.

This module is never imported. The builder reads its source, strips this
docstring, compiles it to a code object, marshals + zlib-compresses it, and
embeds the blob as `_ZUV_LOADER`. A 3-line plaintext stub at the end of the
built .py decompresses the blob and execs it.

Globals injected by the builder above this code:
  _ZUV_ENTRY:    str   entry script path, relative to project root
  _ZUV_BUILD_ID: str   short hash for cache namespacing
  _ZUV_PAYLOAD:  bytes base85 of tar.xz of the project
  _ZUV_SHA:      str   sha256 hex of the *decoded* tar.xz bytes
  _ZUV_PY_TAG:   str   build-time sys.implementation.cache_tag
  _ZUV_HAS_WHEELS: bool  whether _zuv_wheels/ is embedded for offline install
  _ZUV_NO_COMPILE: bool  if True, skip the first-run .py->.pyc compile pass
  _ZUV_UPDATE_PROVIDER: str  "github" or "gitlab"
  _ZUV_UPDATE_REPO:     str  "user/repo" to self-update from (empty = disabled)
  _ZUV_UPDATE_BRANCH:   str  branch to fetch the update file from
  _ZUV_UPDATE_FILE:     str  path of the .zuv.py file inside the repo
"""
import base64
import compileall
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_DROP_ENV = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONHOME", "PYTHONPATH")
_READY = ".zuv-ready"
_MAX_BYTES = int(os.environ.get("ZUV_MAX_EXTRACT_BYTES", str(2 * 1024 * 1024 * 1024)))
_UPDATE_SHA_FILE = ".zuv-update-known-sha"  # last sha seen (installed or declined)


def _cache_root(script: Path) -> Path:
    env = os.environ.get("ZUV_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    candidate = script.parent / ".zuv"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / f".write-test-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return candidate
    except OSError:
        pass
    xdg = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "zuv"


def _extract(payload: bytes, dst: Path) -> None:
    total = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as tf:
        for m in tf:
            total += max(m.size, 0)
            if total > _MAX_BYTES:
                raise RuntimeError(
                    f"zuv: extracted size exceeds ZUV_MAX_EXTRACT_BYTES "
                    f"({_MAX_BYTES} bytes); aborting"
                )
        tf.fileobj.seek(0)  # re-open to actually extract
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as tf:
        tf.extractall(dst, filter="data")


def _provider_urls(repo: str, branch: str, file: str, provider: str) -> tuple[str, str, str]:
    """Return (metadata_url, sha_field, download_url) for the given provider.
    `download_url` for GitLab is a real URL; for GitHub it's '' (use the
    'download_url' field on the metadata response instead)."""
    if provider == "gitlab":
        proj = urllib.parse.quote(repo, safe="")
        path = urllib.parse.quote(file, safe="")
        base = f"https://gitlab.com/api/v4/projects/{proj}/repository/files/{path}"
        return f"{base}?ref={branch}", "blob_id", f"{base}/raw?ref={branch}"
    api = f"https://api.github.com/repos/{repo}/contents/{file}?ref={branch}"
    return api, "sha", ""


def _auth_headers(provider: str) -> dict:
    """User-Agent + appropriate auth header for the provider, if a token is
    set in the environment. Lets private repos work with no extra plumbing."""
    headers = {"User-Agent": "zuv-updater"}
    if provider == "gitlab":
        token = os.environ.get("GITLAB_TOKEN")
        if token:
            headers["PRIVATE-TOKEN"] = token
    else:
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _check_update(script: Path, cache_root: Path) -> None:
    """If _ZUV_UPDATE_REPO is set, ask the GitHub Contents API for the file's
    current git blob sha (one call per startup). If the sha differs from the
    last-known one (either installed or previously declined), prompt the user
    [Y/n]; on Y, download and atomically replace this script, then re-exec.

    Silent on any failure (network, missing file, auth, non-TTY) so a broken
    update path never blocks the app. Honors ZUV_NO_UPDATE=1 to disable.
    """
    if not _ZUV_UPDATE_REPO or not _ZUV_UPDATE_FILE:  # noqa: F821
        return
    if os.environ.get("ZUV_NO_UPDATE"):
        return

    provider = _ZUV_UPDATE_PROVIDER  # noqa: F821
    api, sha_field, raw_url = _provider_urls(
        _ZUV_UPDATE_REPO, _ZUV_UPDATE_BRANCH, _ZUV_UPDATE_FILE, provider,  # noqa: F821
    )
    headers = _auth_headers(provider)
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return

    remote_sha = meta.get(sha_field) or ""
    # GitHub embeds the download URL in the metadata response; GitLab uses a
    # separate /raw endpoint that we built in _provider_urls.
    download_url = raw_url or meta.get("download_url") or ""
    if not remote_sha or not download_url:
        return

    sha_cache = cache_root / _UPDATE_SHA_FILE
    try:
        known_sha = sha_cache.read_text(encoding="ascii").strip()
    except OSError:
        known_sha = ""
    if remote_sha == known_sha:
        return  # already installed OR already declined this exact sha

    # Prompt requires a TTY; in CI / pipes / GUI launchers, skip silently.
    if not sys.stdin.isatty():
        return

    print(
        f"zuv: update available for {provider}:{_ZUV_UPDATE_REPO}"  # noqa: F821
        f"@{_ZUV_UPDATE_BRANCH}/{_ZUV_UPDATE_FILE}",  # noqa: F821
        file=sys.stderr,
    )
    try:
        answer = input("zuv: install latest version? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    if answer not in ("", "y", "yes"):
        # Remember the declined sha so we don't re-prompt until a newer one.
        try:
            sha_cache.write_text(remote_sha, encoding="ascii")
        except OSError:
            pass
        return

    print("zuv: downloading...", file=sys.stderr, flush=True)
    tmp = script.with_name(script.name + ".tmp")
    try:
        req = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(tmp, script)
        sha_cache.write_text(remote_sha, encoding="ascii")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"zuv: update failed ({e}); continuing with current version", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return

    # Re-exec the new bundle. On Windows os.execvp has flaky parent-process
    # behaviour; subprocess + exit is reliable everywhere.
    rc = subprocess.call(["uv", "run", str(script), *sys.argv[1:]])
    sys.exit(rc)


def _run():
    script = Path(sys.argv[0]).resolve()

    cache_root = _cache_root(script)
    _check_update(script, cache_root)
    cache = cache_root / f"{script.stem}_{_ZUV_BUILD_ID}"  # noqa: F821
    ready = cache / _READY

    if not ready.exists():
        raw = base64.b85decode(_ZUV_PAYLOAD)  # noqa: F821
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != _ZUV_SHA:  # noqa: F821
            print(
                f"zuv: payload checksum mismatch (got {actual_sha[:16]}, "
                f"expected {_ZUV_SHA[:16]}); file is corrupted",  # noqa: F821
                file=sys.stderr,
            )
            return 1

        print(
            f"zuv: first-run setup for {script.name} "
            f"(extracting + uv will install deps)...",
            file=sys.stderr,
            flush=True,
        )
        t0 = time.monotonic()

        if cache.exists():
            shutil.rmtree(cache, ignore_errors=True)
        tmp = cache.with_name(f"{cache.name}.tmp.{os.getpid()}")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            _extract(raw, tmp)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        try:
            tmp.rename(cache)
        except OSError:
            shutil.rmtree(cache, ignore_errors=True)
            tmp.rename(cache)

        # Pre-compile .py -> .pyc for the target Python so imports skip the
        # source-compile step on every run. Quiet, best-effort: a syntax error
        # in user code shouldn't block the run (uv will surface it on import).
        if not _ZUV_NO_COMPILE:  # noqa: F821
            try:
                compileall.compile_dir(
                    str(cache),
                    quiet=1,
                    force=False,
                    legacy=False,
                    workers=0,
                )
            except Exception as e:
                print(f"zuv: warning: bytecode pre-compile skipped: {e}", file=sys.stderr)

        ready.write_text(_ZUV_SHA, encoding="ascii")  # noqa: F821

        removed = 0
        prefix = f"{script.stem}_"
        for sibling in cache_root.iterdir():
            if sibling == cache or not sibling.name.startswith(prefix):
                continue
            if not sibling.is_dir() or sibling.is_symlink():
                continue
            shutil.rmtree(sibling, ignore_errors=True)
            removed += 1

        msg = f"zuv: cached at {cache} ({time.monotonic() - t0:.1f}s)"
        if removed:
            msg += f"; gc'd {removed} old build{'s' if removed != 1 else ''}"
        print(msg + "; future runs skip extraction", file=sys.stderr)

    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    if _ZUV_HAS_WHEELS:  # noqa: F821
        wheels_dir = cache / "_zuv_wheels"
        if wheels_dir.is_dir():
            env["UV_OFFLINE"] = "1"
            env["UV_FIND_LINKS"] = str(wheels_dir)
            env["UV_NO_INDEX"] = "1"
    cmd = ["uv", "run", "--project", str(cache), str(cache / _ZUV_ENTRY), *sys.argv[1:]]  # noqa: F821
    try:
        rc = subprocess.call(cmd, cwd=str(cache), env=env)
        if rc != 0 and _ZUV_HAS_WHEELS:  # noqa: F821
            print(
                "zuv: hint: this is an offline bundle with embedded wheels. "
                "If install failed, your platform may not be in the embedded "
                "set (win/linux/macOS x86_64+aarch64). Rebuild without --deps "
                "for an online install, or on a matching platform.",
                file=sys.stderr,
            )
        return rc
    except FileNotFoundError:
        if os.name == "nt":
            install = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        else:
            install = "curl -LsSf https://astral.sh/uv/install.sh | sh"
        print(
            "zuv: error: 'uv' not found on PATH.\n"
            "      This bundle needs uv to run. Install it (30s, ships its own Python):\n"
            f"        {install}\n"
            "      Or see https://astral.sh/uv",
            file=sys.stderr,
        )
        return 127


sys.exit(_run())
