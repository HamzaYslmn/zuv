"""Build-time support for the bundle's self-update feature.

The *runtime* update logic lives in `zuv/_loader_template.py` because it has
to run inside the bundle (no access to the `zuv` package). This module is the
single source of truth for everything *build-time*: parsing the CLI flags,
deriving defaults, and baking the four `_ZUV_UPDATE_*` globals into the .py.

Updates pull from the GitHub or GitLab Releases API. Recipients see a prompt
when an asset's id changes (GitHub) or the release's released_at changes
(GitLab) — i.e. when the publisher re-uploads the bundle.
"""
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..constants import (
    UPDATE_FILE_VAR,
    UPDATE_PROVIDER_VAR,
    UPDATE_REPO_VAR,
    UPDATE_TAG_VAR,
)

DEFAULT_TAG = "latest"  # special value: hits /releases/latest endpoint
DEFAULT_PROVIDER = "github"
PROVIDERS = ("github", "gitlab")


@dataclass(frozen=True)
class UpdateConfig:
    """Auto-update target for a bundle. `None` everywhere means disabled."""
    provider: str  # "github" or "gitlab"
    repo: str      # "user/repo"
    tag: str       # release tag, or "latest" for the most-recent release
    file: str      # asset filename inside the release, e.g. "fastapi.zuv.py"


def _parse_repo(spec: str) -> tuple[str, str]:
    """Return (provider, 'user/repo') from a full URL or a 'user/repo' shorthand.

    Accepted forms:
      https://github.com/user/repo[.git]
      https://gitlab.com/user/repo[.git]
      user/repo                      -> defaults to github
      gitlab:user/repo               -> explicit gitlab shorthand
      github:user/repo               -> explicit github shorthand
    """
    spec = spec.strip()
    if spec.startswith(("http://", "https://")):
        u = urlparse(spec)
        host = (u.netloc or "").lower()
        path = u.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        # Trim trailing /releases or /releases/tag if user pasted the page URL.
        if "/releases" in path:
            path = path.split("/releases", 1)[0]
        if path.count("/") != 1:
            raise ValueError(f"expected URL path like /user/repo, got {u.path!r}")
        if "github.com" in host:
            return "github", path
        if "gitlab.com" in host:
            return "gitlab", path
        raise ValueError(f"unsupported host {host!r}; supported: github.com, gitlab.com")
    if ":" in spec and spec.split(":", 1)[0] in PROVIDERS:
        provider, repo = spec.split(":", 1)
        if repo.count("/") != 1:
            raise ValueError(f"expected user/repo after '{provider}:', got {repo!r}")
        return provider, repo
    if spec.count("/") == 1:
        return DEFAULT_PROVIDER, spec
    raise ValueError(
        f"could not parse --update-repo {spec!r}; use 'user/repo', "
        f"'gitlab:user/repo', or a full https URL"
    )


def from_cli(
    repo: str | None,
    tag: str | None,
    file: str | None,
    output: Path,
) -> UpdateConfig | None:
    """Turn raw CLI args into an UpdateConfig, or None if --update-repo absent.
    Derives `file` from the output stem when not given so the recipient and
    publisher line up by default (e.g. dist/fastapi.zuv.py -> fastapi.zuv.py).
    """
    if not repo:
        return None
    provider, parsed_repo = _parse_repo(repo)
    if not file:
        # output.stem strips one suffix: foo.zuv.py -> foo.zuv, foo.zuv.zip -> foo.zuv.
        # Append .py to get the canonical .zuv.py filename.
        stem = output.stem
        if not stem.endswith(".zuv"):
            stem += ".zuv"
        file = stem + ".py"
    return UpdateConfig(
        provider=provider, repo=parsed_repo, tag=tag or DEFAULT_TAG, file=file,
    )


def bake(cfg: UpdateConfig | None) -> str:
    """Return the Python assignment lines that the loader template reads on
    startup. When `cfg` is None, all values are empty / defaults — the loader
    checks `repo` and skips the update path when empty."""
    provider = cfg.provider if cfg else DEFAULT_PROVIDER
    repo = cfg.repo if cfg else ""
    tag = cfg.tag if cfg else DEFAULT_TAG
    file = cfg.file if cfg else ""
    return (
        f"{UPDATE_PROVIDER_VAR} = {provider!r}\n"
        f"{UPDATE_REPO_VAR} = {repo!r}\n"
        f"{UPDATE_TAG_VAR} = {tag!r}\n"
        f"{UPDATE_FILE_VAR} = {file!r}\n"
    )


def describe(cfg: UpdateConfig) -> str:
    """Human-readable one-liner for the build log."""
    return f"{cfg.provider}:{cfg.repo} (release {cfg.tag}, asset {cfg.file})"
