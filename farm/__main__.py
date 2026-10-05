"""Enables `python -m farm ...`."""

from __future__ import annotations

import sys

from farm.cli import main

if __name__ == "__main__":
    sys.exit(main())