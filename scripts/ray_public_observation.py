#!/usr/bin/env python3
"""Run the ray public observation command from a source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ravora.ray_public_cli import main

if __name__ == '__main__':
    main()
