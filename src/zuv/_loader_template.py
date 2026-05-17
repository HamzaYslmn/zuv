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
"""
import base64
import compileall
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

_DROP_ENV = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONHOME", "PYTHONPATH")
_READY = ".zuv-ready"
_MAX_BYTES = int(os.environ.get("ZUV_MAX_EXTRACT_BYTES", str(2 * 1024 * 1024 * 1024)))


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


def _run():
    script = Path(sys.argv[0]).resolve()

    if sys.implementation.cache_tag != _ZUV_PY_TAG:  # noqa: F821
        print(
            f"zuv: warning: built for {_ZUV_PY_TAG} but running on "  # noqa: F821
            f"{sys.implementation.cache_tag}; bytecode/marshal compat not guaranteed",
            file=sys.stderr,
        )

    cache_root = _cache_root(script)
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
        print(
            f"zuv: cached at {cache} ({time.monotonic() - t0:.1f}s); "
            f"future runs skip extraction",
            file=sys.stderr,
        )

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
        print(
            "zuv: error: 'uv' not found on PATH. Install it from "
            "https://astral.sh/uv and try again.",
            file=sys.stderr,
        )
        return 127


sys.exit(_run())
