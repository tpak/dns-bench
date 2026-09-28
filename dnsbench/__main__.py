"""``python -m dnsbench``: the same command line as the ``dns-bench`` launcher.

The Python version is checked before the rest of the package is imported, so an older Python gets
the launcher's clear message instead of a SyntaxError or ImportError from deep inside dnsbench.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 13):  # noqa: UP036 - false on the 3.13 target, but older Pythons run this line
    sys.exit("dns-bench needs Python 3.13 or newer (found {}.{})".format(*sys.version_info[:2]))

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
