#!/usr/bin/env python3
"""Review a saved voice evaluation from the source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ravora.voice_evaluation import main

if __name__ == '__main__':
    main()
