#!/usr/bin/env python3
"""Emit the current wifimimo state file for the plasmoid."""

from __future__ import annotations

import sys

from wifimimo_core import STATE_PATH


def main() -> int:
    try:
        sys.stdout.write(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
