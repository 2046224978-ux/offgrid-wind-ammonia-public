"""Validate package integrity and retained inputs; does not solve the model."""
import ast
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    manifest = json.loads((ROOT/'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    for relative, expected in manifest.items():
        path = ROOT/relative
        require(path.is_file(), 'Missing file: '+relative)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        require(actual == expected, 'Checksum differs from packaged source: '+relative)
    scripts = list((ROOT/'Model').rglob('*.py'))
    for path in scripts:
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=path.name)
    for path in (ROOT/'Model/Case_Inputs').glob('*.json'):
        json.loads(path.read_text(encoding='utf-8-sig'),
                   parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON: '+value)))
    workbook = pd.ExcelFile(ROOT/'Model/Resource_Inputs.xlsx')
    expected = {**{'Wind_'+str(y): 8784 if y == 2024 else 8760 for y in range(2021,2026)},
                **{s+'_2025': 8760 for s in ['Keshiketeng','Siziwang','Ngari','Pingtan']}}
    require(set(workbook.sheet_names) == set(expected), 'Unexpected resource sheets')
    for sheet, count in expected.items():
        frame = pd.read_excel(workbook, sheet_name=sheet)
        require(len(frame) == count, 'Wrong annual row count: '+sheet)
        require(not frame.isna().any().any(), 'Missing values: '+sheet)
        numeric = frame.select_dtypes(include='number')
        require(np.isfinite(numeric.to_numpy()).all(), 'Non-finite input: '+sheet)
        if 'hour' in frame:
            hours = frame['hour'].to_numpy()
            require(len(np.unique(hours)) == count and np.all(np.diff(hours) == 1), 'Non-consecutive hour index: '+sheet)
        for col in ['wind_generation_kW_per_kW','wind_per_kw','pv_per_kw']:
            if col in frame:
                require(frame[col].between(0,1).all(), 'Capacity-factor range: '+sheet+'/'+col)
        for col in frame:
            if 'timestamp' in col.lower():
                stamps = pd.to_datetime(frame[col])
                require(stamps.is_unique, 'Duplicate timestamps: '+sheet)
                require((stamps.diff().dropna() == pd.Timedelta(hours=1)).all(), 'Non-hourly timestamps: '+sheet)
    ledgers = json.loads((ROOT/'Model/Case_Inputs/Baseline_Ledgers.json').read_text(encoding='utf-8-sig'))
    routes = {sid+'+'+el for sid in ['Ru','Fe'] for el in ['ALK','AEM','PEM','SOEC']}
    require(set(ledgers) == routes, 'Unexpected baseline routes')
    for route, ledger in ledgers.items():
        product = ledger['annual_liquid_NH3_component_t']
        require(np.isfinite(product) and product > 0, 'Invalid recovered product: '+route)
        total = ledger['residual_annual_CNY']+sum(ledger['annual_response_CNY'].values())
        np.testing.assert_allclose(total/product, ledger['baseline_LCOA_CNY_per_t'], rtol=1e-10)
    sampled = pd.read_csv(ROOT/'Plotting/Data/Fig08_LAEC_Sampled_Results.csv')
    require(set(sampled.route) == routes, 'Missing LAEC sampling routes')
    require(sampled.utilization_target_percent.between(1,100).all(), 'Invalid LAEC utilisation target')
    for name in ['Carbon_Price_Paths.csv','Cost_Path_Anchors.csv']:
        data = pd.read_csv(ROOT/'Model/Case_Inputs'/name)
        require(len(data) > 0 and not data.isna().any().any(), 'Missing scenario inputs: '+name)
    print('PASS: %d source hashes, %d Python syntax checks, %d hourly resource sheets, eight baseline ledgers, and case input files.'
          % (len(manifest), len(scripts), len(expected)))
    print('Scope: integrity and input arithmetic only; no optimisation or global optimality claim.')


if __name__ == '__main__':
    main()
