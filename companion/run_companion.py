"""Entry point used by PyInstaller (DeskMediaCompanion.exe). Same as: python -m desk_media_companion"""
import sys

from desk_media_companion.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
