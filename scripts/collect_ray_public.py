#!/usr/bin/env python3
"""Validate and publish an aggregate observation from this checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ravora.observation_site import main

if __name__ == '__main__':
    sys.exit(main())
