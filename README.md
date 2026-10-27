# Off-grid wind-to-ammonia model

Research code supporting the manuscript:

**Which electrolysis-synthesis combination minimises green-ammonia cost? A comparison of eight off-grid wind-powered routes under common boundaries**

The model compares alkaline (ALK), anion-exchange-membrane (AEM), proton-exchange-membrane (PEM), and solid-oxide (SOEC) electrolysis, each coupled with an iron-based (Fe) or ruthenium-based (Ru) ammonia synthesis loop. The baseline synthesis nameplate is 475 kt/year. Nameplate capacity is not actual annual output. Costs use recovered liquid-ammonia component mass as the denominator in the reported comparison.

## Repository scope

- `Model/Ammonia_MILP.py`: unchanged process, equipment, economics, capacity and hourly-dispatch implementation.
- `Model/Cases/`: 12 case entry points.
- `Model/Resource_Inputs.xlsx`: complete hourly wind years for 2021-2025 and four regional resource profiles for 2025.
- `Model/Case_Inputs/`: retained baseline cost ledgers, sensitivity-design records, and external scenario price paths.
- `Plotting/Data/Fig08_LAEC_Sampled_Results.csv`: retained sampling table required to select the default utilisation targets for Case 06. It is not substituted for new optimisation outputs.
- `checks/validate_inputs.py`: portable syntax, input-integrity, time-series and cost-ledger checks.
- `SOURCE_MANIFEST.json`: SHA-256 hashes of the packaged model files and the added, unchanged sampling table.
- `AUDIT_REPORT.md`: checks performed, findings, and limits of verification.

This is a model-only package, not the complete manuscript, all figure code, Aspen project files or all retained solver witnesses. The other published figure tables mentioned in the original module documentation are not included here. No manuscript or cover letter is included.

## Installation

Use Python 3.11 or later in a separate environment:

```sh
python -m venv .venv
```

Activate it with `.venv\Scripts\activate` on Windows or `source .venv/bin/activate` on macOS/Linux, then run:

```sh
python -m pip install -r requirements-model.txt
```

`requirements-model.txt` contains the dependencies actually imported by the model. `requirements-tested.txt` records the model dependency versions used in the packaging audit on Windows with Python 3.13.2; it is not a guarantee of identical numerical behaviour on every platform. The original `Model/requirements.txt` is preserved and also lists plotting-related packages that are not needed for this model-only package.

### Solvers

The formal hourly integer baseline, fixed-production, fixed-wind and weather calculations require a separately installed and appropriately licensed **Gurobi command-line executable**. Installing the Python requirements does not install Gurobi or provide a licence. Specify `--gurobi PATH_TO_GUROBI_CL`, put `gurobi_cl` on `PATH`, or set `GUROBI_CL`. The original executable interface was developed for Gurobi 10.0.1.

The LAEC, reduced-PEM LAEC, regional and wind-PV cases use **HiGHS** through `highspy`. These are continuous approximations, not equivalent replacements for the integer baseline. Cost-ledger calculations do not require a fresh optimisation or a Gurobi licence.

Never commit solver licences, access tokens, passwords or private keys.

## Quick checks

Run these commands from the repository root:

```sh
python checks/validate_inputs.py
python Model/Ammonia_MILP.py --help
python Model/Ammonia_MILP.py --check --route Ru+ALK
python Model/Cases/06_LAEC_Utilisation.py --check --route Ru+ALK --targets 70
```

`--check` assembles matrices for optimisation cases and reconciles the baseline ledger for economic cases. It does not solve the optimisation or prove that manuscript results have been replicated. In particular, the sensitivity, carbon-price and cost-path inputs are not all exercised by their `--check` branch; actual economic runs are needed to test those paths.

New outputs default to `Results/New_<case>/`. Use `--output PATH` to choose another directory. Keep each rerun in a separate output folder. The program rejects an output folder inside `Plotting/Data`, but it does not protect every other existing folder from overwriting.

## Cases

| Entry point in `Model/Cases/` | Calculation | Important scope |
| --- | --- | --- |
| `01_Baseline.py` | Eight routes, 475 kt/year nameplate | Formal hourly integer commitment |
| `02_Fixed_Production.py` | Fixed 300 kt/year recovered liquid product | Candidate synthesis nameplates 100-1000 kt/year, 25 kt/year steps |
| `03_Fixed_Wind.py` | Fixed 1 GW wind capacity | Candidate synthesis nameplates 100-1000 kt/year, 25 kt/year steps |
| `04_Investment_Sensitivity.py` | Investment-cost envelope | Uses retained feasible-design ledgers by default; `--reoptimize` recomputes candidate designs |
| `05_Cost_Uncertainty.py` | Cost-parameter uncertainty | 100,000 fixed-design draws, seed 20260920; not 100,000 new dispatch optimisations |
| `06_LAEC_Utilisation.py` | Levelised ammonia-energy cost versus utilisation | Continuous model; default targets read from the supplied Fig08 sampling table |
| `07_PEM_Cost_Competition.py` | PEM versus ALK investment parity | Fixed design and dispatch; conditional price trajectories |
| `08_PEM_Reduced_Cost_LAEC.py` | Reduced-cost PEM utilisation | Continuous model; PEM investment set to 2200 CNY/kW |
| `09_Green_Coal_Competition.py` | Green/coal cost and carbon-price comparisons | Fixed-design repricing and external price scenarios, not an emissions inventory |
| `10_Weather_Years.py` | Ru+ALK under 2021-2025 weather | Year-specific optimisation; 2024 includes 8784 hours; 2025 is the required reference |
| `11_Regional_Resources.py` | Four regional resource comparisons | Continuous approximation; includes a solar-only Ngari case |
| `12_Wind_PV_Colocation.py` | Wind/PV nameplate-share scenarios | Continuous approximation; inspect fractional convergence and feasibility status |

Examples:

```sh
python Model/Cases/01_Baseline.py --route Ru+ALK --time-limit 3600 --threads 8
python Model/Cases/05_Cost_Uncertainty.py
python Model/Cases/07_PEM_Cost_Competition.py
python Model/Cases/09_Green_Coal_Competition.py
python Model/Cases/06_LAEC_Utilisation.py --route Ru+ALK --targets 70 --time-limit 60
python Model/Cases/10_Weather_Years.py --years 2023 2024 2025 --time-limit 3600
python Model/Cases/12_Wind_PV_Colocation.py --sites IM_KESHIKETENG --shares 0 0.05 0.1
```

`--time-limit` is a per-solve budget, not an overall case runtime limit. A case can contain multiple solver calls and iterations. Full integer runs can be computationally expensive. Check reported incumbent feasibility, bounds, gaps and convergence; a saved output file alone is not proof of optimality.

## Interpretation and reproducibility

- The main baseline uses complete hourly years, capacity decisions, modular electrolyser operation, battery exclusivity, and cyclic storage boundaries.
- Process relationships and engineering/economic parameters are embedded in the main module. Aspen-derived approximations are not new Aspen reruns or experimental validation.
- Fixed-design cost parity and repricing results should not be described as fully reoptimised technology-transition results.
- The carbon-price calculation applies the carbon charge to the assumed coal-ammonia process emissions, with an unchanged reference coal-ammonia cost.
- Resource-year and regional inputs are supplied as retained inputs. This package audit does not independently certify the original weather-source retrieval or process calibration.
- Ten historical provenance labels retain only their original filenames; personal usernames and local directory paths have been removed. These labels are descriptive metadata, not runtime input locations. The model implementation and all scientific inputs are unchanged.

## Access and licensing

This package is prepared for anonymous reviewer access through a public GitHub repository. Viewing and downloading a public repository do not require a GitHub account. Running the formal integer cases still requires a separate Gurobi installation and licence, as explained above.

No new software or data licence has been selected or granted by this packaging step. The authors must confirm licensing and redistribution rights before any public release. Gurobi is separate proprietary software and is not distributed here.
