"""Portable entry point: python run.py, from a checkout with requirements installed."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path[:0] = [str(root / "backend"), str(root / "backend" / "vendor")]

from oni_web.__main__ import main

if __name__ == "__main__":
    main()
