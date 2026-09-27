"""``python -m dnsbench``: the same command line as the ``dns-bench`` launcher."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
