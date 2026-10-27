"""Ru+ALK annual weather cases, retaining the 8784-hour leap year.

Run: python 10_Weather_Years.py --help
Outputs are kept separately from the published Results tables.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Ammonia_MILP import run_case

if __name__ == "__main__":
    run_case('weather')
