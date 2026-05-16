"""Runtime loader embedded inside every .zuv file.

Built into the .zuv as plain source. uv executes the file via PEP 723 with an
empty dependency list, so this loader runs in a minimal env (stdlib only).

On first run, the loader:
  1. Materializes a `.venv` next to the .zuv using `uv venv` + `uv pip install`.
  2. Extracts the embedded user source tarball to `<dir>/.zuv/<name>_<hash>/`.
  3. Writes a `.zuv-ready` marker.
On every run:
  4. Prepends the venv's site-packages and the extracted source dir to sys.path.
  5. chdirs to the .zuv's folder so user code can use cwd-relative paths
     (e.g. `load_dotenv('.env')`, `open('frontend/index.html')`).
  6. Exports ZUV_DIR / ZUV_CWD / ZUV_CACHE env vars.
  7. Imports the entry callable and invokes it.
"""
import base64
import io
import os
import subprocess
import sys
import tarfile
from importlib import import_module
from pathlib import Path

# Builder injects these literals above the loader body:
#   _ZUV_ENV: dict   (entry, build_id, build_tag, dependencies)
#   _ZUV_PAYLOAD: bytes  (base85 of the tar.gz of the user source tree)


def _venv_site_packages(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Lib" / "site-packages"
    py = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return venv_dir / "lib" / py / "site-packages"


def _uv(*args: str) -> None:
    proc = subprocess.run(["uv", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"error: uv {' '.join(args)} failed (exit {proc.returncode})")


def _ensure_venv(venv_dir: Path, deps: list[str]) -> None:
    if not (venv_dir / "pyvenv.cfg").exists():
        _uv("venv", str(venv_dir), "--python", sys.executable, "--quiet")
    if deps:
        py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _uv("pip", "install", "--python", str(py), "--quiet", *deps)


def _extract(payload: bytes, target: Path) -> None:
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
        tf.extractall(tmp, filter="data")
    if target.exists():
        import shutil
        shutil.rmtree(target)
    tmp.rename(target)


def _import_callable(target: str):
    module_name, _, attr = target.partition(":")
    obj = import_module(module_name)
    for part in attr.split(".") if attr else ():
        obj = getattr(obj, part)
    return obj


def _run() -> int:
    env = _ZUV_ENV  # noqa: F821 - injected by builder
    archive_path = Path(sys.argv[0]).resolve()
    arch_dir = archive_path.parent

    venv_dir = arch_dir / ".venv"
    cache = arch_dir / ".zuv" / f"{archive_path.stem}_{env['build_id'][:12]}"
    marker = cache / ".zuv-ready"
    deps = env.get("dependencies", [])

    if not marker.exists():
        _ensure_venv(venv_dir, deps)
        _extract(base64.b85decode(_ZUV_PAYLOAD), cache)  # noqa: F821 - injected by builder
        marker.write_text("ok")
    elif deps and not (venv_dir / "pyvenv.cfg").exists():
        _ensure_venv(venv_dir, deps)

    sys.path.insert(0, str(cache))
    sp = _venv_site_packages(venv_dir)
    if sp.is_dir():
        sys.path.insert(0, str(sp))

    os.environ["ZUV_DIR"] = str(arch_dir)
    os.environ["ZUV_CWD"] = str(arch_dir)
    os.environ["ZUV_CACHE"] = str(cache)
    os.chdir(arch_dir)

    return int(_import_callable(env["entry"])() or 0)


if __name__ == "__main__":
    sys.exit(_run())
