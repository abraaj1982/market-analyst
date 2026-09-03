"""Single source of truth for the code version stamped onto every analysis."""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

__version__ = "1.0.0"


@lru_cache(maxsize=1)
def code_version() -> str:
    """`<semver>+<git sha>` when in a repo, otherwise just the semver.

    Stamped onto every stored analysis so that months of accumulated statistics
    stay attributable to the exact code that produced them.
    """
    root = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except Exception:
        return __version__
    return f"{__version__}+{sha}" if sha else __version__
