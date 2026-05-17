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
  _ZUV_UPDATE_TAG:      str  release tag, or "latest" for /releases/latest
  _ZUV_UPDATE_FILE:     str  asset filename inside the release
  _ZUV_APP_VERSION:     str  [project] version from pyproject.toml at build time
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
_DEBUG = bool(os.environ.get("ZUV_DEBUG"))


def _dbg(msg: str) -> None:
    """Print a diagnostic line iff ZUV_DEBUG=1. Lets users introspect the
    update path's decisions without altering normal-quiet behaviour."""
    if _DEBUG:
        print(f"zuv[debug]: {msg}", file=sys.stderr, flush=True)


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


def _release_url(repo: str, tag: str, provider: str) -> str:
    """URL of the release metadata endpoint. `tag == 'latest'` is special on
    both providers — it returns the most recently published release."""
    if provider == "gitlab":
        proj = urllib.parse.quote(repo, safe="")
        if tag == "latest":
            return f"https://gitlab.com/api/v4/projects/{proj}/releases/permalink/latest"
        return f"https://gitlab.com/api/v4/projects/{proj}/releases/{urllib.parse.quote(tag, safe='')}"
    if tag == "latest":
        return f"https://api.github.com/repos/{repo}/releases/latest"
    return f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"


def _find_asset(release: dict, file: str, provider: str) -> tuple[str, str]:
    """Return (download_url, change_token) for the named asset in the release,
    or ('', '') if not found. `change_token` is the value we compare across
    runs to decide whether the asset is new:
      - GitHub: asset 'id' (changes whenever `--clobber` re-uploads)
      - GitLab: release 'released_at' (per-asset change isn't exposed)
    """
    if provider == "gitlab":
        for link in (release.get("assets") or {}).get("links") or []:
            if (link.get("name") or "") == file:
                return link.get("direct_asset_url") or link.get("url") or "", \
                       str(release.get("released_at") or "")
        return "", ""
    for asset in release.get("assets") or []:
        if (asset.get("name") or "") == file:
            return asset.get("browser_download_url") or "", str(asset.get("id") or "")
    return "", ""


def _newer_version(remote: str, local: str) -> bool | None:
    """Return True if `remote` is a strictly newer dotted-int version than
    `local`, False if not, or None if either side doesn't parse cleanly
    (e.g. rolling tags like "latest"). Strips one leading 'v'.
    """
    if not remote or not local:
        return None

    def _parse(s: str):
        if s.startswith("v"):
            s = s[1:]
        try:
            return tuple(int(p) for p in s.split("."))
        except ValueError:
            return None

    r, l = _parse(remote), _parse(local)
    if r is None or l is None:
        return None
    n = max(len(r), len(l))
    return r + (0,) * (n - len(r)) > l + (0,) * (n - len(l))


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


def _check_update_inner(script: Path, cache_root: Path) -> None:
    """Implementation of the update check. Raises on any non-network error;
    the outer wrapper catches everything to keep production silent."""
    if not _ZUV_UPDATE_REPO or not _ZUV_UPDATE_FILE:  # noqa: F821
        _dbg("update disabled (no _ZUV_UPDATE_REPO or _ZUV_UPDATE_FILE)")
        return
    if os.environ.get("ZUV_NO_UPDATE"):
        _dbg("update disabled (ZUV_NO_UPDATE=1)")
        return

    provider = _ZUV_UPDATE_PROVIDER  # noqa: F821
    api = _release_url(_ZUV_UPDATE_REPO, _ZUV_UPDATE_TAG, provider)  # noqa: F821
    headers = _auth_headers(provider)
    _dbg(f"GET {api}")
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        _dbg(f"release API call failed: {e!r}")
        return

    remote_tag = release.get("tag_name") or ""
    _dbg(f"release tag_name={remote_tag!r}, local _ZUV_APP_VERSION={_ZUV_APP_VERSION!r}")  # noqa: F821

    # Version check short-circuits when both sides parse: skip if local >= remote.
    # When the remote tag isn't a version (e.g. "latest") or no version was
    # baked at build time, this falls through to change-token comparison below.
    newer = _newer_version(remote_tag, _ZUV_APP_VERSION)  # noqa: F821
    if newer is False:
        _dbg("local version >= remote; skipping")
        return
    _dbg(f"version-check result: newer={newer!r} (None = fall through to change-token)")

    download_url, change_token = _find_asset(release, _ZUV_UPDATE_FILE, provider)  # noqa: F821
    if not download_url or not change_token:
        _dbg(f"asset not found in release (looking for {_ZUV_UPDATE_FILE!r})")  # noqa: F821
        return
    _dbg(f"asset found: change_token={change_token!r}, download_url={download_url}")

    sha_cache = cache_root / _UPDATE_SHA_FILE
    try:
        known = sha_cache.read_text(encoding="ascii").strip()
    except OSError:
        known = ""
    _dbg(f"cached known token={known!r}")
    if change_token == known:
        _dbg("change-token matches cache; nothing to do")
        return

    # Auto-accept mode for headless / scripted invocations.
    auto = bool(os.environ.get("ZUV_AUTO_UPDATE"))
    # Otherwise, prompt requires a TTY; in CI / pipes / GUI launchers, skip silently.
    if not auto and not sys.stdin.isatty():
        _dbg("non-TTY and ZUV_AUTO_UPDATE not set; skipping update")
        return

    version_line = (
        f" ({_ZUV_APP_VERSION} -> {remote_tag})"  # noqa: F821
        if _ZUV_APP_VERSION else f" (release {remote_tag})"
    )
    print(
        f"zuv: update available for {provider}:{_ZUV_UPDATE_REPO}{version_line}",  # noqa: F821
        file=sys.stderr,
    )
    if auto:
        print("zuv: ZUV_AUTO_UPDATE=1 -> accepting", file=sys.stderr)
        answer = "y"
    else:
        try:
            answer = input("zuv: install latest version? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _dbg("input cancelled")
            return

    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    if answer not in ("", "y", "yes"):
        _dbg("declined; writing change_token to cache to avoid re-prompt")
        try:
            sha_cache.write_text(change_token, encoding="ascii")
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
        sha_cache.write_text(change_token, encoding="ascii")
        _dbg(f"replaced {script.name} and wrote new known-token {change_token!r}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"zuv: update failed ({e}); continuing with current version", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return

    # Re-exec the new bundle. On Windows os.execvp has flaky parent-process
    # behaviour; subprocess + exit is reliable everywhere.
    print(f"zuv: re-exec via `uv run {script.name}`", file=sys.stderr, flush=True)
    rc = subprocess.call(["uv", "run", str(script), *sys.argv[1:]])
    sys.exit(rc)


def _check_update(script: Path, cache_root: Path) -> None:
    """Catch-all wrapper for the update check. Quietly swallows any
    unexpected error so a broken update path never blocks the app. Set
    ZUV_DEBUG=1 to see what happened (and ZUV_AUTO_UPDATE=1 to skip the
    interactive prompt — for testing / CI / scripted deploys).
    """
    try:
        _check_update_inner(script, cache_root)
    except SystemExit:
        raise  # re-exec path; let it through
    except Exception as e:
        _dbg(f"unexpected error in update check: {type(e).__name__}: {e}")
        if _DEBUG:
            import traceback
            traceback.print_exc(file=sys.stderr)


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
