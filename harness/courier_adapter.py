"""Sole contact point with the courier inbox: subprocess only, never the `cox` package."""

from __future__ import annotations

import shutil
import subprocess


def send(ref: str, to: str, note: str) -> bool:
    """Best-effort `cox courier send`; absent `cox` or any failure returns False, never raises."""
    if shutil.which("cox") is None:
        return False
    try:
        result = subprocess.run(
            ["cox", "courier", "send", ref, "--to", to, "--note", note],
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0
