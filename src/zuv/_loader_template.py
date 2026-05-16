"""Runtime loader embedded in every zuv-built .py.

First run: decode the embedded tar.gz of the uv project and extract it to
.zuv/<stem>_<hash>/ next to the script. Every run: exec
`uv run --project <extracted> <entry>`. uv creates the project's .venv inside
the extracted dir (always the same place) and runs the entry script.

Globals injected by the builder above this code:
  _ZUV_ENTRY:    str   entry script path, relative to project root
  _ZUV_BUILD_ID: str   short hash for cache namespacing
  _ZUV_PAYLOAD:  bytes base85 of the project tar.xz
"""
import base64
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

_DROP_ENV = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONHOME", "PYTHONPATH")


def _run() -> int:
    script = Path(sys.argv[0]).resolve()
    cache = script.parent / ".zuv" / f"{script.stem}_{_ZUV_BUILD_ID}"  # noqa: F821

    if not cache.exists():
        tmp = cache.with_name(cache.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(base64.b85decode(_ZUV_PAYLOAD)), mode="r:xz") as tf:  # noqa: F821
            tf.extractall(tmp, filter="data")
        tmp.rename(cache)

    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    cmd = ["uv", "run", "--project", str(cache), str(cache / _ZUV_ENTRY), *sys.argv[1:]]  # noqa: F821
    return subprocess.call(cmd, cwd=str(cache), env=env)


if __name__ == "__main__":
    sys.exit(_run())
