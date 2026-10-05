"""Enables `python -m gates ...`."""

from __future__ import annotations

import sys

from gates.cli import main

if __name__ == "__main__":
    sys.exit(main())