"""Where dns-bench finds its files: the web UI in the package, and the user's data (config.json, runs/).

The data lives in the checkout, next to the code: ``./dns-bench``, ``python -m dnsbench`` and an
editable install (``uv tool install --editable .``) all run the checkout's code, so they share one
config and one set of runs. ``DNSBENCH_HOME`` moves both somewhere else, and ``--config`` /
``--runs-dir`` override each one.

A non-editable install copies the package into site-packages, where there is no checkout to keep data
in. Rather than guess a location, resolving the paths then fails with a message asking for
``DNSBENCH_HOME``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
CHECKOUT_DIR = PACKAGE_DIR.parent
HOME_ENV = "DNSBENCH_HOME"
CONFIG_NAME = "config.json"
RUNS_NAME = "runs"


class DataHomeError(Exception):
    """There is nowhere to keep config.json or runs/: not in a checkout, and DNSBENCH_HOME is unset."""


@dataclass(frozen=True)
class DataPaths:
    config: Path
    runs_dir: Path


def in_checkout(root: Path | None = None) -> bool:
    """True if ``root`` (default: the directory above the package) is a dns-bench checkout.

    Both files are checked because site-packages, the parent of an installed package, holds neither.
    """
    root = CHECKOUT_DIR if root is None else root
    return (root / "pyproject.toml").is_file() and (root / "dns-bench").is_file()


def _absolute(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(value)))


def data_home(environ: Mapping[str, str] | None = None, checkout: Path | None = None) -> Path:
    """``$DNSBENCH_HOME`` if set, else the checkout. DataHomeError if neither is available."""
    environ = os.environ if environ is None else environ
    home = environ.get(HOME_ENV, "").strip()
    if home:
        return _absolute(home)
    checkout = CHECKOUT_DIR if checkout is None else checkout
    if in_checkout(checkout):
        return checkout
    raise DataHomeError(
        f"dns-bench is installed outside its checkout ({PACKAGE_DIR}), so it doesn't know where to keep "
        f"{CONFIG_NAME} and {RUNS_NAME}/. Set {HOME_ENV} to a directory for them (for example "
        f"`export {HOME_ENV}=~/dns-bench`), pass --config and --runs-dir, or install the checkout "
        "with `uv tool install --editable .`"
    )


def resolve(
    config: str | os.PathLike[str] | None = None,
    runs_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    checkout: Path | None = None,
) -> DataPaths:
    """Absolute paths of the config file and the runs directory.

    An explicit ``config`` or ``runs_dir`` wins; the other comes from ``data_home()``. With both given,
    no data home is needed, so a non-editable install works with just the two flags.
    """
    if config is not None and runs_dir is not None:
        return DataPaths(_absolute(config), _absolute(runs_dir))
    home = data_home(environ, checkout)
    return DataPaths(
        _absolute(config) if config is not None else home / CONFIG_NAME,
        _absolute(runs_dir) if runs_dir is not None else home / RUNS_NAME,
    )
