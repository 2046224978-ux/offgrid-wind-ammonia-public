"""Fixed-design PEM parity and equipment-price paths.

Run: python 07_PEM_Cost_Competition.py --help
Outputs are kept separately from the published Results tables.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Ammonia_MILP import run_case

if __name__ == "__main__":
    run_case('pem_cost')
