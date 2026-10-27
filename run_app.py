"""Launcher used by PyInstaller (and handy for a plain `python run_app.py`).

Supports the same CLI flags as `python -m wdgwars_uploader` so the built exe can
also do `WardriveUploader.exe --once` / `--detect` / `--check`.
"""

import sys

from wdgwars_uploader.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
