import argparse
import platform
import subprocess
import sys
from pathlib import Path

from . import __version__
from .constants import WHEEL_PLATFORMS
from .inspector import inspect
from .modules.build import build_pyz
from .modules.cache import clean_caches


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
        "--clean",
        action="store_true",
        help="Wipe the output's parent directory before building (was implicit for dist/ in <=0.0.2).",
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
        default_suffix = ".zip" if args.make_zip else ".py"
        if args.output is None:
            output = Path.cwd() / "dist" / f"{project_dir.name}{default_suffix}"
        else:
            output = Path(args.output).expanduser().resolve()
            if output.suffix == "":
                output = output.with_suffix(default_suffix)
            elif args.make_zip and output.suffix != ".zip":
                output = output.with_suffix(".zip")
        return build_pyz(
            project_dir=project_dir,
            output=output,
            entry=args.entry,
            clean=args.clean,
            embed_deps=deps_platforms,
            no_compile=args.no_compile,
            make_zip=args.make_zip,
        )

    if args.command == "inspect":
        return inspect(Path(args.file).expanduser().resolve())

    if args.command == "clean":
        return clean_caches(Path(args.target).expanduser().resolve())

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


if __name__ == "__main__":
    sys.exit(main())
