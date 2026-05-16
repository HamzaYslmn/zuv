import argparse
import sys
from pathlib import Path

from . import __version__
from .builder import build_pyz
from .inspector import inspect


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

    insp = sub.add_parser("inspect", help="Print an LLM-friendly summary of a built .py (payload elided).")
    insp.add_argument("file", help="Path to a zuv-built .py file.")

    args = parser.parse_args(argv)

    if args.command == "build":
        project_dir = Path(args.project).expanduser().resolve()
        if args.output is None:
            output = Path.cwd() / "dist" / f"{project_dir.name}.py"
        else:
            output = Path(args.output).expanduser().resolve()
        if output.suffix == "":
            output = output.with_suffix(".py")
        return build_pyz(
            project_dir=project_dir,
            output=output,
            entry=args.entry,
        )

    if args.command == "inspect":
        return inspect(Path(args.file).expanduser().resolve())

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
