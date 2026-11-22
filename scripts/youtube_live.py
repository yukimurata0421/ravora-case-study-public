#!/usr/bin/env python3
"""Start the optional live studio from a source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ravora.youtube_live import main

if __name__ == '__main__':
    main()
