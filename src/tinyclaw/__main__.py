"""tinyClaw CLI entry point.

Usage:
    python -m tinyclaw --help
    python -m tinyclaw --mode full
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from main import main

if __name__ == "__main__":
    main()
