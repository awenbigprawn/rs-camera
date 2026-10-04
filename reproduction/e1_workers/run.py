#!/usr/bin/env python3
"""Capture/check this experiment or analyze and plot its saved results."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reproduce import experiment_main

if __name__ == "__main__":
    experiment_main('e1_workers')
