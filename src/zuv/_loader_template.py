"""Runtime loader embedded (as inline Python source) in every zuv-built .py.

This module is never imported. The builder reads its source, strips this
docstring, and pastes the rest verbatim below the metadata + payload in the
output .py. No marshal, no compile -- the source is portable across any
Python that supports the syntax in this file.

Globals injected by the builder above this code:
  _ZUV_ENTRY:        str   entry script path, relative to project root
  _ZUV_BUILD_ID:     str   short hash for cache namespacing
  _ZUV_PAYLOAD:      bytes base85 of tar.xz of the project
  _ZUV_SHA:          str   sha256 hex of the *decoded* tar.xz bytes
  _ZUV_HAS_WHEELS:   bool  whether _zuv_wheels/ is embedded for offline install
  _ZUV_NO_COMPILE:   bool  if True, skip the first-run .py->.pyc compile pass
  _ZUV_APP_VERSION:  str   [project] version from pyproject.toml at build time
  _ZUV_UPDATE_PROVIDER: str  "github" or "gitlab"
  _ZUV_UPDATE_REPO:     str  "user/repo" to self-update from (empty = disabled)
  _ZUV_UPDATE_TAG:      str  release tag, or "latest" for the rolling release
  _ZUV_UPDATE_FILE:     str  asset filename inside the release
  _ZUV_VOLUME_PATH:     str  relative project path mounted as persistent volume ("" = disabled)
"""
import base64
import hashlib
import io
import json
import os
import re
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
_VOLUME_MARKER = ".zuv-volume"
_MAX_BYTES = int(os.environ.get("ZUV_MAX_EXTRACT_BYTES", str(2 * 1024 * 1024 * 1024)))
_UPDATE_SHA_FILE = ".zuv-update-known-sha"  # last sha seen (installed or declined)
_UPDATE_SHA_FILE_PRE = ".zuv-update-known-sha.pre"  # same, for --prerelease channel
_LINK_MODE_FILE = ".zuv-link-mode"
_PRERELEASE_TAG_RE = re.compile(r"(?:^|[-.+_])(rc|alpha|beta|pre|dev|a|b)\d*", re.I)
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
    if sys.platform == "win32":
        base_env = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    else:
        base_env = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    base = Path(base_env) if base_env else Path.home() / ".cache"
    return base / "zuv"


def _hardlinks_work(cache_root: Path) -> bool:
    """Probe whether os.link works inside cache_root. Hardlinks can't cross
    volumes (any OS) and some filesystems (exFAT/FAT32, certain SMB/NFS mounts,
    WSL drvfs) don't support them at all. Probe once, cache the verdict so the
    next run is free."""
    flag = cache_root / _LINK_MODE_FILE
    try:
        return flag.read_text(encoding="ascii").strip() == "hardlink"
    except OSError:
        pass
    src = dst = None
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        src = cache_root / f".zuv-linkprobe-src-{os.getpid()}"
        dst = cache_root / f".zuv-linkprobe-dst-{os.getpid()}"
        src.write_bytes(b"")
        dst.unlink(missing_ok=True)
        os.link(src, dst)
        works = True
    except (OSError, NotImplementedError, AttributeError):
        works = False
    finally:
        for p in (src, dst):
            if p is not None:
                p.unlink(missing_ok=True)
    try:
        flag.write_text("hardlink" if works else "copy", encoding="ascii")
    except OSError:
        pass
    return works


def _uv_env(cache_root: Path) -> dict:
    """Cross-platform uv environment: keep uv's package cache on the same
    volume as the venv so hardlinks succeed, and fall back to copy mode when
    the filesystem can't hardlink at all. Respects user overrides."""
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    if "UV_CACHE_DIR" not in env:
        env["UV_CACHE_DIR"] = str(cache_root / "uv-cache")
    if "UV_LINK_MODE" not in env and not _hardlinks_work(cache_root):
        env["UV_LINK_MODE"] = "copy"
    return env


def _volume_dirname(mount: str) -> str:
    # Flatten the mount path so on-disk volume is a single child of cache_root.
    return mount.strip("/\\").replace("\\", "/").replace("/", "_")


def _mount_volume(extract_dir: Path, cache_root: Path, mount: str) -> Path | None:
    """Promote-on-first-create, then symlink/junction. Returns the persistent
    volume path, or None if mounting failed."""
    persistent = cache_root / _volume_dirname(mount)
    target = extract_dir / mount
    fresh = not persistent.exists()
    if fresh:
        persistent.parent.mkdir(parents=True, exist_ok=True)
        # Docker-style seed: if developer shipped content at <project>/<mount>,
        # the first extraction becomes the initial volume.
        if target.exists() and not target.is_symlink() and target.is_dir():
            target.rename(persistent)
        else:
            persistent.mkdir(parents=True, exist_ok=True)
        try:
            (persistent / _VOLUME_MARKER).touch()
        except OSError:
            pass
    # Fast-path: already correctly mounted (covers symlinks AND NTFS junctions).
    if target.exists():
        try:
            if target.resolve() == persistent.resolve():
                return persistent
        except OSError:
            pass
    if target.exists() or target.is_symlink():
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                # On Windows a junction looks like a regular dir; rmdir handles it.
                try:
                    target.rmdir()
                except OSError:
                    shutil.rmtree(target, ignore_errors=True)
        except OSError:
            pass
    target.parent.mkdir(parents=True, exist_ok=True)

    # On Windows, junction is tried first because it works on NTFS/ReFS without
    # admin or Developer Mode. /D is the fallback for SMB shares where /J can't
    # resolve. os.symlink last covers anything the cmd.exe path misses.
    if sys.platform == "win32":
        attempts = (("junction", "/J"), ("dir-symlink", "/D"), ("os.symlink", None))
    else:
        attempts = (("symlink", None),)

    errors: list[str] = []
    for name, flag in attempts:
        try:
            if flag is None:
                os.symlink(persistent, target, target_is_directory=True)
            else:
                r = subprocess.run(
                    ["cmd", "/c", "mklink", flag, str(target), str(persistent)],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode != 0:
                    raise OSError(r.stderr.strip() or f"rc={r.returncode}")
            return persistent
        except (OSError, subprocess.SubprocessError, FileNotFoundError) as e:
            errors.append(f"{name}: {e}")

    print(
        f"zuv: warning: could not mount volume at {target}.{_fs_hint(target)} "
        f"Tried: {'; '.join(errors)}. Persistent path {persistent} is unreachable "
        f"to the app this run.",
        file=sys.stderr,
    )
    return None


def _fs_hint(target: Path) -> str:
    """Return a one-liner naming the filesystem if it's known not to support
    symlinks/junctions (FAT family), else "". Best-effort, never raises."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(64)
        drive = os.path.splitdrive(str(target))[0] + "\\"
        if not (drive and ctypes.windll.kernel32.GetVolumeInformationW(
            drive, None, 0, None, None, None, buf, 64,
        )):
            return ""
        fs = buf.value
        if fs.upper() in ("FAT", "FAT32", "EXFAT"):
            return (
                f" The target filesystem is {fs}, which does not support "
                f"symlinks or junctions; move the bundle to an NTFS/ReFS drive."
            )
    except (OSError, AttributeError, ImportError):
        pass
    return ""


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


def _asset_url(repo: str, tag: str, file: str, provider: str) -> str:
    """CDN-served direct download URL for a release asset. Used for both the
    cheap HEAD freshness check and the GET download - both go through the
    asset CDN, not the API, so no 60/hr unauthenticated rate limit applies.
    """
    if provider == "gitlab":
        proj = urllib.parse.quote(repo, safe="")
        path = urllib.parse.quote(file, safe="")
        if tag == "latest":
            return f"https://gitlab.com/api/v4/projects/{proj}/releases/permalink/latest/downloads/{path}"
        return (
            f"https://gitlab.com/api/v4/projects/{proj}/releases/"
            f"{urllib.parse.quote(tag, safe='')}/downloads/{path}"
        )
    if tag == "latest":
        return f"https://github.com/{repo}/releases/latest/download/{file}"
    return f"https://github.com/{repo}/releases/download/{tag}/{file}"


def _resolve_prerelease_tag(repo: str, provider: str, headers: dict) -> str | None:
    """Return the tag of the most-recent prerelease, or None if we couldn't find
    one. Uses the provider's listing API (rate-limited; only hit when the user
    explicitly opted in via --prerelease).

    GitHub: filter by the `prerelease: true` flag.
    GitLab: no first-class prerelease flag; treat `upcoming_release` or a tag
    matching common prerelease suffixes (rc/alpha/beta/pre/dev) as prerelease.
    """
    if provider == "gitlab":
        proj = urllib.parse.quote(repo, safe="")
        api = f"https://gitlab.com/api/v4/projects/{proj}/releases?per_page=30"
    else:
        api = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    _dbg(f"GET {api} (prerelease lookup)")
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        print(
            f"zuv: prerelease lookup failed ({e}); running local version.",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, list):
        return None
    for rel in data:
        if not isinstance(rel, dict):
            continue
        tag = rel.get("tag_name") or rel.get("name") or ""
        if not tag:
            continue
        if provider == "github":
            if rel.get("prerelease"):
                return tag
        else:
            if rel.get("upcoming_release") or _PRERELEASE_TAG_RE.search(tag):
                return tag
    _dbg("no prerelease found in listing")
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Opener handler that turns 3xx into HTTPError instead of following the
    redirect, so we can read the Location header ourselves."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _resolve_display_tag(url: str, provider: str, headers: dict) -> str | None:
    """For a `/releases/latest/...` asset URL, peek at the first 3xx Location
    header to recover the real tag (e.g. `v1.6.0`). Returns None if anything
    goes wrong - callers should fall back to printing 'latest'."""
    opener = urllib.request.build_opener(_NoRedirect)
    loc = ""
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        try:
            resp = opener.open(req, timeout=5)
            loc = resp.headers.get("Location") or ""
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") or ""
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None
    if not loc:
        return None
    if provider == "gitlab":
        m = re.search(r"/releases/([^/]+)/downloads/", loc)
    else:
        m = re.search(r"/releases/download/([^/]+)/", loc)
    if not m:
        return None
    tag = urllib.parse.unquote(m.group(1))
    return tag if tag and tag != "latest" else None


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


def _check_update_inner(script: Path, cache_root: Path, prerelease: bool) -> None:
    """Implementation of the update check. Raises on any non-network error;
    the outer wrapper catches everything to keep the app non-blocking."""
    if not _ZUV_UPDATE_REPO or not _ZUV_UPDATE_FILE:  # noqa: F821
        _dbg("update disabled (no _ZUV_UPDATE_REPO or _ZUV_UPDATE_FILE)")
        return
    if os.environ.get("ZUV_NO_UPDATE"):
        _dbg("update disabled (ZUV_NO_UPDATE=1)")
        return
    if (cache_root / ".zuv-update-disabled").exists():
        _dbg("update disabled (.zuv-update-disabled sentinel present)")
        return

    provider = _ZUV_UPDATE_PROVIDER  # noqa: F821
    headers = _auth_headers(provider)
    tag = _ZUV_UPDATE_TAG  # noqa: F821
    if prerelease:
        resolved = _resolve_prerelease_tag(_ZUV_UPDATE_REPO, provider, headers)  # noqa: F821
        if resolved is None:
            return
        tag = resolved
        _dbg(f"--prerelease resolved tag={tag!r}")
    url = _asset_url(_ZUV_UPDATE_REPO, tag, _ZUV_UPDATE_FILE, provider)  # noqa: F821

    # Use HEAD on the CDN-served asset URL (not the API) for change detection.
    # The CDN isn't subject to the 60/hr unauthenticated API rate limit, so
    # public-repo updates work without any token. ETag changes whenever the
    # asset bytes change (i.e. when --clobber re-uploads).
    _dbg(f"HEAD {url}")
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            change_token = (
                resp.headers.get("ETag")
                or resp.headers.get("Last-Modified")
                or ""
            )
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        # User asked for explicit visibility of errors and an explicit fallback.
        print(
            f"zuv: update check skipped ({e}); running local version.",
            file=sys.stderr,
        )
        return

    if not change_token:
        _dbg("HEAD response has no ETag/Last-Modified; cannot detect changes")
        return
    _dbg(f"change_token={change_token!r}")

    sha_cache = cache_root / (_UPDATE_SHA_FILE_PRE if prerelease else _UPDATE_SHA_FILE)
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

    display_tag = tag
    if tag == "latest":
        resolved_display = _resolve_display_tag(url, provider, headers)
        if resolved_display:
            display_tag = resolved_display
            _dbg(f"resolved display tag for 'latest' -> {display_tag!r}")

    version_line = (
        f" (current local version: {_ZUV_APP_VERSION})"  # noqa: F821
        if _ZUV_APP_VERSION else ""
    )
    channel = " [prerelease]" if prerelease else ""
    print(
        f"zuv: update available for {provider}:{_ZUV_UPDATE_REPO}{channel}"  # noqa: F821
        f" (release {display_tag}, asset {_ZUV_UPDATE_FILE}){version_line}",  # noqa: F821
        file=sys.stderr,
    )
    if auto:
        print("zuv: ZUV_AUTO_UPDATE=1 -> accepting", file=sys.stderr)
        answer = "y"
    else:
        try:
            answer = input(
                f"zuv: install {display_tag}? [Y/n] "
            ).strip().lower()
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
        # GET the same CDN URL we HEAD'd above.
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(tmp, script)
        sha_cache.write_text(change_token, encoding="ascii")
        _dbg(f"replaced {script.name} and wrote new known-token {change_token!r}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(
            f"zuv: download failed ({e}); running local version.",
            file=sys.stderr,
        )
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return

    # Re-exec the new bundle. On Windows os.execvp has flaky parent-process
    # behaviour; subprocess + exit is reliable everywhere.
    print(f"zuv: re-exec via `uv run {script.name}`", file=sys.stderr, flush=True)
    forward = sys.argv[1:]
    if prerelease and "--prerelease" not in forward:
        # Keep the channel sticky across re-exec so the new bundle also self-
        # updates on the prerelease track rather than reverting to stable.
        forward = ["--prerelease", *forward]
    rc = subprocess.call(["uv", "run", str(script), *forward])
    sys.exit(rc)


def _check_update(script: Path, cache_root: Path, prerelease: bool) -> None:
    """Catch-all wrapper for the update check. Quietly swallows any
    unexpected error so a broken update path never blocks the app. Set
    ZUV_DEBUG=1 to see what happened (and ZUV_AUTO_UPDATE=1 to skip the
    interactive prompt - for testing / CI / scripted deploys).
    """
    try:
        _check_update_inner(script, cache_root, prerelease)
    except SystemExit:
        raise  # re-exec path; let it through
    except Exception as e:
        _dbg(f"unexpected error in update check: {type(e).__name__}: {e}")
        if _DEBUG:
            import traceback
            traceback.print_exc(file=sys.stderr)


def _run() -> int:
    script = Path(sys.argv[0]).resolve()

    # `--prerelease` is a loader-level flag: when present, route the self-update
    # check to the latest prerelease instead of the stable release. Strip it
    # from argv before forwarding so the embedded app doesn't see it.
    prerelease = False
    if "--prerelease" in sys.argv[1:]:
        prerelease = True
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--prerelease"]

    cache_root = _cache_root(script)
    _check_update(script, cache_root, prerelease)
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

        # Compile .py -> .pyc using the venv's Python (the one uv will run
        # the entry with), so bytecode always matches the runtime interpreter
        # and is portable across builders. This is also the first `uv run` of
        # the cache, so it creates the venv + installs deps as a side effect.
        # Best-effort: any error here is reported but doesn't block the run.
        if not _ZUV_NO_COMPILE:  # noqa: F821
            # Strip parent venv vars so uv doesn't print the
            # "VIRTUAL_ENV does not match the project environment" warning.
            _env = _uv_env(cache_root)
            try:
                rc = subprocess.call(
                    ["uv", "run", "--project", str(cache),
                     "python", "-m", "compileall", "-q", str(cache)],
                    cwd=str(cache), env=_env,
                )
                if rc != 0:
                    print(f"zuv: warning: bytecode pre-compile exited {rc}", file=sys.stderr)
            except FileNotFoundError:
                pass  # uv missing; subprocess.call below will surface the same error
            except Exception as e:
                print(f"zuv: warning: bytecode pre-compile skipped: {e}", file=sys.stderr)

        if _ZUV_VOLUME_PATH:  # noqa: F821
            _mount_volume(cache, cache_root, _ZUV_VOLUME_PATH)  # noqa: F821

        ready.write_text(_ZUV_SHA, encoding="ascii")  # noqa: F821

        removed = 0
        prefix = f"{script.stem}_"
        for sibling in cache_root.iterdir():
            if sibling == cache or not sibling.name.startswith(prefix):
                continue
            if not sibling.is_dir() or sibling.is_symlink():
                continue
            if (sibling / _VOLUME_MARKER).exists():
                continue  # persistent volume - never GC
            shutil.rmtree(sibling, ignore_errors=True)
            removed += 1

        msg = f"zuv: cached at {cache} ({time.monotonic() - t0:.1f}s)"
        if removed:
            msg += f"; gc'd {removed} old build{'s' if removed != 1 else ''}"
        print(msg + "; future runs skip extraction", file=sys.stderr)

    # Warm-cache: re-mount the volume on every run so a missing/broken link heals itself.
    if _ZUV_VOLUME_PATH:  # noqa: F821
        _mount_volume(cache, cache_root, _ZUV_VOLUME_PATH)  # noqa: F821

    env = _uv_env(cache_root)
    env["IS_ZUV"] = "true"
    env["ZUV_CACHE_ROOT"] = str(cache_root)
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
