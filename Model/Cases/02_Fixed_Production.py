"""Eight routes at 300 kt/y recovered NH3 delivery.

Run: python 02_Fixed_Production.py --help
Outputs are kept separately from the published Results tables.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Ammonia_MILP import run_case

if __name__ == "__main__":
    run_case('fixed_production')
