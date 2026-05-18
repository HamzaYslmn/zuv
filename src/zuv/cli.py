import argparse
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from . import __version__
from .constants import VOLUME_MARKER, VOLUME_VAR, WHEEL_PLATFORMS
from .inspector import inspect
from .modules.build import build_pyz
from .modules.cache import clean_caches
from .modules.updater import from_cli as updater_from_cli


def _host_platform() -> str | None:
    sysname = platform.system()
    arch = platform.machine().lower()
    is_arm = arch in ("arm64", "aarch64")
    if sysname == "Windows":
        return "windows"
    if sysname == "Linux":
        return "linux-arm" if is_arm else "linux"
    if sysname == "Darwin":
        return "macos-arm" if is_arm else "macos"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zuv",
        description="Build click-and-run Python apps powered by uv.",
    )
    parser.add_argument("--version", action="version", version=f"zuv {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build a single-file Python app from a uv project.")
    build.add_argument(
        "project",
        nargs="?",
        default=".",
        help="Path to the uv project (containing pyproject.toml). Default: cwd.",
    )
    build.add_argument(
        "-o", "--output",
        default=None,
        help="Output file path. Default: <cwd>/dist/<project-name>.py.",
    )
    build.add_argument(
        "-e", "--entry",
        default=None,
        help="Entry script relative to project root. Default: 'src/main.py' if it exists else 'main.py', or [tool.zuv].entry.",
    )
    build.add_argument(
        "--no-compile",
        dest="no_compile",
        action="store_true",
        help=(
            "Tell the loader to skip the on-first-run .py->.pyc compile step. "
            "The extracted cache stays as plain .py sources (no __pycache__/). "
            "Slower per-import startup; useful when bytecode files are undesired."
        ),
    )
    build.add_argument(
        "--zip",
        dest="make_zip",
        action="store_true",
        help=(
            "Package the .py bundle into a .zip alongside run.bat and run.sh "
            "launchers. The launchers auto-install uv if missing, then run the "
            "bundle. Recipient extracts the zip and double-clicks run.bat "
            "(Windows) or ./run.sh (Unix/macOS) -- no Python or uv needed first."
        ),
    )
    build.add_argument(
        "--update-repo",
        dest="update_repo",
        default=None,
        metavar="REPO",
        help=(
            "GitHub or GitLab repo whose Releases page hosts the rolling build. "
            "Accepts a full URL (https://github.com/user/repo, "
            "https://gitlab.com/user/repo) or shorthand (user/repo for GitHub, "
            "gitlab:user/repo for GitLab). The bundle self-updates on startup: "
            "fetches the named asset from the named release and, if it changed, "
            "prompts to download. Private repos: $GH_TOKEN (GitHub) or "
            "$GITLAB_TOKEN (GitLab). ZUV_NO_UPDATE=1 disables."
        ),
    )
    build.add_argument(
        "--update-tag",
        dest="update_tag",
        default="latest",
        metavar="TAG",
        help=(
            "Release tag to fetch the asset from. Default: 'latest' (special "
            "value: hits the provider's `/releases/latest` endpoint — the most "
            "recently-published release). Any other value pins to that tag."
        ),
    )
    build.add_argument(
        "--update-file",
        dest="update_file",
        default=None,
        metavar="FILENAME",
        help="Asset filename inside the release. Default: <output-stem>.zuv.py.",
    )
    build.add_argument(
        "--deps",
        nargs="?",
        const="__host__",
        default=None,
        metavar="PLATFORMS",
        help=(
            "Embed wheels for the project's locked deps so the bundle runs "
            "offline. With no value, bundles your current OS only. Pass 'all' "
            "for every platform, or a comma-list: windows, linux, linux-arm, "
            "macos, macos-arm. Example: --deps windows,linux"
        ),
    )
    insp = sub.add_parser("inspect", help="Print an LLM-friendly summary of a built .py (payload elided).")
    insp.add_argument("file", help="Path to a zuv-built .py file.")

    clean = sub.add_parser("clean", help="Remove .zuv/ cache directories.")
    clean.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Directory to walk (or a built .py — its parent is used). Default: cwd.",
    )
    clean.add_argument(
        "--data",
        action="store_true",
        help="Also wipe persistent volume directories (DESTRUCTIVE; default keeps them).",
    )

    vol = sub.add_parser("volume", help="Manage a bundle's persistent volume.")
    vol_sub = vol.add_subparsers(dest="vol_command", required=True)
    vol_locate = vol_sub.add_parser("locate", help="Print the on-disk path of the volume.")
    vol_locate.add_argument("file", help="Path to a zuv-built .py file.")
    vol_wipe = vol_sub.add_parser("wipe", help="Delete the persistent volume (data loss).")
    vol_wipe.add_argument("file", help="Path to a zuv-built .py file.")
    vol_wipe.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt.")
    vol_backup = vol_sub.add_parser("backup", help="Write a tar.gz of the persistent volume.")
    vol_backup.add_argument("file", help="Path to a zuv-built .py file.")
    vol_backup.add_argument("-o", "--output", default=None, help="Output tar.gz path.")

    run = sub.add_parser(
        "run",
        help="Run a zuv-built .py via uv (thin wrapper over `uv run`).",
    )
    run.add_argument("file", help="Path to a zuv-built .py file.")
    run.add_argument("script_args", nargs=argparse.REMAINDER, help="Arguments forwarded to the script.")

    args = parser.parse_args(argv)

    if args.command == "build":
        deps_platforms: list[str] | None = None
        if args.deps is not None:
            if args.deps == "__host__":
                host = _host_platform()
                if host is None:
                    print(
                        "error: could not detect host platform for --deps; "
                        f"pass one explicitly: {', '.join(WHEEL_PLATFORMS)}",
                        file=sys.stderr,
                    )
                    return 2
                deps_platforms = [host]
            elif args.deps == "all":
                deps_platforms = list(WHEEL_PLATFORMS)
            else:
                deps_platforms = [p.strip() for p in args.deps.split(",") if p.strip()]
                unknown = [p for p in deps_platforms if p not in WHEEL_PLATFORMS]
                if unknown:
                    print(
                        f"error: unknown --deps platform(s): {', '.join(unknown)}. "
                        f"Valid: {', '.join(WHEEL_PLATFORMS)}, all",
                        file=sys.stderr,
                    )
                    return 2
        project_dir = Path(args.project).expanduser().resolve()
        # .zuv.{py,zip} is zuv's own protocol-marker extension. Keeps .py
        # last so `uv run app.zuv.py` is still recognised by uv.
        default_suffix = ".zuv.zip" if args.make_zip else ".zuv.py"
        if args.output is None:
            output = Path.cwd() / "dist" / f"{project_dir.name}{default_suffix}"
        else:
            output = Path(args.output).expanduser().resolve()
            if output.suffix == "":
                output = output.with_suffix(default_suffix)
            elif args.make_zip and output.suffix != ".zip":
                output = output.with_suffix(".zip")
        try:
            update = updater_from_cli(
                args.update_repo, args.update_tag, args.update_file, output
            )
        except ValueError as e:
            print(f"error: --update-repo: {e}", file=sys.stderr)
            return 2
        return build_pyz(
            project_dir=project_dir,
            output=output,
            entry=args.entry,
            embed_deps=deps_platforms,
            no_compile=args.no_compile,
            make_zip=args.make_zip,
            update=update,
        )

    if args.command == "inspect":
        return inspect(Path(args.file).expanduser().resolve())

    if args.command == "clean":
        return clean_caches(
            Path(args.target).expanduser().resolve(),
            include_data=args.data,
        )

    if args.command == "volume":
        return _volume_cli(args)

    if args.command == "run":
        target = Path(args.file).expanduser().resolve()
        if not target.is_file():
            print(f"error: not a file: {target}", file=sys.stderr)
            return 2
        try:
            return subprocess.call(["uv", "run", str(target), *args.script_args])
        except FileNotFoundError:
            print(
                "error: 'uv' not found on PATH. Install it from https://astral.sh/uv.",
                file=sys.stderr,
            )
            return 127

    parser.print_help()
    return 1


def _volume_dirname(mount: str) -> str:
    return mount.strip("/\\").replace("\\", "/").replace("/", "_")


def _read_volume_path(bundle: Path) -> str | None:
    """Parse the bundle for `_ZUV_VOLUME_PATH`. Returns the path, or None if
    the bundle has no volume configured / isn't a zuv-built file."""
    try:
        text = bundle.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        print(f"error: cannot read {bundle}: {e}", file=sys.stderr)
        return None
    m = re.search(rf'^{re.escape(VOLUME_VAR)}\s*=\s*[\'"]([^\'"]*)[\'"]', text, re.MULTILINE)
    return m.group(1) if m else None


def _volume_cli(args: argparse.Namespace) -> int:
    bundle = Path(args.file).expanduser().resolve()
    if not bundle.is_file():
        print(f"error: not a file: {bundle}", file=sys.stderr)
        return 2
    mount = _read_volume_path(bundle)
    if mount is None:
        print(f"error: not a zuv-built file (no {VOLUME_VAR}): {bundle}", file=sys.stderr)
        return 2
    if not mount:
        print(f"error: bundle has no [tool.zuv].volume configured: {bundle}", file=sys.stderr)
        return 2
    volume = bundle.parent / ".zuv" / _volume_dirname(mount)

    if args.vol_command == "locate":
        print(volume)
        return 0 if volume.exists() else 1

    if args.vol_command == "wipe":
        if not volume.exists():
            print(f"nothing to wipe: {volume}")
            return 0
        if not args.yes:
            try:
                ans = input(f"WARNING: delete {volume}? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
            if ans not in ("y", "yes"):
                print("cancelled")
                return 0
        try:
            shutil.rmtree(volume)
            print(f"removed {volume}")
            return 0
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if args.vol_command == "backup":
        if not volume.exists():
            print(f"error: volume does not exist: {volume}", file=sys.stderr)
            return 1
        out = args.output or f"{bundle.stem}_volume_{int(time.time())}.tar.gz"
        out_path = Path(out).expanduser().resolve()
        try:
            with tarfile.open(out_path, "w:gz") as tf:
                for item in volume.rglob("*"):
                    if item.name == VOLUME_MARKER:
                        continue
                    tf.add(item, arcname=item.relative_to(volume).as_posix())
            print(f"backed up {volume} -> {out_path}")
            return 0
        except (OSError, tarfile.TarError) as e:
            print(f"error: backup failed: {e}", file=sys.stderr)
            return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
