"""Enables `python -m collect ...`."""

from __future__ import annotations

import sys

from collect.cli import main

if __name__ == "__main__":
    sys.exit(main())
