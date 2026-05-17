import sys
from pathlib import Path

import pydantic
import pydantic_core
import rich
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


class Report(BaseModel):
    name: str = "bigtest"
    version: str = "0.1.1"
    python: str = Field(default_factory=lambda: sys.version.split()[0])
    args: list[str] = Field(default_factory=list)


def _inspect(mod) -> tuple[str, str, bool]:
    """Return (loaded-from-suffix, path, has-pyc-cache)."""
    f = getattr(mod, "__file__", None)
    if not f:
        return ("builtin", "<built-in>", False)
    p = Path(f)
    suffix = p.suffix
    has_pyc = False
    if suffix == ".py":
        tag = sys.implementation.cache_tag
        pyc = p.parent / "__pycache__" / f"{p.stem}.{tag}.pyc"
        has_pyc = pyc.exists()
    return (suffix, str(p), has_pyc)


def main() -> int:
    console = Console()
    report = Report(args=sys.argv[1:])

    table = Table(title="zuv bigtest \u2014 runtime report", show_lines=False)
    table.add_column("Module", style="cyan")
    table.add_column("Suffix", style="magenta")
    table.add_column(".pyc cached", style="yellow")
    table.add_column("Loaded from", style="green", overflow="fold")

    rows = [(m.__name__, *_inspect(m)) for m in (sys.modules[__name__], rich, pydantic, pydantic_core)]
    for name, suffix, path, has_pyc in rows:
        table.add_row(name, suffix, "yes" if has_pyc else ("n/a" if suffix != ".py" else "no"), path)

    console.print(Panel.fit(
        f"[bold]name[/]    = {report.name}\n"
        f"[bold]version[/] = {report.version}\n"
        f"[bold]python[/]  = {report.python}\n"
        f"[bold]args[/]    = {report.args}",
        title="pydantic Report",
        border_style="cyan",
    ))
    console.print(table)

    # Hard assertions \u2014 these are the real proof that zuv supports all three.
    cext_path = Path(pydantic_core.__file__).parent
    cext_files = list(cext_path.glob("*.pyd")) + list(cext_path.glob("*.so"))
    assert cext_files, f"expected a C-ext (.pyd/.so) inside {cext_path}"

    rich_init = Path(rich.__file__)
    rich_pyc = rich_init.parent / "__pycache__" / f"__init__.{sys.implementation.cache_tag}.pyc"
    assert rich_pyc.exists(), f"expected pre-compiled .pyc at {rich_pyc}"

    console.print(f"[bold green]ALL CHECKS PASSED[/] (C-ext: {cext_files[0].name}, .pyc: {rich_pyc.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
