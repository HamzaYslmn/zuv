"""Embed locked-deps wheels into the bundle so it installs offline.
Used by `zuv build --deps`.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from ..constants import WHEEL_PLATFORMS


def download_wheels(project_dir: Path, dest: Path, platforms: list[str]) -> int:
    """Export the project's locked deps and download wheels for each platform
    label into `dest`. Pure-Python wheels (`*-none-any.whl`) dedupe naturally
    because pip writes by filename. Returns the count of wheel files staged.

    Uses the builder's Python X.Y as the wheel-target Python version, matching
    the lockfile's resolution. Sdist-only deps are skipped (--only-binary=:all:).
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
            plat_args: list[str] = []
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
