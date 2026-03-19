#!/usr/bin/env python3
from wifimimo_core import STATE_PATH

if STATE_PATH.exists():
    print(STATE_PATH.read_text(encoding="utf-8"), end="")
