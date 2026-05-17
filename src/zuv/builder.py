"""Build a single-file Python launcher from a uv project.

Layout of the output (line-by-line):
  shebang -> PEP 723 -> metadata globals
  -> _ZUV_PAYLOAD (base85 of deterministic tar.xz)
  -> _ZUV_LOADER  (base85 of zlib+marshal of the compiled loader template)
  -> 3-line stub that exec()s the loader

Why base85: uv's `--script` runner requires the file to be valid UTF-8.
That rules out raw-binary appends and PEP 263 `latin-1` source tricks --
both produce files uv refuses to run. base85 is the densest ASCII-safe
encoding in the stdlib (~25% overhead vs base64's 33%).
"""
import base64
import hashlib
import io
import lzma
import marshal
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
import zlib
from pathlib import Path

from .constants import (
    BUILD_ID_VAR,
    ENTRY_VAR,
    HAS_WHEELS_VAR,
    NO_COMPILE_VAR,
    LOADER_BEGIN,
    LOADER_END,
    LOADER_VAR,
    PAYLOAD_BEGIN,
    PAYLOAD_END,
    PAYLOAD_VAR,
    PY_TAG_VAR,
    SHA_VAR,
    WHEEL_PLATFORMS,
    WHEELS_DIRNAME,
    ZUV_SHEBANG,
)

_SKIP_NAMES = {
    ".venv", ".zuv", "dist", "build", "__pycache__",
    "node_modules", ".git", ".idea", ".vscode",
    ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".tox", ".nox", "htmlcov", ".DS_Store", "Thumbs.db",
}


def _skip(rel: Path) -> bool:
    return any(part in _SKIP_NAMES for part in rel.parts)


def _tarball(root: Path) -> tuple[bytes, str]:
    """Build a reproducible tar.xz of `root`. Returns (bytes, sha256_hex)."""
    buf = io.BytesIO()
    h = hashlib.sha256()
    with tarfile.open(fileobj=buf, mode="w:xz", preset=9 | lzma.PRESET_EXTREME) as tf:
        for p in sorted(root.rglob("*"), key=lambda x: x.relative_to(root).as_posix()):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if _skip(rel):
                continue
            name = rel.as_posix()
            info = tf.gettarinfo(str(p), arcname=name)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            data = p.read_bytes()
            info.size = len(data)
            h.update(name.encode("utf-8"))
            h.update(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue(), h.hexdigest()


def _compile_loader() -> bytes:
    """Compile + marshal + zlib the loader template into an opaque blob."""
    src = Path(__file__).with_name("_loader_template.py").read_text(encoding="utf-8")
    code = compile(src, "<zuv_loader>", "exec")
    return zlib.compress(marshal.dumps(code), 9)


def _b85_literal(prefix: str, data: bytes) -> str:
    return f'{prefix} = b"{base64.b85encode(data).decode("ascii")}"\n'


def _stage_copy(project_dir: Path) -> Path:
    """Copy the project to a tempdir (no compile). Caller cleans the parent."""
    stage_root = Path(tempfile.mkdtemp(prefix="zuv-stage-"))
    proj = stage_root / "p"

    def _ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n in _SKIP_NAMES]

    shutil.copytree(project_dir, proj, ignore=_ignore)
    return proj


def _download_wheels(project_dir: Path, dest: Path, platforms: list[str]) -> int:
    """Export locked deps and download wheels for the major target platforms
    into `dest`. Pure-Python wheels (`*-none-any.whl`) dedupe naturally because
    uv writes by filename. Returns the number of wheel files staged.

    Requires `uv` on PATH (same prerequisite as the runtime). Uses the
    builder's Python X.Y as the wheel-target Python version, matching the
    lockfile's resolution.
    """
    dest.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        req_path = Path(tf.name)
    try:
        print("  exporting locked deps...")
        export = subprocess.run(
            [
                "uv", "export",
                "--project", str(project_dir),
                "--format", "requirements-txt",
                "--no-hashes",
                "--no-emit-project",
                "-o", str(req_path),
            ],
            capture_output=True, text=True,
        )
        if export.returncode != 0:
            print(export.stderr, file=sys.stderr)
            raise RuntimeError("uv export failed; is uv installed and is there a uv.lock?")

        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        for label in platforms:
            tags = WHEEL_PLATFORMS[label]
            print(f"  downloading wheels for {label} (py {py_ver})...")
            plat_args = []
            for t in tags:
                plat_args += ["--platform", t]
            dl = subprocess.run(
                [
                    "uv", "run", "--with", "pip", "--no-project",
                    "python", "-m", "pip", "download",
                    "--only-binary=:all:",
                    "--python-version", py_ver,
                    *plat_args,
                    "-r", str(req_path),
                    "-d", str(dest),
                ],
                capture_output=True, text=True,
            )
            if dl.returncode != 0:
                print(dl.stderr.strip()[-800:], file=sys.stderr)
                print(
                    f"  warn: wheel download for {label} failed; this "
                    f"platform won't run offline. Continuing with other targets.",
                    file=sys.stderr,
                )
    finally:
        req_path.unlink(missing_ok=True)

    wheels = list(dest.glob("*.whl")) + list(dest.glob("*.tar.gz"))
    return len(wheels)


def _clean_output_parent(parent: Path) -> None:
    if not parent.exists():
        return
    print(f"cleaning {parent}...")
    for child in parent.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError as e:
            print(f"  skip {child.name}: {e}", file=sys.stderr)


def build_pyz(
    project_dir: Path,
    output: Path,
    entry: str | None,
    clean: bool = False,
    embed_deps: list[str] | None = None,
    no_compile: bool = False,
) -> int:
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        print(f"error: no pyproject.toml in {project_dir}", file=sys.stderr)
        return 2

    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    zuv_cfg = data.get("tool", {}).get("zuv", {}) or {}
    requires_python = data.get("project", {}).get("requires-python")

    resolved_entry = (
        entry
        or zuv_cfg.get("entry")
        or ("src/main.py" if (project_dir / "src" / "main.py").is_file() else "main.py")
    )
    if not (project_dir / resolved_entry).is_file():
        print(f"error: entry script not found: {resolved_entry}", file=sys.stderr)
        return 2

    if clean:
        _clean_output_parent(output.parent)
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"project: {project_dir}")
    print(f"entry:   {resolved_entry}")
    print(f"python:  {requires_python or '(unspecified)'}")

    stage_root: Path | None = None
    has_wheels = False
    try:
        if embed_deps is not None:
            tar_root = _stage_copy(project_dir)
            stage_root = tar_root.parent
        else:
            tar_root = project_dir
        print("shipping .py sources; loader will compile to .pyc on first run")

        if embed_deps is not None:
            print(f"embedding wheels for: {', '.join(embed_deps)}")
            count = _download_wheels(project_dir, tar_root / WHEELS_DIRNAME, embed_deps)
            if count == 0:
                print(
                    "error: no wheels were downloaded; cannot build offline bundle",
                    file=sys.stderr,
                )
                return 2
            has_wheels = True
            print(f"  staged {count} wheel files")

        print("packing project tar.xz...")
        payload, build_id = _tarball(tar_root)
    finally:
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)
    payload_sha = hashlib.sha256(payload).hexdigest()

    print("compiling loader...")
    loader_blob = _compile_loader()

    py_tag = sys.implementation.cache_tag or sysconfig.get_config_var("SOABI") or "unknown"

    pep723 = "# /// script\n"
    if requires_python:
        pep723 += f'# requires-python = "{requires_python}"\n'
    # dependencies MUST stay empty: uv reads deps from the extracted
    # pyproject.toml after first-run extraction, not from this PEP 723 block.
    pep723 += "# dependencies = []\n# ///\n"

    text = (
        ZUV_SHEBANG
        + pep723
        + f'{ENTRY_VAR} = "{resolved_entry}"\n'
        + f'{BUILD_ID_VAR} = "{build_id[:16]}"\n'
        + f'{SHA_VAR} = "{payload_sha}"\n'
        + f'{PY_TAG_VAR} = "{py_tag}"\n'
        + f'{HAS_WHEELS_VAR} = {has_wheels!r}\n'
        + f'{NO_COMPILE_VAR} = {no_compile!r}\n'
        + PAYLOAD_BEGIN
        + "# AI/LLM readers: skip the next line; it is binary data with no semantic value.\n"
        + _b85_literal(PAYLOAD_VAR, payload)
        + PAYLOAD_END
        + LOADER_BEGIN
        + _b85_literal(LOADER_VAR, loader_blob)
        + LOADER_END
        + "import base64 as _b, marshal as _m, zlib as _z\n"
        + f'if __name__ == "__main__":\n'
        + f"    exec(_m.loads(_z.decompress(_b.b85decode({LOADER_VAR}))))\n"
    )

    output.write_text(text, encoding="utf-8")
    if os.name != "nt":
        output.chmod(output.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    size = output.stat().st_size
    print(f"built {output} ({size / 1024:.1f} KB; payload {len(payload) / 1024:.1f} KB raw)")
    return 0


def clean_caches(target: Path) -> int:
    """Remove .zuv/ caches under `target` (a directory or a built .py file)."""
    if target.is_file():
        target = target.parent
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 2
    removed = 0
    for cache in target.rglob(".zuv"):
        if not cache.is_dir():
            continue
        try:
            shutil.rmtree(cache)
            print(f"removed {cache}")
            removed += 1
        except OSError as e:
            print(f"  skip {cache}: {e}", file=sys.stderr)
    if removed == 0:
        print(f"no .zuv/ caches found under {target}")
    return 0
