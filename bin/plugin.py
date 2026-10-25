#!/usr/bin/env python3
"""Install and activate the AS-BOT plugin in an existing official Hermes profile."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    # Keep client.py as the backwards-compatible entry point and wire protocol.
    runpy.run_path(str(Path(__file__).with_name('client.py')), run_name='__main__')
