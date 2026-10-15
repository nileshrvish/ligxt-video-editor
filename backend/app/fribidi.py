"""Load the bundled GNU FriBiDi (bin/fribidi, LGPL-2.1+) before Pillow's ImageFont is imported anywhere.

Pillow's raqm text layout (HarfBuzz is in the wheel) looks for FriBiDi once, when PIL.ImageFont is first imported;
on Windows it isn't on the system, so complex scripts would fall back to unshaped text. Import this module first.
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

DLL = Path(__file__).resolve().parents[2] / "bin" / "fribidi" / "libfribidi-0.dll"
loaded = False
if sys.platform == "win32" and DLL.exists():
    try:
        ctypes.CDLL(str(DLL))
        loaded = True
    except OSError:
        loaded = False
