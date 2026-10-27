# Model package audit

Audit date: 2026-10-27.

## Preservation

At initial packaging, the 19 files under `Model/` matched the supplied `Model.zip` byte for byte. Before public release, ten provenance strings in `Model/Ammonia_MILP.py` were shortened to their original filenames to remove personal usernames and local directory paths. The other 18 model/input files remain byte-identical. No model equations, parameters, input values, retained ledgers, case scripts or scientific conclusions were edited. The original source files outside this package were not modified. `SOURCE_MANIFEST.json` records the packaged files after this metadata-only cleanup.

One unchanged input dependency was added: `Plotting/Data/Fig08_LAEC_Sampled_Results.csv`. Documentation, a model-only dependency list, the tested dependency versions, integrity hashes, and a portable input-check script were added separately.

## Findings

1. **The standalone Model.zip omits a default runtime dependency.** Case 06 reads `Plotting/Data/Fig08_LAEC_Sampled_Results.csv` when `--targets` is not supplied. That file is outside the original model archive. The repository includes the unchanged retained table at the expected relative location. The default Ru+ALK check completed for all 18 retained targets after packaging. Explicit `--targets` also works.
2. **Installing the original requirements is insufficient for the formal integer cases.** Cases 01, 02, 03 and 10 explicitly require a separately licensed Gurobi command-line executable. This is now explained in the README. No licence file is included.
3. **The economic `--check` branch is narrower than its generic description.** It checks baseline-ledger arithmetic and returns before exercising the sensitivity-design file or the coal price-path CSV inputs. Actual runs of Cases 04, 05, 07 and 09 were performed during this audit instead of relying only on `--check`.
4. **Full-year optimisation has not been newly replicated.** One continuous Ru+ALK LAEC point with a 70% utilisation target reached its 30-second limit, and a second attempt reached its 120-second limit without a verified solution. Both were explicitly rejected by the model's feasibility gate. This is an incomplete optimisation test, not evidence that the problem is infeasible or that the published solution is wrong. No longer full-year solves were run in this audit.
5. **Historical local paths were removed before public release.** Ten embedded provenance strings now contain filenames only. These strings are descriptive metadata, not runtime file dependencies. This privacy cleanup does not change model parameters or calculations.

## Completed checks

| Check | Result and scope |
| --- | --- |
| Archive/directory correspondence | Initial packaging: all 19 model/input files matched the submitted archive; public-release cleanup changes only ten provenance strings in the main module |
| Syntax | Main module and all 12 case scripts parse successfully |
| Command-line interfaces | `--help` succeeds for all 13 entry points |
| Resource workbook | Nine sheets; eight with 8760 rows and leap-year 2024 with 8784 rows; no missing or non-finite numeric values |
| Resource ranges | Supplied wind/PV capacity factors lie in [0, 1] |
| Time indexing | Five timestamped wind years have unique, regular hourly timestamps; regional profiles retain consecutive hourly indices |
| Case `--check` coverage | All 12 case entry points pass for the selections detailed below |
| Baseline cost arithmetic | All eight ledgers reconcile with their retained LCOA and the retained baseline comparison table |
| Capital annualisation | Independent discounted replacement/residual-value calculation agrees for equipment lives of 15, 20, 25 and 30 years |
| Retained Ru+ALK dispatch | Current-model feasibility and independent hourly balance checks pass; details below |
| Economic recalculations | Cases 04, 05, 07 and 09 execute successfully using retained input ledgers/designs |
| Table comparison | Recomputed numeric data in 12 corresponding tables agree with retained values within rtol 1e-9 and atol 1e-6 after matching route rows |
| Gurobi executable/licence | A separate two-variable integer smoke test returns the known optimum of 3; this is not a scientific model rerun |
| Credential scan | No matches for the inspected GitHub-token, private-key and direct credential-assignment patterns in the model text files; not a guarantee of absence of all sensitive information |
| Source integrity after execution | Original copied source/input hashes unchanged during the numerical audit; packaged checksums updated after metadata-only privacy cleanup |

Table-format differences were also checked: the baseline route table has a different row order, and sensitivity witness identifiers use `Design_XX` rather than the retained `Model/Case_Inputs/Sensitivity_Designs.json#Design_XX` prefix. These do not change the checked numerical values. Some regenerated tables have different metadata columns; equality of entire original files is not claimed.

### Matrix/input check selections

- Baseline: all eight routes, full 8760-hour 2025 profile.
- Fixed production and fixed wind: Ru+ALK, all 37 synthesis nameplate candidates assembled in each model.
- Weather: Ru+ALK, all five years 2021-2025, including 8784 hours in 2024.
- LAEC: Ru+ALK at 70%, plus all 18 default retained Ru+ALK targets.
- Reduced-cost PEM LAEC: Fe+PEM and Ru+PEM at 70%.
- Regional: all four default regions.
- Wind-PV: the two default regions at 5% PV nameplate share.
- Economic cases: baseline-ledger checks plus actual sensitivity, uncertainty, PEM-price and coal-comparison recalculations.

Matrix construction is not solution feasibility or global-optimality certification.

### Retained Ru+ALK witness verification

The existing 8760-hour Ru+ALK dispatch and baseline capacities were read from retained figure-source data outside this model-only package. They were substituted into the current model without reoptimisation.

- Variables checked: 122,647.
- Matrix rows checked: 280,327, with indicator rows enforced only when active.
- Rows exceeding the model's stated feasibility tolerance: zero.
- Bound and integrality violations: zero.
- Annual objective cost: 870,204,590.6766101 CNY, agreeing with the retained cost total with zero reported residual.
- Independent maximum electrical balance residual: approximately 1.49e-10 kW.
- Independent cyclic battery residual: approximately 3.55e-11 kWh.
- Independent cyclic hydrogen inventory residual: approximately 4.07e-10 Nm3.
- Native synthesis plus product-terminal checks: passed.

This verifies the retained Ru+ALK feasible witness under the current model. It does not independently prove a global lower bound or validate all eight retained hourly dispatches. The external retained witness files are not included in this model-only repository, so `checks/validate_inputs.py` does not repeat this witness test.

### Independently recalculated headline quantities

| Quantity | Recalculated value | Boundary |
| --- | --- | --- |
| Ru+ALK LCOA | 3040.1824 CNY/t | Recovered liquid-ammonia component |
| Ru+ALK saving versus Fe+ALK | 9.0732% | Retained baseline designs |
| Ru PEM investment reduction to match ALK | 65.1715% | Fixed design and dispatch |
| Fe PEM investment reduction to match ALK | 66.9615% | Fixed design and dispatch |
| Coal carbon-price parity | 150.0396 CNY/t CO2 | Coal reference 2350 CNY/t NH3; all 4.6 t CO2/t NH3 assumed charged |

These arithmetic checks are not fresh optimisation results.

## Remaining limits

- No complete eight-route integer optimisation rerun, all-design sensitivity reoptimisation, or independent global-optimality proof was performed.
- No fresh Aspen simulation/convergence test, experimental validation, or independent weather-data retrieval was performed.
- Parameter provenance, distribution rights and the appropriate software/data licence still require author confirmation.
- Actual portability tests used Windows and Python 3.13.2, not every supported platform or minimum package version.
- This report records the model audit. Remote-repository privacy and anonymous-access checks are separate from scientific validation.
