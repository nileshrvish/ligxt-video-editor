"""File helpers."""
from __future__ import annotations

import time
from pathlib import Path


def replace_retry(tmp: Path, dest: Path, attempts: int = 40, wait: float = 0.025) -> None:
    """Atomic replace that survives Windows sharing violations: replacing a file that another
    request has open for reading at that moment fails with 'Access is denied' (seen when the UI
    polls media status while a background job saves it). Retries for up to ~1 s."""
    for i in range(attempts):
        try:
            tmp.replace(dest)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait)
