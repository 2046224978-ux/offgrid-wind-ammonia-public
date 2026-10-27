"""Eight-route off-grid wind-to-ammonia capacity and hourly-dispatch model.

Python 3.11+, numpy, pandas, scipy, highspy and openpyxl are required. The exact hourly
commitment formulation uses Gurobi; set GUROBI_CL to its licensed executable.
All process, technology, economic and land parameters are defined in this file.
Hourly resource profiles are read from Resource_Inputs.xlsx. Fixed-design cost
analyses also use the retained ledgers and external price paths in Case_Inputs.

Examples:
    python Ammonia_MILP.py --check
    python Ammonia_MILP.py --route Ru+ALK --time-limit 3600
    python Ammonia_MILP.py --route all --time-limit 3600

The published plotting tables are retained independently in Plotting/Data.
Separate cases are in Model/Cases. New calculations are written to
Results/New_<case> and never overwrite the published tables.
"""


from __future__ import annotations
import argparse
import concurrent.futures
import copy
import gc
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Final
import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix, csr_matrix, hstack, vstack, eye



# Land boundary


land_CONTRACT_ID: Final = 'siziwang-land-only-market-5d3d-20260825'

land_SITE_ENVELOPE_HA: Final = 45300.0

land_ROTOR_DIAMETER_M: Final = 221.0

land_ALONGWIND_SPACING_D: Final = 5.0

land_CROSSWIND_SPACING_D: Final = 3.0

land_WIND_TURBINE_MW: Final = 10.0

land_WIND_PERMANENT_HA_PER_MW: Final = 1038.0 / 15.0 / 1000.0

land_RENEWABLE_LAND_LEASE_CNY_PER_MU_YEAR: Final = 400.0

land_PROCESS_REFERENCE_AREA_HA: Final = 297012.12 / 10000.0

land_PROCESS_REFERENCE_NAMEPLATE_KT_PER_Y: Final = 180.0

land_PROCESS_LAND_PRICE_CNY_PER_M2: Final = 9400000.0 / 63000.0

land_PV_CAPACITY_MW: Final = 0.0

def land_process_site_ha(synthesis_nameplate_kt_per_y: float) -> float:
    nameplate = float(synthesis_nameplate_kt_per_y)
    if nameplate <= 0.0:
        raise ValueError('synthesis nameplate must be positive')
    return land_PROCESS_REFERENCE_AREA_HA * nameplate / land_PROCESS_REFERENCE_NAMEPLATE_KT_PER_Y

def land_wind_spacing_control_ha_per_turbine() -> float:
    return land_ALONGWIND_SPACING_D * land_CROSSWIND_SPACING_D * land_ROTOR_DIAMETER_M ** 2 / 10000.0

def land_annual_wind_land_cost_cny_per_kw() -> float:
    return land_WIND_PERMANENT_HA_PER_MW * 15.0 * land_RENEWABLE_LAND_LEASE_CNY_PER_MU_YEAR / 1000.0

def land_process_land_capex_cny(synthesis_nameplate_kt_per_y: float) -> float:
    return land_process_site_ha(synthesis_nameplate_kt_per_y) * 10000.0 * land_PROCESS_LAND_PRICE_CNY_PER_M2

def land__unsigned_contract_payload() -> dict:
    return {'contract_id': land_CONTRACT_ID, 'renewable_scope': 'wind_only', 'pv_capacity_MW': land_PV_CAPACITY_MW, 'site_envelope_ha': land_SITE_ENVELOPE_HA, 'rotor_diameter_m': land_ROTOR_DIAMETER_M, 'wind_turbine_MW': land_WIND_TURBINE_MW, 'wind_spacing_rule': '5D_by_3D', 'alongwind_spacing_D': land_ALONGWIND_SPACING_D, 'crosswind_spacing_D': land_CROSSWIND_SPACING_D, 'wind_spacing_control_ha_per_turbine': land_wind_spacing_control_ha_per_turbine(), 'wind_permanent_ha_per_MW': land_WIND_PERMANENT_HA_PER_MW, 'renewable_land_lease_CNY_per_mu_year': land_RENEWABLE_LAND_LEASE_CNY_PER_MU_YEAR, 'process_reference_area_ha': land_PROCESS_REFERENCE_AREA_HA, 'process_reference_nameplate_kt_per_y': land_PROCESS_REFERENCE_NAMEPLATE_KT_PER_Y, 'process_land_price_CNY_per_m2': land_PROCESS_LAND_PRICE_CNY_PER_M2, 'roads_included': False, 'wind_equipment_and_land_cost_separated': True, 'process_equipment_and_land_cost_separated': True, 'sources': {'site_and_permanent_land': 'https://nm.cnr.cn/xinwensudi/20231225/t20231225_526533567.shtml', 'wind_spacing': 'https://sthjj.xlgl.gov.cn/sthjj/hjgl/hpsp/xmslqk/f1879b0769d84920a77107d19f498556/P020220711386550127505.pdf', 'rotor_diameter': 'https://www.xam.gov.cn/xam/2025-07/08/8790693498d842029cce6b5c84fe825b/2025070817032070459.pdf', 'process_site': 'https://xxgk.jl.gov.cn/zcbm/fgw/xxgkmlqy/202308/t20230804_8775981.html'}}

def land_land_contract_payload() -> dict:
    payload = land__unsigned_contract_payload()
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return {**payload, 'land_contract_sha256': hashlib.sha256(encoded.encode('utf-8')).hexdigest()}

def land_build_land_audit(*, wind_capacity_MW: float, synthesis_nameplate_kt_per_y: float) -> dict:
    wind_mw = float(wind_capacity_MW)
    if wind_mw < 0.0:
        raise ValueError('wind capacity must be non-negative')
    turbine_count = wind_mw / land_WIND_TURBINE_MW
    wind_spacing_ha = turbine_count * land_wind_spacing_control_ha_per_turbine()
    plant_ha = land_process_site_ha(synthesis_nameplate_kt_per_y)
    accounted_ha = wind_spacing_ha + plant_ha
    remaining_ha = land_SITE_ENVELOPE_HA - accounted_ha
    wind_permanent_ha = wind_mw * land_WIND_PERMANENT_HA_PER_MW
    annual_wind_land = wind_permanent_ha * 15.0 * land_RENEWABLE_LAND_LEASE_CNY_PER_MU_YEAR
    process_land_capex = land_process_land_capex_cny(synthesis_nameplate_kt_per_y)
    return {'land_contract_sha256': land_land_contract_payload()['land_contract_sha256'], 'site_envelope_ha': land_SITE_ENVELOPE_HA, 'wind_turbine_count': turbine_count, 'wind_spacing_control_ha': wind_spacing_ha, 'wind_permanent_land_ha': wind_permanent_ha, 'process_site_ha': plant_ha, 'total_accounted_land_ha': accounted_ha, 'remaining_land_ha': remaining_ha, 'land_utilization_fraction': accounted_ha / land_SITE_ENVELOPE_HA, 'land_hard_constraint_passed': remaining_ha >= -1e-07, 'pv_capacity_MW': land_PV_CAPACITY_MW, 'annual_wind_land_cost_CNY': annual_wind_land, 'process_land_capex_CNY': process_land_capex, 'wind_equipment_and_land_cost_separated': True, 'process_equipment_and_land_cost_separated': True}

land_only = SimpleNamespace(ALONGWIND_SPACING_D=land_ALONGWIND_SPACING_D,
CONTRACT_ID=land_CONTRACT_ID,
CROSSWIND_SPACING_D=land_CROSSWIND_SPACING_D,
PROCESS_LAND_PRICE_CNY_PER_M2=land_PROCESS_LAND_PRICE_CNY_PER_M2,
PROCESS_REFERENCE_AREA_HA=land_PROCESS_REFERENCE_AREA_HA,
PROCESS_REFERENCE_NAMEPLATE_KT_PER_Y=land_PROCESS_REFERENCE_NAMEPLATE_KT_PER_Y,
PV_CAPACITY_MW=land_PV_CAPACITY_MW,
RENEWABLE_LAND_LEASE_CNY_PER_MU_YEAR=land_RENEWABLE_LAND_LEASE_CNY_PER_MU_YEAR,
ROTOR_DIAMETER_M=land_ROTOR_DIAMETER_M,
SITE_ENVELOPE_HA=land_SITE_ENVELOPE_HA,
WIND_PERMANENT_HA_PER_MW=land_WIND_PERMANENT_HA_PER_MW,
WIND_TURBINE_MW=land_WIND_TURBINE_MW,
_unsigned_contract_payload=land__unsigned_contract_payload,
annual_wind_land_cost_cny_per_kw=land_annual_wind_land_cost_cny_per_kw,
build_land_audit=land_build_land_audit,
land_contract_payload=land_land_contract_payload,
process_land_capex_cny=land_process_land_capex_cny,
process_site_ha=land_process_site_ha,
wind_spacing_control_ha_per_turbine=land_wind_spacing_control_ha_per_turbine)


# Current numerical parameters


MODEL_SOURCE_SHA256 = '9f8255ae57ce90f4703d8c84e45794627bef3a2eb7a806ec8212df5e68103570'

LAND_ONLY_ACTIVE = True

MODEL_CONFIGURATION_ID = 'wind-only-exact-battery-modular-bound-enumerated-plant-Fe900F-land-only-market-5d3d'

REQUIRE_ALK_BELOW_ENVELOPE_VALIDATION = True

SURROGATE_COUPLING_ENABLED = True

SURROGATE_COUPLING_MAX_ITER = 8

SURROGATE_COUPLING_TOL = 0.001

SURROGATE_COUPLING_DAMPING = 0.8

FE_SURROGATE_PWL_LOAD_KNOTS = [0.3, 0.375, 0.475, 0.6, 0.775, 0.875, 1.0]

SURROGATE_PWL_MAX_REL_ERROR = 0.003

N_HOURS = 8760

TARGET_YEAR = 2025

FIXED_WIND_KW = 1000000.0

TARGET_NH3_T_PER_Y = 300000.0

MIN_FIXED_WIND_NH3_T_PER_Y = 1000.0

VARIABLE_SCALE_MIN_WIND_KW = 500000.0

VARIABLE_SCALE_MAX_WIND_KW = 1500000.0

DISCOUNT_RATE = 0.08

PROJECT_LIFE_Y = 30

FULL_EQUIPMENT_REPLACEMENT_RATIO = 0.8

USD_TO_CNY_2025 = 7.1875

EUR_TO_CNY_2025 = 8.1185

BLS_CPI_ALL_URBAN_2019 = 255.657416666667

BLS_CPI_ALL_URBAN_2025 = 321.943

CATALYST_COST_2019_TO_2025 = 1.2592750259217331

WIND_CAPEX_CNY_PER_KW = 3600.0

WIND_FIXED_OM_RATIO = 0.03

WIND_LIFE_Y = 30

WIND_TURBINE_KW = 10000.0

ENGINEERING_FIXED_WIND_KW = 1000000.0

WIND_HUB_HEIGHT_M = 125.0

WIND_ROTOR_DIAMETER_M = 221.0

WIND_REFERENCE_HEIGHT_M = 50.0

WIND_ROUGHNESS_M = 0.03

WIND_CUT_IN_MPS = 2.5

WIND_RATED_MPS = 11.3

WIND_CUT_OUT_MPS = 24.0

WIND_OUTPUT_FACTOR = 0.95

AIR_DENSITY_REFERENCE_KG_M3 = 1.225

DRY_AIR_GAS_CONSTANT_J_KG_K = 287.0

BESS_CAPEX_CNY_PER_KWH = 935.4

BESS_FIXED_OM_RATIO = 0.025

BESS_LIFE_Y = 15

BESS_DURATION_H = 4.0

BESS_ROUNDTRIP_EFFICIENCY = 0.85

BESS_CHARGE_EFFICIENCY = 0.9219544457292888

BESS_DISCHARGE_EFFICIENCY = 0.9219544457292888

BESS_MIN_SOC = 0.05

BESS_MAX_SOC = 0.9

BESS_FLOW_TIEBREAKER_CNY_PER_KWH = 1e-06

ELECTROLYZER_TRANSITION_TIEBREAKER_CNY_PER_MODULE = 1e-06

SURROGATE_POWER_TIEBREAKER_CNY_PER_KWH = 1e-06

H2_KG_PER_NM3 = 0.08994378513429106

PSI_PER_BAR = 14.5037738

STANDARD_ATMOSPHERE_PSI = 14.6959488

COMMON_H2_DELIVERY_PRESSURE_BAR_A = 30.0

H2A_HYDROGEN_PRESSURE_BAR_A = 21.697521840832902

FE_ASPEN_FRESH_FEED_PRESSURE_BAR_A = 20.684271841029403

H2_TANK_CAPEX_CNY_PER_NM3 = 194.23549281698936

H2_TANK_FIXED_OM_RATIO = 0.02

H2_TANK_LIFE_Y = 20

H2_MIN_SOC = 0.05

H2_MAX_SOC = 0.95

ELECTROLYZER_LIFE_Y = 30

STACK_REPLACEMENT_FRACTION = 0.11

WATER_PRICE_CNY_PER_M3 = 4.81

US_GALLON_TO_M3 = 0.003785411784

DOE_LTE_WATER_GAL_PER_KG_H2 = 3.78

DOE_SOEC_WATER_GAL_PER_KG_H2 = 2.38

ALK_CAPEX_CNY_PER_KW = 2200.0

PEM_CAPEX_CNY_PER_KW = 6350.0

SOEC_CAPEX_CNY_PER_KW = 15000.0

PEM_SYSTEM_ENERGY_KWH_PER_KG_H2 = 55.2

SOEC_SYSTEM_ENERGY_KWH_PER_KG_H2 = 47.0

ALK_MODULE_POWER_KW = 10000.0

PEM_MODULE_POWER_KW = 5000.0

SOEC_MODULE_POWER_KW = 6000.0

ALK_TARGET_MIN_ELECTRICAL_LOAD = 0.25

PEM_MIN_ELECTRICAL_LOAD = 0.1

SOEC_MIN_ELECTRICAL_LOAD = 0.3

ALK_ELECTROLYZER_RAMP_PER_H = 1.0

PEM_ELECTROLYZER_RAMP_PER_H = 1.0

SOEC_ELECTROLYZER_RAMP_PER_H = 1.0

HOURLY_RAMP_EQUIVALENT_PERCENT_PER_MIN = 1.6666666666666667

ALK_REPORTED_RAMP_PERCENT_PER_MIN = 240.0

PEM_REPORTED_RAMP_PERCENT_PER_MIN = 600.0

SOEC_REPORTED_RAMP_PERCENT_PER_MIN = 6.0

SYNTHESIS_LIFE_Y = 30

SYNTHESIS_FIXED_OM_RATIO = 0.02

FE_SYNTHESIS_MIN_LOAD = 0.3

FE_SYNTHESIS_RAMP_PER_H = 0.3

FE_SYNTHESIS_RAMP_PERCENT_PER_MIN = 0.5

TOPSOE_DYNAMIC_MIN_LOAD = 0.1

TOPSOE_DYNAMIC_RAMP_PERCENT_PER_MIN = 3.0

TOPSOE_DYNAMIC_RAMP_PER_H = 1.0

RU_SYNTHESIS_MIN_LOAD = 0.1

RU_SYNTHESIS_RAMP_PER_H = 1.0

SYNTHESIS_LOOP_CAPEX_USD_PER_KG_NH3_PER_H = 3510.0

ASU_CAPEX_USD_PER_KG_N2_PER_H = 1697.0

SYNTHESIS_COST_REFERENCE_KT_PER_Y = 300.0

JANO_HB_BARE_COMPONENTS_MUSD = {'HB reactor': 19.0, 'HB boiler': 10.8, 'HB heat exchangers': 52.8, 'HB drums': 24.6, 'HB turbines': 44.5, 'HB compressors': 54.1, 'HB pumps': 2.4}

JANO_HB_TOTAL_BARE_MUSD = 208.2

SYNTHESIS_COMPONENT_SCALE_EXPONENTS = {'HB reactor': 0.52, 'HB boiler': 0.6, 'HB heat exchangers': 0.71, 'HB drums': 0.6, 'HB turbines': 0.6, 'HB compressors': 0.71, 'HB pumps': 0.6}

ASU_SCALE_EXPONENT = 0.6

RU_COMPONENT_FACTORS = {'HB reactor': 0.6433346441211167, 'HB heat exchangers': 1.19958071278826, 'HB compressors': 1.0129702684499182}

FE_CATALYST_CNY_PER_KG = 1.7772949797668054

RU_CATALYST_CNY_PER_KG = 3169.904335024084

CATALYST_LIFE_Y = 10

FE_CATALYST_T_PER_100_TPD = 11.81

RU75_CATALYST_T_PER_100_TPD = 3.816

COOLING_WATER_TEMPERATURE_C = 25.0

COMMON_NH3_PRODUCT_PRESSURE_BAR = 10.0

COMMON_NH3_PRODUCT_TEMPERATURE_C = 25.0

NH3_LIQUID_DENSITY_KG_PER_M3 = 602.1

NH3_PRODUCT_PUMP_EFFICIENCY = 0.75

MAX_WIND_KW = 5000000.0

MAX_ELECTROLYZER_KW = 3000000.0

MAX_BESS_KWH = 20000000.0

MAX_H2_TANK_NM3 = 100000000.0

PLANT_OPTIONS_KT_PER_Y = [100.0, 125.0, 150.0, 175.0, 200.0, 225.0, 250.0, 275.0, 300.0, 325.0, 350.0, 375.0, 400.0, 425.0, 450.0, 475.0, 500.0, 525.0, 550.0, 575.0, 600.0, 625.0, 650.0, 675.0, 700.0, 725.0, 750.0, 775.0, 800.0, 825.0, 850.0, 875.0, 900.0, 925.0, 950.0, 975.0, 1000.0]

SIZING_MIP_REL_GAP = 1e-05

MIP_REL_GAP = 0.02

MIP_TIME_LIMIT_S = 3600.0

DINKELBACH_TOL = 0.005

DINKELBACH_MAX_ITER = 15

PLANT_ENUMERATION_WORKERS = 3

FIXED_WIND_SCREENING_PLANT_KT_PER_Y = 300.0

ELECTROLYZER_FLEXIBILITY_ENABLED = True

TARGETED_TERMINAL_COUPLING_VERSION = 'v2-explicit-Ru-affine-and-rated-power'

REVIEWED_PARAMETER_VERSION = 'reviewed-parameters-20260915-aem-rev'

REVIEWED_PARAMETER_SHA256 = '40b8dfd9544783f99da644ba907c7a9931d7b2b022cc0c18bfcffd062e79a1d4'

REVIEWED_REOPTIMIZATION_COMPLETED = False


nan = float('nan')
THIS_FILE = Path(__file__).resolve()
RUN_DIR = THIS_FILE.parent / 'Results'
AUDIT_DIR = RUN_DIR / 'Solver_Logs'
INPUT_DIR = RUN_DIR
MODEL_SOURCE_SHA256 = hashlib.sha256(THIS_FILE.read_bytes()).hexdigest()
GUROBI_CL = Path(os.environ.get('GUROBI_CL') or shutil.which('gurobi_cl') or '__gurobi_not_found__')
PLANT_OPTIONS_KT_PER_Y = np.asarray(PLANT_OPTIONS_KT_PER_Y, dtype=float)
model = sys.modules[__name__]


PARAMETERS = {'constants': {'MODEL_SOURCE_SHA256': '9f8255ae57ce90f4703d8c84e45794627bef3a2eb7a806ec8212df5e68103570',
               'LAND_ONLY_ACTIVE': True,
               'MODEL_CONFIGURATION_ID': 'wind-only-exact-battery-modular-bound-enumerated-plant-Fe900F-land-only-market-5d3d',
               'REQUIRE_ALK_BELOW_ENVELOPE_VALIDATION': True,
               'SURROGATE_COUPLING_ENABLED': True,
               'SURROGATE_COUPLING_MAX_ITER': 8,
               'SURROGATE_COUPLING_TOL': 0.001,
               'SURROGATE_COUPLING_DAMPING': 0.8,
               'FE_SURROGATE_PWL_LOAD_KNOTS': [0.3, 0.375, 0.475, 0.6, 0.775, 0.875, 1.0],
               'SURROGATE_PWL_MAX_REL_ERROR': 0.003,
               'N_HOURS': 8760,
               'TARGET_YEAR': 2025,
               'FIXED_WIND_KW': 1000000.0,
               'TARGET_NH3_T_PER_Y': 300000.0,
               'MIN_FIXED_WIND_NH3_T_PER_Y': 1000.0,
               'VARIABLE_SCALE_MIN_WIND_KW': 500000.0,
               'VARIABLE_SCALE_MAX_WIND_KW': 1500000.0,
               'DISCOUNT_RATE': 0.08,
               'PROJECT_LIFE_Y': 30,
               'FULL_EQUIPMENT_REPLACEMENT_RATIO': 0.8,
               'USD_TO_CNY_2025': 7.1875,
               'EUR_TO_CNY_2025': 8.1185,
               'BLS_CPI_ALL_URBAN_2019': 255.657416666667,
               'BLS_CPI_ALL_URBAN_2025': 321.943,
               'CATALYST_COST_2019_TO_2025': 1.2592750259217331,
               'WIND_CAPEX_CNY_PER_KW': 3600.0,
               'WIND_FIXED_OM_RATIO': 0.03,
               'WIND_LIFE_Y': 30,
               'WIND_TURBINE_KW': 10000.0,
               'ENGINEERING_FIXED_WIND_KW': 1000000.0,
               'WIND_HUB_HEIGHT_M': 125.0,
               'WIND_ROTOR_DIAMETER_M': 221.0,
               'WIND_REFERENCE_HEIGHT_M': 50.0,
               'WIND_ROUGHNESS_M': 0.03,
               'WIND_CUT_IN_MPS': 2.5,
               'WIND_RATED_MPS': 11.3,
               'WIND_CUT_OUT_MPS': 24.0,
               'WIND_OUTPUT_FACTOR': 0.95,
               'AIR_DENSITY_REFERENCE_KG_M3': 1.225,
               'DRY_AIR_GAS_CONSTANT_J_KG_K': 287.0,
               'BESS_CAPEX_CNY_PER_KWH': 935.4,
               'BESS_FIXED_OM_RATIO': 0.025,
               'BESS_LIFE_Y': 15,
               'BESS_DURATION_H': 4.0,
               'BESS_ROUNDTRIP_EFFICIENCY': 0.85,
               'BESS_CHARGE_EFFICIENCY': 0.9219544457292888,
               'BESS_DISCHARGE_EFFICIENCY': 0.9219544457292888,
               'BESS_MIN_SOC': 0.05,
               'BESS_MAX_SOC': 0.9,
               'BESS_FLOW_TIEBREAKER_CNY_PER_KWH': 1e-06,
               'ELECTROLYZER_TRANSITION_TIEBREAKER_CNY_PER_MODULE': 1e-06,
               'SURROGATE_POWER_TIEBREAKER_CNY_PER_KWH': 1e-06,
               'H2_KG_PER_NM3': 0.08994378513429106,
               'PSI_PER_BAR': 14.5037738,
               'STANDARD_ATMOSPHERE_PSI': 14.6959488,
               'COMMON_H2_DELIVERY_PRESSURE_BAR_A': 30.0,
               'H2A_HYDROGEN_PRESSURE_BAR_A': 21.697521840832902,
               'FE_ASPEN_FRESH_FEED_PRESSURE_BAR_A': 20.684271841029403,
               'H2_TANK_CAPEX_CNY_PER_NM3': 194.23549281698936,
               'H2_TANK_FIXED_OM_RATIO': 0.02,
               'H2_TANK_LIFE_Y': 20,
               'H2_MIN_SOC': 0.05,
               'H2_MAX_SOC': 0.95,
               'ELECTROLYZER_LIFE_Y': 30,
               'STACK_REPLACEMENT_FRACTION': 0.11,
               'WATER_PRICE_CNY_PER_M3': 4.81,
               'US_GALLON_TO_M3': 0.003785411784,
               'DOE_LTE_WATER_GAL_PER_KG_H2': 3.78,
               'DOE_SOEC_WATER_GAL_PER_KG_H2': 2.38,
               'ALK_CAPEX_CNY_PER_KW': 2200.0,
               'PEM_CAPEX_CNY_PER_KW': 6350.0,
               'SOEC_CAPEX_CNY_PER_KW': 15000.0,
               'PEM_SYSTEM_ENERGY_KWH_PER_KG_H2': 55.2,
               'SOEC_SYSTEM_ENERGY_KWH_PER_KG_H2': 47.0,
               'ALK_MODULE_POWER_KW': 10000.0,
               'PEM_MODULE_POWER_KW': 5000.0,
               'SOEC_MODULE_POWER_KW': 6000.0,
               'ALK_TARGET_MIN_ELECTRICAL_LOAD': 0.25,
               'PEM_MIN_ELECTRICAL_LOAD': 0.1,
               'SOEC_MIN_ELECTRICAL_LOAD': 0.3,
               'ALK_ELECTROLYZER_RAMP_PER_H': 1.0,
               'PEM_ELECTROLYZER_RAMP_PER_H': 1.0,
               'SOEC_ELECTROLYZER_RAMP_PER_H': 1.0,
               'HOURLY_RAMP_EQUIVALENT_PERCENT_PER_MIN': 1.6666666666666667,
               'ALK_REPORTED_RAMP_PERCENT_PER_MIN': 240.0,
               'PEM_REPORTED_RAMP_PERCENT_PER_MIN': 600.0,
               'SOEC_REPORTED_RAMP_PERCENT_PER_MIN': 6.0,
               'SYNTHESIS_LIFE_Y': 30,
               'SYNTHESIS_FIXED_OM_RATIO': 0.02,
               'FE_SYNTHESIS_MIN_LOAD': 0.3,
               'FE_SYNTHESIS_RAMP_PER_H': 0.3,
               'FE_SYNTHESIS_RAMP_PERCENT_PER_MIN': 0.5,
               'TOPSOE_DYNAMIC_MIN_LOAD': 0.1,
               'TOPSOE_DYNAMIC_RAMP_PERCENT_PER_MIN': 3.0,
               'TOPSOE_DYNAMIC_RAMP_PER_H': 1.0,
               'RU_SYNTHESIS_MIN_LOAD': 0.1,
               'RU_SYNTHESIS_RAMP_PER_H': 1.0,
               'SYNTHESIS_LOOP_CAPEX_USD_PER_KG_NH3_PER_H': 3510.0,
               'ASU_CAPEX_USD_PER_KG_N2_PER_H': 1697.0,
               'SYNTHESIS_COST_REFERENCE_KT_PER_Y': 300.0,
               'JANO_HB_BARE_COMPONENTS_MUSD': {'HB reactor': 19.0,
                                                'HB boiler': 10.8,
                                                'HB heat exchangers': 52.8,
                                                'HB drums': 24.6,
                                                'HB turbines': 44.5,
                                                'HB compressors': 54.1,
                                                'HB pumps': 2.4},
               'JANO_HB_TOTAL_BARE_MUSD': 208.2,
               'SYNTHESIS_COMPONENT_SCALE_EXPONENTS': {'HB reactor': 0.52,
                                                       'HB boiler': 0.6,
                                                       'HB heat exchangers': 0.71,
                                                       'HB drums': 0.6,
                                                       'HB turbines': 0.6,
                                                       'HB compressors': 0.71,
                                                       'HB pumps': 0.6},
               'ASU_SCALE_EXPONENT': 0.6,
               'RU_COMPONENT_FACTORS': {'HB reactor': 0.6433346441211167,
                                        'HB heat exchangers': 1.19958071278826,
                                        'HB compressors': 1.0129702684499182},
               'FE_CATALYST_CNY_PER_KG': 1.7772949797668054,
               'RU_CATALYST_CNY_PER_KG': 3169.904335024084,
               'CATALYST_LIFE_Y': 10,
               'FE_CATALYST_T_PER_100_TPD': 11.81,
               'RU75_CATALYST_T_PER_100_TPD': 3.816,
               'COOLING_WATER_TEMPERATURE_C': 25.0,
               'COMMON_NH3_PRODUCT_PRESSURE_BAR': 10.0,
               'COMMON_NH3_PRODUCT_TEMPERATURE_C': 25.0,
               'NH3_LIQUID_DENSITY_KG_PER_M3': 602.1,
               'NH3_PRODUCT_PUMP_EFFICIENCY': 0.75,
               'MAX_WIND_KW': 5000000.0,
               'MAX_ELECTROLYZER_KW': 3000000.0,
               'MAX_BESS_KWH': 20000000.0,
               'MAX_H2_TANK_NM3': 100000000.0,
               'PLANT_OPTIONS_KT_PER_Y': [100.0,
                                          125.0,
                                          150.0,
                                          175.0,
                                          200.0,
                                          225.0,
                                          250.0,
                                          275.0,
                                          300.0,
                                          325.0,
                                          350.0,
                                          375.0,
                                          400.0,
                                          425.0,
                                          450.0,
                                          475.0,
                                          500.0,
                                          525.0,
                                          550.0,
                                          575.0,
                                          600.0,
                                          625.0,
                                          650.0,
                                          675.0,
                                          700.0,
                                          725.0,
                                          750.0,
                                          775.0,
                                          800.0,
                                          825.0,
                                          850.0,
                                          875.0,
                                          900.0,
                                          925.0,
                                          950.0,
                                          975.0,
                                          1000.0],
               'SIZING_MIP_REL_GAP': 1e-05,
               'MIP_REL_GAP': 0.02,
               'MIP_TIME_LIMIT_S': 3600.0,
               'DINKELBACH_TOL': 0.005,
               'DINKELBACH_MAX_ITER': 15,
               'PLANT_ENUMERATION_WORKERS': 3,
               'FIXED_WIND_SCREENING_PLANT_KT_PER_Y': 300.0,
               'ELECTROLYZER_FLEXIBILITY_ENABLED': True,
               'TARGETED_TERMINAL_COUPLING_VERSION': 'v2-explicit-Ru-affine-and-rated-power',
               'REVIEWED_PARAMETER_VERSION': 'reviewed-parameters-20260915-aem-rev',
               'REVIEWED_PARAMETER_SHA256': '40b8dfd9544783f99da644ba907c7a9931d7b2b022cc0c18bfcffd062e79a1d4',
               'REVIEWED_REOPTIMIZATION_COMPLETED': False},
 'electrolyzers': {'ALK': {'tech_id': 'ALK',
                           'tech_name': 'Pressurized alkaline electrolyzer (Aspen rated anchor)',
                           'unit_power_kW': 10000.0,
                           'base_process_power_kW': 9890.475561550991,
                           'delivery_compression_kW': 109.52443844900883,
                           'base_surrogate_rated_power_kW': 894.6386073252098,
                           'surrogate_module_scale': 11.177697807942803,
                           'h2_prod_kg_per_h': 160.42374968491453,
                           'h2_prod_Nm3_per_h': 1783.6001614274178,
                           'system_energy_kWh_per_kgH2': 62.3349100095268,
                           'water_m3_per_h': 1.5201669018802213,
                           'installed_capex_CNY_per_kW': 2200.0,
                           'fixed_om_ratio': 0.025,
                           'stack_life_h': 60000.0,
                           'unit_min_load_ratio': 0.25,
                           'target_min_power_ratio': 0.25,
                           'minimum_load_basis': 'rated total electrical input per online module',
                           'minimum_load_curve_basis': 'retained Aspen rated anchor and existing part-load '
                                                       'function, restricted to 25-100%',
                           'effective_min_power_ratio': 0.25,
                           'aspen_min_feasible_power_ratio': 0.3684555092918948,
                           'target_min_power_represented_in_validated_envelope': False,
                           'target_min_power_represented_in_active_curve': True,
                           'target_min_power_feasible_in_aspen': None,
                           'target_min_power_feasibility_status': '25_percent_model_floor_not_a_new_independent_Aspen_point',
                           'cold_start_min': '30-120',
                           'cold_start_hours_model': 2,
                           'ramp_up_ratio_per_h': 1.0,
                           'ramp_down_ratio_per_h': 1.0,
                           'reported_ramp_percent_per_min': 240.0,
                           'reported_ramp_note': 'World Bank/ESMAP reports 4% nameplate/s, equivalent to '
                                                 '240%/min; this only establishes that the hourly endpoint '
                                                 'cap is nonbinding.',
                           'aggregate_dispatch_note': '10 MW modules; each online module consumes 2.5-10 MW. '
                                                      'Offline modules consume zero in the retained model. '
                                                      'The plant aggregate may be below 25% of installed '
                                                      'power. The original raw Aspen minimum is retained '
                                                      'unchanged.',
                           'energy_scope_note': 'Retained Aspen rated electricity plus 6.7-to-30 bara '
                                                'delivery compression; existing part-load relation used only '
                                                'at 25-100% per online module.',
                           'water_scope_note': 'Rated fresh-water use is anchored to the Aspen process stream; '
                                               'part-load water use follows the literature-corrected '
                                               'hydrogen-output fraction.',
                           'source_id': 'ASPEN_ALK; VARELA2021; RMI2025; LIN2023; WB2026; HAEOULUS2022; '
                                        'DOE2024',
                           'system_life_y': 30.0,
                           'parameter_profile': 'reviewed-parameters-20260915-aem-rev',
                           'capital_source_note': 'RMI 2025 Table 8 domestic system-cost midpoint scenario; '
                                                  'installation treated as included in the adopted model '
                                                  'input; no second installation multiplier; not a matched '
                                                  'complete-EPC quote',
                           'fixed_om_source_note': 'DOE water electrolysis assessment Tables A-1 to A-3: 5% '
                                                   'of installed capital',
                           'model_floor_above_raw_aspen_minimum': False},
                   'PEM': {'tech_id': 'PEM',
                           'tech_name': 'PEM electrolyzer',
                           'unit_power_kW': 5000.0,
                           'base_process_power_kW': 4986.669993298549,
                           'delivery_compression_kW': 13.330006701450975,
                           'h2_prod_kg_per_h': 90.33822451627806,
                           'h2_prod_Nm3_per_h': 1004.3853989622304,
                           'system_energy_kWh_per_kgH2': 55.347556660237984,
                           'water_m3_per_h': 1.2926366949997241,
                           'installed_capex_CNY_per_kW': 6350.0,
                           'fixed_om_ratio': 0.0425,
                           'stack_life_h': 40000.0,
                           'unit_min_load_ratio': 0.1,
                           'target_min_power_ratio': 0.1,
                           'minimum_load_basis': 'rated total electrical input per online module',
                           'minimum_load_curve_basis': 'constant-efficiency literature benchmark',
                           'effective_min_power_ratio': 0.1,
                           'aspen_min_feasible_power_ratio': None,
                           'target_min_power_represented_in_validated_envelope': None,
                           'target_min_power_represented_in_active_curve': None,
                           'target_min_power_feasible_in_aspen': None,
                           'target_min_power_feasibility_status': 'not_applicable_constant_efficiency_benchmark',
                           'cold_start_min': '5-30',
                           'cold_start_hours_model': 1,
                           'ramp_up_ratio_per_h': 1.0,
                           'ramp_down_ratio_per_h': 1.0,
                           'reported_ramp_percent_per_min': 600.0,
                           'reported_ramp_note': 'HAEOULUS reports reaching rated power in less than 10 s; '
                                                 '600%/min is the conservative lower-bound equivalent.',
                           'aggregate_dispatch_note': 'Installed capacity uses 5 MW modules. Each online PEM '
                                                      'module operates at 10-100% of rated total electrical '
                                                      'input; the plant aggregate may be below 10% when only '
                                                      'a subset is online.',
                           'energy_scope_note': 'DOE current-system beginning-of-life electricity use at the '
                                                '300-psig H2A gate plus isothermal compression from 21.70 to '
                                                '30 bar(a).',
                           'water_scope_note': 'DOE Table A-1 water/steam use of 3.78 US gal/kgH2.',
                           'source_id': 'RMI2025; LIN2023; HAEOULUS2022; DOE2024; H2A_PRESSURE',
                           'system_life_y': 30.0,
                           'parameter_profile': 'reviewed-parameters-20260915-aem-rev',
                           'capital_source_note': 'RMI 2025 Table 8 domestic system-cost midpoint scenario; '
                                                  'installation treated as included in the adopted model '
                                                  'input; no second installation multiplier; not a matched '
                                                  'complete-EPC quote',
                           'fixed_om_source_note': 'DOE water electrolysis assessment Tables A-1 to A-3: 5% '
                                                   'of installed capital'},
                   'SOEC': {'tech_id': 'SOEC',
                            'tech_name': 'Solid oxide electrolyzer (current-system benchmark)',
                            'unit_power_kW': 6000.0,
                            'base_process_power_kW': 5981.221933348361,
                            'delivery_compression_kW': 18.778066651639357,
                            'h2_prod_kg_per_h': 127.2600411350715,
                            'h2_prod_Nm3_per_h': 1414.8842073420103,
                            'system_energy_kWh_per_kgH2': 47.14755666023798,
                            'water_m3_per_h': 1.1465213492411581,
                            'installed_capex_CNY_per_kW': 15000.0,
                            'fixed_om_ratio': 0.05,
                            'stack_life_h': 20000.0,
                            'unit_min_load_ratio': 0.3,
                            'target_min_power_ratio': 0.3,
                            'minimum_load_basis': 'rated total electrical input per online module',
                            'minimum_load_curve_basis': 'constant-efficiency literature benchmark',
                            'effective_min_power_ratio': 0.3,
                            'aspen_min_feasible_power_ratio': None,
                            'target_min_power_represented_in_validated_envelope': None,
                            'target_min_power_represented_in_active_curve': None,
                            'target_min_power_feasible_in_aspen': None,
                            'target_min_power_feasibility_status': 'not_applicable_constant_efficiency_benchmark',
                            'cold_start_min': '>360',
                            'cold_start_hours_model': 10,
                            'ramp_up_ratio_per_h': 1.0,
                            'ramp_down_ratio_per_h': 1.0,
                            'reported_ramp_percent_per_min': 6.0,
                            'reported_ramp_note': 'Liao et al. report 30% nameplate per 5 min, equivalent to '
                                                  '6%/min.',
                            'aggregate_dispatch_note': 'Installed capacity uses 6 MW modules. Each online '
                                                       'SOEC module remains at or above 30% of rated total '
                                                       'electrical input; clustered restart eligibility '
                                                       'requires a 10 h minimum-down interval. SOEC remains '
                                                       'a lower-TRL screening route.',
                            'energy_scope_note': 'DOE 47 kWh/kgH2 includes electrical and thermal inputs at '
                                                 'the H2A 300-psig plant-gate boundary; both are charged to '
                                                 'wind-electricity-equivalent supply here, plus 21.70-to-30 '
                                                 'bar(a) delivery compression.',
                            'water_scope_note': 'DOE Table A-3 water/steam use of 2.38 US gal/kgH2.',
                            'source_id': 'RMI2025; HAEOULUS2022; SOEC_DISPATCH2026; DOE2024; H2A_PRESSURE',
                            'system_life_y': 30.0,
                            'parameter_profile': 'reviewed-parameters-20260915-aem-rev',
                            'capital_source_note': 'RMI 2025 Table 8 domestic system-cost midpoint scenario; '
                                                   'installation treated as included in the adopted model '
                                                   'input; no second installation multiplier; not a matched '
                                                   'complete-EPC quote',
                            'fixed_om_source_note': 'DOE water electrolysis assessment Tables A-1 to A-3: 5% '
                                                    'of installed capital'},
                   'AEM': {'tech_id': 'AEM',
                           'tech_name': 'Anion-exchange-membrane electrolyzer (dilute KOH)',
                           'electrolyte': 'KOH',
                           'electrolyte_molarity_mol_l': 0.1,
                           'stack_temperature_C': 55.0,
                           'unit_power_kW': 1000.0,
                           'base_process_power_kW': 1000.0,
                           'delivery_compression_kW': 0.0,
                           'h2_prod_kg_per_h': 18.45018450184502,
                           'h2_prod_Nm3_per_h': 205.13017630176304,
                           'system_energy_kWh_per_kgH2': 54.2,
                           'water_m3_per_h': 0.163,
                           'installed_capex_CNY_per_kW': 4000.0,
                           'fixed_om_ratio': 0.05,
                           'stack_life_h': 20000.0,
                           'system_life_y': 20.0,
                           'unit_min_load_ratio': 0.085,
                           'target_min_power_ratio': 0.085,
                           'minimum_load_basis': 'rated total electrical input per online module',
                           'minimum_load_curve_basis': 'commercial AEM Nexus operating envelope',
                           'effective_min_power_ratio': 0.085,
                           'aspen_min_feasible_power_ratio': None,
                           'target_min_power_represented_in_validated_envelope': False,
                           'target_min_power_represented_in_active_curve': True,
                           'target_min_power_feasible_in_aspen': None,
                           'target_min_power_feasibility_status': 'manufacturer_envelope_transferred_to_dry_system_scenario',
                           'cold_start_min': None,
                           'cold_start_hours_model': 1,
                           'ramp_up_ratio_per_h': 1.0,
                           'ramp_down_ratio_per_h': 1.0,
                           'reported_ramp_percent_per_min': 26.667,
                           'reported_ramp_note': 'Nexus 1000 hot start reaches full load in 225 s.',
                           'product_pressure_bar': 30.0,
                           'aggregate_dispatch_note': '1 MW dry-system modules, 8.5-100% electrical-load '
                                                      'scenario. Dryer part-load behavior and cold-start '
                                                      'energy are not independently modeled.',
                           'energy_scope_note': 'Wienken 2026 full-system electricity 54.2 kWh/kgH2 (system '
                                                'LHV efficiency 61.5%), 1 MW system aggregation at a 30 bara '
                                                'model gate; no extra Aspen pump or delivery-compressor duty '
                                                'is added. Rated performance is transferred to a '
                                                'constant-efficiency dispatch model, not independently rerun '
                                                'in Aspen.',
                           'water_scope_note': 'Manufacturer rev08 purified-water consumption, scaled with '
                                               'the same dry-system rating as H2',
                           'source_id': 'WIENKEN2026_SYSTEM_LHV_61P5; RMI2025; HENKEL2024; '
                                        'JIN2024_EXPECTED_LIFETIME',
                           'capital_source_note': 'RMI 2025 Table 8 domestic system-cost midpoint scenario; '
                                                  'installation treated as included in the adopted model '
                                                  'input; no second installation multiplier; not a matched '
                                                  'complete-EPC quote',
                           'parameter_profile': 'reviewed-parameters-20260915-aem-rev',
                           'fixed_om_source_note': 'Henkel 2024 Table 4; separate stack replacement is a '
                                                   'study assumption',
                           'legacy_aspen_evidence': {'aspen_archive_sha256': '425a9e4027ed854ccfc6b302a339ad52b0b0b13d1dc9fc11147f98e4dbb4daba',
                                                     'aem_source_sha256': '9d8a88c88b749a64de777c901b763d48860a305e195af8fa4294c61c53ecc312'},
                           'product_pressure_basis': 'absolute',
                           'dry_hydrogen_molar_purity': 0.99999,
                           'cold_start_basis': 'retained one-hour minimum downtime scenario, not inferred '
                                               'from hot-start time',
                           'hot_start_seconds_reported': 225,
                           'active_energy_water_validated_by_legacy_aspen': False}},
 'syntheses': {'Fe': {'syn_id': 'Fe',
                      'loop_name': 'Fe catalyst, one isothermal RPlug loop',
                      'rated_NH3_kg_per_h': 31.9981153,
                      'h2_consumption_Nm3_per_h': 67.2379975,
                      'fresh_N2_kg_per_h': 28.01348,
                      'rated_power_kW': 23.49735817727419,
                      'direct_aspen_power_kW': 9.81144060379,
                      'corrected_aspen_power_kW': 8.445967958167145,
                      'aspen_common_gate_duplicate_power_removed_kW': 1.3654726456228556,
                      'fresh_feed_compression_kW': 11.55164246963713,
                      'refrigeration_electric_kW': 8.167235545389406,
                      'refrigeration_COP': 3.6285999999999996,
                      'refrigeration_evaporation_temperature_C': -10.0,
                      'cooling_water_temperature_C': 25.0,
                      'external_heat_electric_equivalent_kW': 0.0,
                      'asu_electric_kW': 3.0814828000000003,
                      'specific_power_kWh_per_kgNH3': 0.734335693117344,
                      'reaction_pressure_bar': 185.9,
                      'reaction_temperature_C': 482.22222222222223,
                      'reaction_temperature_F': 900.0,
                      'configuration': '1 x isothermal RPlug',
                      'native_product_state': 'NH3-PROD: 8.96 bar, -8.996 C, liquid',
                      'product_delivery_state': 'Immediate recovered liquid at -35 C and 1.07325 bar(a); '
                                                'cost denominator is NH3 component mass, not total liquid '
                                                'mixture',
                      'product_delivery_phase_validated': False,
                      'product_conditioning_electric_kWh_per_kgNH3': 0.021626532749497704,
                      'product_conditioning_note': 'Targeted recovery/refrigeration/cooling package; '
                                                   'replaces the old product-gate allowance exactly once',
                      'energy_boundary_note': 'Hydrogen enters at the common 30 bar(a) gate. Nitrogen is '
                                              'compressed from ambient to 30 bar(a); the Aspen mixed-feed '
                                              '20.68-to-30 bar segment is removed before retaining the '
                                              '30-to-185.9 bar synthesis compression. ASU, refrigeration, '
                                              'net electric heat equivalent and common-gate liquid pumping '
                                              'are then added.',
                      'min_load_ratio': 0.3,
                      'ramp_ratio_per_h': 0.3,
                      'reported_ramp_percent_per_min': 0.5,
                      'flexibility_basis': 'Wu et al. system-level envelope: 30% minimum load and 30% '
                                           'nameplate/h ramp.',
                      'catalyst_CNY_per_kg': 1.7772949797668054,
                      'catalyst_t_per_100tpd': 11.81,
                      'source_id': 'ASPEN_FE_900F; PETROCE_AMMONIA_WORKFLOW; CSF_AMMONIA_REVIEW; '
                                   'REFRIG_COP2023; WU2024; JANO2024; YOSHIDA2021',
                      'targeted_terminal': {'route': 'Fe',
                                            'scenario': 'reference',
                                            'synthesis_nameplate_kt_y': 475.0,
                                            'nominal_gross_t_h': 54.22374429223744,
                                            'liquid_recovery_fraction': 0.9998801359998799,
                                            'screened_liquid_purity_wt_pct': 99.9351128,
                                            'selected_Aspen_load_fraction': 0.3,
                                            'compressor_kWh_per_t_gross': 19.615947786430496,
                                            'fan_kWh_per_t_gross': 0.6272051123985456,
                                            'cooling_pump_kWh_per_t_gross': 1.3833798506686616,
                                            'terminal_electricity_kWh_per_t_gross': 21.626532749497702,
                                            'installed_terminal_capital_CNY': 60463201.480717756,
                                            'direct_installed_CNY': 50386001.23393147,
                                            'engineering_contingency_fraction': 0.2,
                                            'terminal_fixed_OM_fraction': 0.03,
                                            'terminal_life_y': 30,
                                            'water_m3_per_t_gross': 0.09635669204777095,
                                            'wastewater_m3_per_t_gross': 0.018708487119443097,
                                            'wastewater_service_CNY_m3': 20.0,
                                            'chemical_CNY_per_t_gross': 0.06524398974784416,
                                            'model_water_price_CNY_m3': 4.81,
                                            'variable_cost_CNY_per_t_gross': 0.9028894208864842,
                                            'product_boundary': 'Immediate recovered liquid at -35 C and '
                                                                '1.07325 bar(a); cost denominator is NH3 '
                                                                'component mass, not total liquid mixture',
                                            'scope': 'Incremental recovery, refrigeration, exchange, '
                                                     'separation, cooling and installation/maintenance '
                                                     'scenario',
                                            'exclusions_not_zero': ['Final treatment of H2-rich purge gas '
                                                                    'and any new environmental compliance '
                                                                    'facilities',
                                                                    'Bulk ammonia storage, loading and '
                                                                    'transport beyond the immediate liquid '
                                                                    'outlet',
                                                                    'Unresolved overlap with the original '
                                                                    'lumped synthesis equipment allowance'],
                                            'source_transfer_assumptions': ['2025 model currency conversion '
                                                                            'from the v3 SSLW cost screen',
                                                                            'Engineering/contingency '
                                                                            '10/20/35%, annual terminal O&M '
                                                                            '3%, wastewater service 5/20/50 '
                                                                            'CNY/m3 are explicit study '
                                                                            'settings',
                                                                            'Fixed equipment rated at 475 '
                                                                            'kt/y nameplate; constant peak '
                                                                            'sampled utility coefficient in '
                                                                            'hourly dispatch',
                                                                            'No arbitrary subtraction of the '
                                                                            'old compressor/HX investment '
                                                                            'pools',
                                                                            'Gas velocity .2 m/s, 5-minute '
                                                                            'liquid hold-up, L/D=3, minimum '
                                                                            'D=1 m, allowable-stress screen '
                                                                            '115 MPa and weld factor .85 are '
                                                                            'engineering sizing assumptions',
                                                                            'Dedicated cooling loop 26-to-30 '
                                                                            'C; no site-specific mechanical '
                                                                            'certification or '
                                                                            'commercial-purity claim'],
                                            'evidence_level': 'C: newly derived engineering cost scenario '
                                                              'coupled to optimized operation'},
                      'replaced_old_terminal_kWh_per_t': 0.06397362318305677,
                      'upstream_rated_power_kW': 22.805349888816536},
               'Ru': {'syn_id': 'Ru',
                      'loop_name': 'Ru/C catalyst, three adiabatic RPlug beds',
                      'rated_NH3_kg_per_h': 33.3056072,
                      'h2_consumption_Nm3_per_h': 67.23797737631945,
                      'fresh_N2_kg_per_h': 28.0134716,
                      'rated_power_kW': 18.66513880656947,
                      'direct_aspen_power_kW': 1.24422545,
                      'fresh_feed_compression_kW': 6.524411287270542,
                      'refrigeration_electric_kW': 7.543489123566224,
                      'refrigeration_COP': 2.3329999999999997,
                      'refrigeration_evaporation_temperature_C': -30.0,
                      'cooling_water_temperature_C': 25.0,
                      'external_heat_electric_equivalent_kW': 0.0,
                      'asu_electric_kW': 3.081481876,
                      'specific_power_kWh_per_kgNH3': 0.5604203128465849,
                      'reaction_pressure_bar': 76.0,
                      'reaction_temperature_C': 'R101 365-472; R102 343-443; R103 354-433',
                      'configuration': '3 x adiabatic RPlug with inter-bed conditioning',
                      'native_product_state': 'NH3: 70 bar, -28 C, liquid',
                      'product_delivery_state': 'Immediate recovered liquid at -35 C and 1.07325 bar(a); '
                                                'cost denominator is NH3 component mass, not total liquid '
                                                'mixture',
                      'product_delivery_phase_validated': False,
                      'product_conditioning_electric_kWh_per_kgNH3': 0.00815996438950441,
                      'product_conditioning_note': 'Targeted recovery/refrigeration/cooling package; '
                                                   'replaces the old product-gate allowance exactly once',
                      'energy_boundary_note': 'Aspen recycle power plus external 30-to-76 bar fresh-gas '
                                              'compression, 1-to-30 bar N2 compression, ASU, mechanical '
                                              'refrigeration, and common product-gate conditioning.',
                      'min_load_ratio': 0.1,
                      'ramp_ratio_per_h': 1.0,
                      'reported_ramp_percent_per_min': 3.0,
                      'flexibility_basis': 'Topsoe route-level envelope: 10% minimum load and a 100%/h '
                                           'endpoint cap translated from the reported 3%/min dynamic-loop '
                                           'capability.',
                      'catalyst_CNY_per_kg': 3169.904335024084,
                      'catalyst_t_per_100tpd': 3.816,
                      'source_id': 'ASPEN_RU; REFRIG_COP2023; TOPSOE_DYNAMIC; JANO2024; YOSHIDA2021',
                      'targeted_terminal': {'route': 'Ru',
                                            'scenario': 'reference',
                                            'synthesis_nameplate_kt_y': 475.0,
                                            'nominal_gross_t_h': 54.22374429223744,
                                            'liquid_recovery_fraction': 0.999885816,
                                            'screened_liquid_purity_wt_pct': 99.9997194,
                                            'selected_Aspen_load_fraction': 1.0,
                                            'compressor_kWh_per_t_gross': 7.454030536430914,
                                            'fan_kWh_per_t_gross': 0.22021716554939713,
                                            'cooling_pump_kWh_per_t_gross': 0.4857166875240977,
                                            'terminal_electricity_kWh_per_t_gross': 8.15996438950441,
                                            'installed_terminal_capital_CNY': 33141115.52700142,
                                            'direct_installed_CNY': 27617596.27250118,
                                            'engineering_contingency_fraction': 0.2,
                                            'terminal_fixed_OM_fraction': 0.03,
                                            'terminal_life_y': 30,
                                            'water_m3_per_t_gross': 0.033831671944333244,
                                            'wastewater_m3_per_t_gross': 0.006568712409574929,
                                            'wastewater_service_CNY_m3': 20.0,
                                            'chemical_CNY_per_t_gross': 0.02290773178882252,
                                            'model_water_price_CNY_m3': 4.81,
                                            'variable_cost_CNY_per_t_gross': 0.317012322032564,
                                            'product_boundary': 'Immediate recovered liquid at -35 C and '
                                                                '1.07325 bar(a); cost denominator is NH3 '
                                                                'component mass, not total liquid mixture',
                                            'scope': 'Incremental recovery, refrigeration, exchange, '
                                                     'separation, cooling and installation/maintenance '
                                                     'scenario',
                                            'exclusions_not_zero': ['Final treatment of H2-rich purge gas '
                                                                    'and any new environmental compliance '
                                                                    'facilities',
                                                                    'Bulk ammonia storage, loading and '
                                                                    'transport beyond the immediate liquid '
                                                                    'outlet',
                                                                    'Unresolved overlap with the original '
                                                                    'lumped synthesis equipment allowance'],
                                            'source_transfer_assumptions': ['2025 model currency conversion '
                                                                            'from the v3 SSLW cost screen',
                                                                            'Engineering/contingency '
                                                                            '10/20/35%, annual terminal O&M '
                                                                            '3%, wastewater service 5/20/50 '
                                                                            'CNY/m3 are explicit study '
                                                                            'settings',
                                                                            'Fixed equipment rated at 475 '
                                                                            'kt/y nameplate; constant peak '
                                                                            'sampled utility coefficient in '
                                                                            'hourly dispatch',
                                                                            'No arbitrary subtraction of the '
                                                                            'old compressor/HX investment '
                                                                            'pools',
                                                                            'Gas velocity .2 m/s, 5-minute '
                                                                            'liquid hold-up, L/D=3, minimum '
                                                                            'D=1 m, allowable-stress screen '
                                                                            '115 MPa and weld factor .85 are '
                                                                            'engineering sizing assumptions',
                                                                            'Dedicated cooling loop 26-to-30 '
                                                                            'C; no site-specific mechanical '
                                                                            'certification or '
                                                                            'commercial-purity claim'],
                                            'evidence_level': 'C: newly derived engineering cost scenario '
                                                              'coupled to optimized operation'},
                      'replaced_old_terminal_kWh_per_t': 0.0,
                      'upstream_rated_power_kW': 18.39336623784665}},
 'curves': {'ALK': {'model_id': 'ALK',
                    'source_path': 'ALK_milp_breakpoints.csv',
                    'load_fraction': [0.25, 1.0],
                    'total_power_kW': [223.65965183130245, 894.6386073252098],
                    'power_fraction': [0.25, 1.0],
                    'rated_power_kW': 894.6386073252098,
                    'h2_Nm3_per_h': [44.64964196567046, 159.56775644444446],
                    'rated_h2_Nm3_per_h': 159.56775644444446,
                    'water_m3_per_h': [0.03805500210467238, 0.136],
                    'rated_water_m3_per_h': 0.136,
                    'raw_aspen_min_power_fraction': 0.3684555092918948,
                    'minimum_point_basis': 'Aspen full-load anchor plus Varela et al. Eq. (5) normalized '
                                           'part-load shape',
                    'active_curve_source_path': 'alk_25pct_literature_correction.json',
                    'target_hydrogen_output_fraction': 0.2798161919461204,
                    'target_point_is_direct_aspen': False},
            'Ru': {'model_id': 'Ru',
                   'source_path': 'Ru_milp_breakpoints.csv',
                   'load_fraction': [0.1,
                                     0.125,
                                     0.15,
                                     0.175,
                                     0.2,
                                     0.225,
                                     0.25,
                                     0.275,
                                     0.3,
                                     0.325,
                                     0.35,
                                     0.375,
                                     0.4,
                                     0.425,
                                     0.45,
                                     0.475,
                                     0.5,
                                     0.525,
                                     0.55,
                                     0.575,
                                     0.6,
                                     0.625,
                                     0.65,
                                     0.675,
                                     0.7,
                                     0.725,
                                     0.75,
                                     0.775,
                                     0.8,
                                     0.825,
                                     0.85,
                                     0.875,
                                     0.9,
                                     0.925,
                                     0.95,
                                     0.975,
                                     1.0],
                   'total_power_kW': [1.8393962632540175,
                                      2.2991846682742407,
                                      2.7590079070006936,
                                      3.2188232056740387,
                                      3.6787421159209224,
                                      4.13852646719081,
                                      4.598456427732593,
                                      5.0583332721036385,
                                      5.517953525697714,
                                      5.977755296278789,
                                      6.437835790520817,
                                      6.89771662974872,
                                      7.357571267481813,
                                      7.817154263697687,
                                      8.277431719142738,
                                      8.736964106555355,
                                      9.196908761648071,
                                      9.656772201478175,
                                      10.116582144351002,
                                      10.576035647307982,
                                      11.036403803843045,
                                      11.496393567411875,
                                      11.9557597522667,
                                      12.415712798416136,
                                      12.874823122363116,
                                      13.33506232934953,
                                      13.795124210054903,
                                      14.254938047928587,
                                      14.714479129502799,
                                      15.17523957689084,
                                      15.635205869408473,
                                      16.09421873336936,
                                      16.5542639078587,
                                      17.013992233926984,
                                      17.4730876006195,
                                      17.933762301608486,
                                      18.39336623784665],
                   'power_fraction': [0.1000032424445086,
                                      0.1250007550843728,
                                      0.15000016154322476,
                                      0.17499913632181735,
                                      0.20000374419509195,
                                      0.22500103644297992,
                                      0.250006245092358,
                                      0.2750085659521902,
                                      0.2999969366316338,
                                      0.3249951759226547,
                                      0.35000856870202285,
                                      0.3750111067519444,
                                      0.4000122202940042,
                                      0.42499856538565056,
                                      0.45002266643888644,
                                      0.47500626006010577,
                                      0.5000122676144121,
                                      0.5250138597038404,
                                      0.5500125433013382,
                                      0.574991848177658,
                                      0.6000208804158026,
                                      0.6250293404019004,
                                      0.6500038980176576,
                                      0.6750103617721294,
                                      0.6999710088886045,
                                      0.7249930304715497,
                                      0.7500054112808188,
                                      0.7750043066394922,
                                      0.7999883729399091,
                                      0.8250387330224463,
                                      0.8500459169478768,
                                      0.8750012654156526,
                                      0.9000127379509377,
                                      0.9250069842527557,
                                      0.9499668181817877,
                                      0.9750125164532161,
                                      1.0],
                   'rated_power_kW': 18.39336623784665,
                   'nh3_t_per_h': [0.00333072861,
                                   0.0041632460599999995,
                                   0.0049958797400000005,
                                   0.00582845142,
                                   0.00666135785,
                                   0.007493759159999999,
                                   0.00832673296,
                                   0.00915943979,
                                   0.00999125457,
                                   0.0108238711,
                                   0.0116573048,
                                   0.0124902493,
                                   0.0133227187,
                                   0.0141545223,
                                   0.0149882523,
                                   0.015820429900000002,
                                   0.016653198,
                                   0.017486023899999998,
                                   0.0183184632,
                                   0.019150162300000002,
                                   0.0199842105,
                                   0.0208171415,
                                   0.0216484642,
                                   0.0224813409,
                                   0.0233116921,
                                   0.024145613399999998,
                                   0.024979103899999997,
                                   0.025811309900000003,
                                   0.0266436815,
                                   0.027479030600000003,
                                   0.0283118972,
                                   0.029142053600000002,
                                   0.029975274,
                                   0.0308081724,
                                   0.0316375475,
                                   0.0324730634,
                                   0.0333056072],
                   'rated_nh3_t_per_h': 0.0333056072,
                   'h2_Nm3_per_h': [6.723797737631944,
                                    8.404747163701389,
                                    10.08569660088889,
                                    11.766646004722224,
                                    13.4475954975,
                                    15.128544879097223,
                                    16.809494371875,
                                    18.490443753472224,
                                    20.171393246250002,
                                    21.85234262784722,
                                    23.533292009444448,
                                    25.214241502222226,
                                    26.895190883819446,
                                    28.576140376597223,
                                    30.257089758194446,
                                    31.938039250972228,
                                    33.61898863256945,
                                    35.299938125347225,
                                    36.98088750694445,
                                    38.661836999722226,
                                    40.34278638131945,
                                    42.02373587409723,
                                    43.70468525569444,
                                    45.38563474847222,
                                    47.06658413006945,
                                    48.747533511666674,
                                    50.42848300444445,
                                    52.10943238604167,
                                    53.790381878819446,
                                    55.47133126041667,
                                    57.15228075319445,
                                    58.83323013479167,
                                    60.51417962756945,
                                    62.19512900916667,
                                    63.876078501944455,
                                    65.55702788354166,
                                    67.23797737631945],
                   'rated_h2_Nm3_per_h': 67.23797737631945},
            'Fe': {'model_id': 'Fe',
                   'source_path': 'Fe_milp_breakpoints.csv',
                   'load_fraction': [0.3,
                                     0.325,
                                     0.35,
                                     0.375,
                                     0.4,
                                     0.425,
                                     0.45,
                                     0.475,
                                     0.5,
                                     0.525,
                                     0.55,
                                     0.575,
                                     0.6,
                                     0.625,
                                     0.65,
                                     0.675,
                                     0.7,
                                     0.725,
                                     0.75,
                                     0.775,
                                     0.8,
                                     0.825,
                                     0.85,
                                     0.875,
                                     0.9,
                                     0.925,
                                     0.95,
                                     0.975,
                                     1.0],
                   'total_power_kW': [6.729621545606808,
                                      7.291773995597433,
                                      7.853759155240574,
                                      8.416153083258662,
                                      8.979099312144106,
                                      9.542857153053994,
                                      10.107152282329745,
                                      10.672149234629577,
                                      11.238032483614449,
                                      11.805037386157254,
                                      12.372596362764146,
                                      12.941538957618533,
                                      13.511604915860438,
                                      14.082689973054796,
                                      14.65505363426877,
                                      15.228333423052952,
                                      15.803321746629546,
                                      16.37956247342651,
                                      16.95671606767033,
                                      17.53537148477531,
                                      18.115440203748772,
                                      18.69650775540286,
                                      19.27922732983677,
                                      19.863201576340675,
                                      20.448682101840102,
                                      21.035761100072833,
                                      21.624336376089953,
                                      22.21406992626568,
                                      22.805349888816536],
                   'power_fraction': [0.2950895986431206,
                                      0.31973962386664495,
                                      0.34438231351547743,
                                      0.36904292739598965,
                                      0.39372775931613074,
                                      0.4184481799042116,
                                      0.44319216024333696,
                                      0.4679669150729877,
                                      0.49278053344515627,
                                      0.5176433356081197,
                                      0.5425304335642539,
                                      0.567478202295195,
                                      0.5924752297918641,
                                      0.6175169441254998,
                                      0.6426147244272462,
                                      0.6677526763367371,
                                      0.6929655464036226,
                                      0.7182333335503371,
                                      0.7435411493504731,
                                      0.7689148191220886,
                                      0.7943504612762974,
                                      0.8198299016044212,
                                      0.8453817820743484,
                                      0.870988679111709,
                                      0.8966616255191895,
                                      0.9224046639331989,
                                      0.9482133131706197,
                                      0.9740727519887422,
                                      1.0],
                   'rated_power_kW': 22.805349888816536,
                   'nh3_t_per_h': [0.0096449215,
                                   0.010450972099999999,
                                   0.011254495,
                                   0.0120576429,
                                   0.0128607349,
                                   0.013663389199999999,
                                   0.0144657828,
                                   0.0152680226,
                                   0.016069105700000003,
                                   0.0168711816,
                                   0.0176722046,
                                   0.0184728246,
                                   0.019272323499999997,
                                   0.0200720062,
                                   0.020871083999999998,
                                   0.0216682589,
                                   0.0224674999,
                                   0.0232646421,
                                   0.024061620399999997,
                                   0.0248578449,
                                   0.0256533571,
                                   0.026449123100000003,
                                   0.0272431144,
                                   0.028037049499999998,
                                   0.0288308183,
                                   0.0296231275,
                                   0.0304149302,
                                   0.0312063226,
                                   0.0319981153],
                   'rated_nh3_t_per_h': 0.0319981153,
                   'h2_Nm3_per_h': [20.17139925,
                                    21.852349187500003,
                                    23.533299125000003,
                                    25.214249062500002,
                                    26.895199,
                                    28.576148937500005,
                                    30.257098875,
                                    31.9380488125,
                                    33.61899875,
                                    35.299948687500006,
                                    36.980898625,
                                    38.661848562500005,
                                    40.3427985,
                                    42.023748437500004,
                                    43.70469837500001,
                                    45.385648312499995,
                                    47.066598250000006,
                                    48.74754818750001,
                                    50.428498125000004,
                                    52.1094480625,
                                    53.790398,
                                    55.4713479375,
                                    57.15229787500001,
                                    58.833247812500005,
                                    60.51419775,
                                    62.195147687500004,
                                    63.876097625,
                                    65.5570475625,
                                    67.2379975],
                   'rated_h2_Nm3_per_h': 67.2379975}},
 'minimum_load_preflight': {'status': 'target_integrated_with_Aspen_anchor_and_Varela_part_load_shape',
                            'target_total_electrical_ratio': 0.25,
                            'target_feasible_in_aspen': False,
                            'target_represented_in_active_curve': True,
                            'active_total_electrical_ratio': 0.25,
                            'raw_aspen_minimum_total_electrical_ratio': 0.3684555092918948,
                            'accepted_process_load_fraction': 0.025,
                            'minimum_is_scan_resolution_dependent': True,
                            'validation_file': 'alk_25pct_electrical_feasibility_v33.json',
                            'correction_file': 'alk_25pct_literature_correction.json',
                            'performance_basis': 'Aspen full-load anchor plus Varela et al. Eq. (5) '
                                                 'normalized part-load shape'},
 'alk_correction': {'schema_version': 1,
                    'status': 'accepted_for_scenario_model',
                    'evidence_class': 'Aspen rated anchor with peer-reviewed normalized part-load shape',
                    'target_total_electrical_load_fraction': 0.25,
                    'method': 'Aspen full-load anchor plus Varela et al. Eq. (5) normalized part-load shape',
                    'aspen_full_load_anchor': {'total_electrical_power_kW': 894.6386073252098,
                                               'hydrogen_kg_per_h': 14.352128,
                                               'makeup_water_kg_per_h': 136.0,
                                               'raw_aspen_minimum_total_electrical_fraction': 0.3684555092918948,
                                               'breakpoint_table_sha256': 'ec88f77f0849eee9fc8b29787b5dd085a7be16b5339dbe852aa26cbad8762c5f'},
                    'literature_shape': {'citation': 'Varela et al. (2021), Modeling alkaline water '
                                                     'electrolysis for power-to-x applications: A scheduling '
                                                     'approach',
                                         'doi': '10.1016/j.ijhydene.2020.12.111',
                                         'pdf_sha256': '0ce3acbf93ce26f1f797d2fe7003bba3f18ddbd3ac89c8f019f5820ce30091cf',
                                         'exact_locator': 'Printed p. 5, Eq. (5); printed p. 3 for the '
                                                          'reported 15-100% load range',
                                         'equation': 'F_H2_Nm3_per_h = 205.31 * U_MW + 17.85',
                                         'slope_Nm3_per_h_per_MW': 205.31,
                                         'intercept_Nm3_per_h': 17.85,
                                         'rated_power_MW': 2.1,
                                         'rated_hydrogen_Nm3_per_h': 449.001,
                                         'target_power_MW': 0.525,
                                         'target_hydrogen_Nm3_per_h': 125.63775,
                                         'normalized_target_hydrogen_fraction': 0.2798161919461204},
                    'integrated_curve': {'power_fraction': [0.25, 1.0],
                                         'hydrogen_fraction': [0.2798161919461204, 1.0],
                                         'water_fraction': [0.2798161919461204, 1.0],
                                         'native_aspen_basis_target_total_power_kW': 223.65965183130245,
                                         'native_aspen_basis_target_hydrogen_kg_per_h': 4.015957803283289,
                                         'native_aspen_basis_target_water_m3_per_h': 0.03805500210467238,
                                         'scaled_10MW_target_hydrogen_kg_per_h': 44.88916273455041,
                                         'scaled_10MW_target_water_m3_per_h': 0.4253673136066552},
                    'scope_limitations': ['The 25% point is not a directly converged Aspen state.',
                                          'The pressurized Aspen model supplies the rated anchor; Varela Eq. '
                                          '(5), reported at 80 C and 1 bar, supplies only the normalized '
                                          'part-load shape.',
                                          'The correction is used as a disclosed scenario input and remains '
                                          'inside the model validation boundary.']},
 'surrogate_source_signature': 'e48772f61963d4bc2c54aa1101e56d7f00452179e03fb5fb3e6620dd405e3960',
 'native_curves': {'ALK': {'model_id': 'ALK',
                           'source_path': 'ALK_milp_breakpoints.csv',
                           'load_fraction': [0.25, 1.0],
                           'total_power_kW': [223.65965183130245, 894.6386073252098],
                           'power_fraction': [0.25, 1.0],
                           'rated_power_kW': 894.6386073252098,
                           'h2_Nm3_per_h': [44.64964196567046, 159.56775644444446],
                           'rated_h2_Nm3_per_h': 159.56775644444446,
                           'water_m3_per_h': [0.03805500210467238, 0.136],
                           'rated_water_m3_per_h': 0.136,
                           'raw_aspen_min_power_fraction': 0.3684555092918948,
                           'minimum_point_basis': 'Aspen full-load anchor plus Varela et al. Eq. (5) '
                                                  'normalized part-load shape',
                           'active_curve_source_path': 'alk_25pct_literature_correction.json',
                           'target_hydrogen_output_fraction': 0.2798161919461204,
                           'target_point_is_direct_aspen': False},
                   'Ru': {'model_id': 'Ru',
                          'source_path': 'Ru_milp_breakpoints.csv',
                          'load_fraction': [0.1,
                                            0.125,
                                            0.15,
                                            0.175,
                                            0.2,
                                            0.225,
                                            0.25,
                                            0.275,
                                            0.3,
                                            0.325,
                                            0.35,
                                            0.375,
                                            0.4,
                                            0.425,
                                            0.45,
                                            0.475,
                                            0.5,
                                            0.525,
                                            0.55,
                                            0.575,
                                            0.6,
                                            0.625,
                                            0.65,
                                            0.675,
                                            0.7,
                                            0.725,
                                            0.75,
                                            0.775,
                                            0.8,
                                            0.825,
                                            0.85,
                                            0.875,
                                            0.9,
                                            0.925,
                                            0.95,
                                            0.975,
                                            1.0],
                          'total_power_kW': [1.8393962632540175,
                                             2.2991846682742407,
                                             2.7590079070006936,
                                             3.2188232056740387,
                                             3.6787421159209224,
                                             4.13852646719081,
                                             4.598456427732593,
                                             5.0583332721036385,
                                             5.517953525697714,
                                             5.977755296278789,
                                             6.437835790520817,
                                             6.89771662974872,
                                             7.357571267481813,
                                             7.817154263697687,
                                             8.277431719142738,
                                             8.736964106555355,
                                             9.196908761648071,
                                             9.656772201478175,
                                             10.116582144351002,
                                             10.576035647307982,
                                             11.036403803843045,
                                             11.496393567411875,
                                             11.9557597522667,
                                             12.415712798416136,
                                             12.874823122363116,
                                             13.33506232934953,
                                             13.795124210054903,
                                             14.254938047928587,
                                             14.714479129502799,
                                             15.17523957689084,
                                             15.635205869408473,
                                             16.09421873336936,
                                             16.5542639078587,
                                             17.013992233926984,
                                             17.4730876006195,
                                             17.933762301608486,
                                             18.39336623784665],
                          'power_fraction': [0.1000032424445086,
                                             0.1250007550843728,
                                             0.15000016154322476,
                                             0.17499913632181735,
                                             0.20000374419509195,
                                             0.22500103644297992,
                                             0.250006245092358,
                                             0.2750085659521902,
                                             0.2999969366316338,
                                             0.3249951759226547,
                                             0.35000856870202285,
                                             0.3750111067519444,
                                             0.4000122202940042,
                                             0.42499856538565056,
                                             0.45002266643888644,
                                             0.47500626006010577,
                                             0.5000122676144121,
                                             0.5250138597038404,
                                             0.5500125433013382,
                                             0.574991848177658,
                                             0.6000208804158026,
                                             0.6250293404019004,
                                             0.6500038980176576,
                                             0.6750103617721294,
                                             0.6999710088886045,
                                             0.7249930304715497,
                                             0.7500054112808188,
                                             0.7750043066394922,
                                             0.7999883729399091,
                                             0.8250387330224463,
                                             0.8500459169478768,
                                             0.8750012654156526,
                                             0.9000127379509377,
                                             0.9250069842527557,
                                             0.9499668181817877,
                                             0.9750125164532161,
                                             1.0],
                          'rated_power_kW': 18.39336623784665,
                          'nh3_t_per_h': [0.00333072861,
                                          0.0041632460599999995,
                                          0.0049958797400000005,
                                          0.00582845142,
                                          0.00666135785,
                                          0.007493759159999999,
                                          0.00832673296,
                                          0.00915943979,
                                          0.00999125457,
                                          0.0108238711,
                                          0.0116573048,
                                          0.0124902493,
                                          0.0133227187,
                                          0.0141545223,
                                          0.0149882523,
                                          0.015820429900000002,
                                          0.016653198,
                                          0.017486023899999998,
                                          0.0183184632,
                                          0.019150162300000002,
                                          0.0199842105,
                                          0.0208171415,
                                          0.0216484642,
                                          0.0224813409,
                                          0.0233116921,
                                          0.024145613399999998,
                                          0.024979103899999997,
                                          0.025811309900000003,
                                          0.0266436815,
                                          0.027479030600000003,
                                          0.0283118972,
                                          0.029142053600000002,
                                          0.029975274,
                                          0.0308081724,
                                          0.0316375475,
                                          0.0324730634,
                                          0.0333056072],
                          'rated_nh3_t_per_h': 0.0333056072,
                          'h2_Nm3_per_h': [6.723797737631944,
                                           8.404747163701389,
                                           10.08569660088889,
                                           11.766646004722224,
                                           13.4475954975,
                                           15.128544879097223,
                                           16.809494371875,
                                           18.490443753472224,
                                           20.171393246250002,
                                           21.85234262784722,
                                           23.533292009444448,
                                           25.214241502222226,
                                           26.895190883819446,
                                           28.576140376597223,
                                           30.257089758194446,
                                           31.938039250972228,
                                           33.61898863256945,
                                           35.299938125347225,
                                           36.98088750694445,
                                           38.661836999722226,
                                           40.34278638131945,
                                           42.02373587409723,
                                           43.70468525569444,
                                           45.38563474847222,
                                           47.06658413006945,
                                           48.747533511666674,
                                           50.42848300444445,
                                           52.10943238604167,
                                           53.790381878819446,
                                           55.47133126041667,
                                           57.15228075319445,
                                           58.83323013479167,
                                           60.51417962756945,
                                           62.19512900916667,
                                           63.876078501944455,
                                           65.55702788354166,
                                           67.23797737631945],
                          'rated_h2_Nm3_per_h': 67.23797737631945},
                   'Fe': {'model_id': 'Fe',
                          'source_path': 'Fe_milp_breakpoints.csv',
                          'load_fraction': [0.3,
                                            0.325,
                                            0.35,
                                            0.375,
                                            0.4,
                                            0.425,
                                            0.45,
                                            0.475,
                                            0.5,
                                            0.525,
                                            0.55,
                                            0.575,
                                            0.6,
                                            0.625,
                                            0.65,
                                            0.675,
                                            0.7,
                                            0.725,
                                            0.75,
                                            0.775,
                                            0.8,
                                            0.825,
                                            0.85,
                                            0.875,
                                            0.9,
                                            0.925,
                                            0.95,
                                            0.975,
                                            1.0],
                          'total_power_kW': [6.729621545606808,
                                             7.291773995597433,
                                             7.853759155240574,
                                             8.416153083258662,
                                             8.979099312144106,
                                             9.542857153053994,
                                             10.107152282329745,
                                             10.672149234629577,
                                             11.238032483614449,
                                             11.805037386157254,
                                             12.372596362764146,
                                             12.941538957618533,
                                             13.511604915860438,
                                             14.082689973054796,
                                             14.65505363426877,
                                             15.228333423052952,
                                             15.803321746629546,
                                             16.37956247342651,
                                             16.95671606767033,
                                             17.53537148477531,
                                             18.115440203748772,
                                             18.69650775540286,
                                             19.27922732983677,
                                             19.863201576340675,
                                             20.448682101840102,
                                             21.035761100072833,
                                             21.624336376089953,
                                             22.21406992626568,
                                             22.805349888816536],
                          'power_fraction': [0.2950895986431206,
                                             0.31973962386664495,
                                             0.34438231351547743,
                                             0.36904292739598965,
                                             0.39372775931613074,
                                             0.4184481799042116,
                                             0.44319216024333696,
                                             0.4679669150729877,
                                             0.49278053344515627,
                                             0.5176433356081197,
                                             0.5425304335642539,
                                             0.567478202295195,
                                             0.5924752297918641,
                                             0.6175169441254998,
                                             0.6426147244272462,
                                             0.6677526763367371,
                                             0.6929655464036226,
                                             0.7182333335503371,
                                             0.7435411493504731,
                                             0.7689148191220886,
                                             0.7943504612762974,
                                             0.8198299016044212,
                                             0.8453817820743484,
                                             0.870988679111709,
                                             0.8966616255191895,
                                             0.9224046639331989,
                                             0.9482133131706197,
                                             0.9740727519887422,
                                             1.0],
                          'rated_power_kW': 22.805349888816536,
                          'nh3_t_per_h': [0.0096449215,
                                          0.010450972099999999,
                                          0.011254495,
                                          0.0120576429,
                                          0.0128607349,
                                          0.013663389199999999,
                                          0.0144657828,
                                          0.0152680226,
                                          0.016069105700000003,
                                          0.0168711816,
                                          0.0176722046,
                                          0.0184728246,
                                          0.019272323499999997,
                                          0.0200720062,
                                          0.020871083999999998,
                                          0.0216682589,
                                          0.0224674999,
                                          0.0232646421,
                                          0.024061620399999997,
                                          0.0248578449,
                                          0.0256533571,
                                          0.026449123100000003,
                                          0.0272431144,
                                          0.028037049499999998,
                                          0.0288308183,
                                          0.0296231275,
                                          0.0304149302,
                                          0.0312063226,
                                          0.0319981153],
                          'rated_nh3_t_per_h': 0.0319981153,
                          'h2_Nm3_per_h': [20.17139925,
                                           21.852349187500003,
                                           23.533299125000003,
                                           25.214249062500002,
                                           26.895199,
                                           28.576148937500005,
                                           30.257098875,
                                           31.9380488125,
                                           33.61899875,
                                           35.299948687500006,
                                           36.980898625,
                                           38.661848562500005,
                                           40.3427985,
                                           42.023748437500004,
                                           43.70469837500001,
                                           45.385648312499995,
                                           47.066598250000006,
                                           48.74754818750001,
                                           50.428498125000004,
                                           52.1094480625,
                                           53.790398,
                                           55.4713479375,
                                           57.15229787500001,
                                           58.833247812500005,
                                           60.51419775,
                                           62.195147687500004,
                                           63.876097625,
                                           65.5570475625,
                                           67.2379975],
                          'rated_h2_Nm3_per_h': 67.2379975}},
 'fe_pwl': {'load_fraction': [0.3, 0.375, 0.475, 0.6, 0.775, 0.875, 1.0],
            'power_kW': [6.729621545606808,
                         8.416153083258662,
                         10.672149234629577,
                         13.511604915860438,
                         17.53537148477531,
                         19.863201576340675,
                         22.805349888816536],
            'nh3_t_per_h': [0.0096449215,
                            0.0120576429,
                            0.0152680226,
                            0.019272323499999997,
                            0.0248578449,
                            0.028037049499999998,
                            0.0319981153],
            'h2_Nm3_per_h': [20.17139925,
                             25.214249062500002,
                             31.9380488125,
                             40.3427985,
                             52.1094480625,
                             58.833247812500005,
                             67.2379975],
            'max_relative_error': 0.0006217711014836972},
 'wind_metadata': {'site_id': 'IM_ULANQAB_SIZIWANG',
                   'site_name': 'Siziwang Banner, Inner Mongolia',
                   'latitude': 41.53,
                   'longitude': 111.7,
                   'profile_year': 2025,
                   'gross_annual_FLH_by_year': {'2021': 3846.45350209091,
                                                '2022': 3672.5469874977803,
                                                '2023': 3870.515068891694,
                                                '2024': 3340.052020578385,
                                                '2025': 3910.5865910532602},
                   'net_annual_FLH_by_year': {'2021': 3654.1308269863644,
                                              '2022': 3488.9196381228912,
                                              '2023': 3676.989315447109,
                                              '2024': 3173.0494195494657,
                                              '2025': 3715.057261500597},
                   'five_year_average_gross_FLH': 3728.030834022406,
                   'five_year_average_net_FLH': 3541.629292321286,
                   'wind_output_factor_eta_WLR': 0.95,
                   'calculated_2025_net_FLH': 3715.0572615005967,
                   'external_AEP_calibration_applied': False,
                   'turbine_rated_power_kW': 10000.0,
                   'turbine_hub_height_m': 125.0,
                   'turbine_rotor_diameter_m': 221.0,
                   'turbine_cut_in_m_per_s': 2.5,
                   'turbine_rated_wind_m_per_s': 11.3,
                   'turbine_cut_out_m_per_s': 24.0,
                   'curve_scope': 'Normalised cubic wind-speed-to-power conversion with Chinese 10 '
                                  'MW onshore WTG1/GWH221 operating parameters and hourly NASA air-density '
                                  'correction.',
                   'loss_scope': 'eta_WLR=0.95 is applied once as the aggregate wind-output factor; no '
                                 'target full-load-hour scaling is used.'}}


ELECTROLYZERS = PARAMETERS['electrolyzers']
SYNTHESES = PARAMETERS['syntheses']

def load_surrogate_curve(model_id):
    return copy.deepcopy(PARAMETERS['curves'][model_id])

def load_alk_literature_correction():
    return copy.deepcopy(PARAMETERS['alk_correction'])

def require_alk_minimum_load_preflight():
    return copy.deepcopy(PARAMETERS['minimum_load_preflight'])

def surrogate_source_signature():
    return PARAMETERS['surrogate_source_signature']

def fe_surrogate_pwl_spec():
    return {key: np.array(value) if isinstance(value,list) else copy.deepcopy(value)
            for key,value in PARAMETERS['fe_pwl'].items()}


def _filesystem_path(path):
    """Use the Windows extended-length form for deeply nested audit files."""
    value = os.path.abspath(os.fspath(path))
    if os.name != "nt" or value.startswith("\\\\?\\") or len(value) < 240:
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value

def crf(years: float) -> float:
    r = DISCOUNT_RATE
    return r * (1.0 + r) ** years / ((1.0 + r) ** years - 1.0)

def annualized_capital_factor(
    equipment_life_y: float,
    replacement_ratio: float = FULL_EQUIPMENT_REPLACEMENT_RATIO,
) -> float:
    """Convert explicit discounted replacements into a project-year annuity."""
    if equipment_life_y <= 0.0:
        raise ValueError("equipment_life_y must be positive")
    if replacement_ratio < 0.0:
        raise ValueError("replacement_ratio must be nonnegative")

    present_value = 1.0
    last_install_year = 0.0
    last_install_ratio = 1.0
    replacement_year = float(equipment_life_y)
    while replacement_year < PROJECT_LIFE_Y - 1.0e-12:
        present_value += replacement_ratio / (
            (1.0 + DISCOUNT_RATE) ** replacement_year
        )
        last_install_year = replacement_year
        last_install_ratio = replacement_ratio
        replacement_year += float(equipment_life_y)

    service_end_year = last_install_year + float(equipment_life_y)
    if service_end_year > PROJECT_LIFE_Y + 1.0e-12:
        unused_life_fraction = (
            service_end_year - PROJECT_LIFE_Y
        ) / float(equipment_life_y)
        present_value -= (
            last_install_ratio
            * unused_life_fraction
            / (1.0 + DISCOUNT_RATE) ** PROJECT_LIFE_Y
        )
    return present_value * crf(PROJECT_LIFE_Y)

def route_input_signature(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    scenario: str,
) -> str:
    """Fingerprint every solved route so stale checkpoints cannot be reused."""

    def numeric_values(mapping: dict) -> dict:
        return {
            str(key): float(value)
            for key, value in sorted(mapping.items())
            if isinstance(value, (bool, int, float, np.number))
        }

    wind_values = np.asarray(wind_per_kw, dtype="<f8")
    payload = {
        "configuration_id": MODEL_CONFIGURATION_ID,
        "model_source_sha256": MODEL_SOURCE_SHA256,
        "scenario": str(scenario),
        "electrolyzer_id": str(el["tech_id"]),
        "electrolyzer_numeric_inputs": numeric_values(el),
        "synthesis_id": str(syn["syn_id"]),
        "synthesis_numeric_inputs": numeric_values(syn),
        "wind_profile_sha256": hashlib.sha256(wind_values.tobytes()).hexdigest(),
        "wind_hours": int(wind_values.size),
        "surrogate_coupling_enabled": bool(SURROGATE_COUPLING_ENABLED),
        "surrogate_source_sha256": surrogate_source_signature(),
        "land_only_active": bool(LAND_ONLY_ACTIVE),
        "land_contract_sha256": (
            land_only.land_contract_payload()["land_contract_sha256"]
            if LAND_ONLY_ACTIVE
            else ""
        ),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

@lru_cache(maxsize=None)
def surrogate_affine_parameters(
    model_id: str,
    output_key: str,
) -> tuple[float, float, float]:
    """Return y = slope*x + intercept and its maximum relative error."""
    curve = load_surrogate_curve(model_id)
    power = np.asarray(curve["total_power_kW"], dtype=float)
    output = np.asarray(curve[output_key], dtype=float)
    design = np.column_stack([power, np.ones_like(power)])
    slope, intercept = np.linalg.lstsq(design, output, rcond=None)[0]
    predicted = slope * power + intercept
    max_relative_error = float(
        np.max(
            np.abs(predicted - output)
            / np.maximum(np.abs(output), 1e-12)
        )
    )
    return float(slope), float(intercept), max_relative_error

def initial_dispatch_coefficients(
    el: dict,
    syn: dict,
    n: int,
) -> dict[str, np.ndarray | float | int | str | bool]:
    """Create full-load coefficients for the first surrogate-coupled MILP."""
    el_h2 = float(el["h2_prod_Nm3_per_h"] / el["unit_power_kW"])
    el_water = float(el["water_m3_per_h"] / el["unit_power_kW"])
    syn_nh3 = float(
        syn["rated_NH3_kg_per_h"] / 1000.0 / syn["rated_power_kW"]
    )
    syn_h2 = float(
        syn["h2_consumption_Nm3_per_h"] / syn["rated_power_kW"]
    )
    return {
        "el_h2_per_kwh": np.full(n, el_h2, dtype=float),
        "el_water_per_kwh": np.full(n, el_water, dtype=float),
        "syn_nh3_t_per_kwh": np.full(n, syn_nh3, dtype=float),
        "syn_h2_per_kwh": np.full(n, syn_h2, dtype=float),
        "surrogate_coupling_enabled": bool(SURROGATE_COUPLING_ENABLED),
        "surrogate_iteration": 0,
        "surrogate_fixed_point_residual": np.nan,
        "surrogate_models": "ALK" if el["tech_id"] == "ALK" else "",
    }

def route_component_factor(syn_id: str, component: str) -> float:
    if syn_id == "Ru":
        return RU_COMPONENT_FACTORS.get(component, 1.0)
    return 1.0

def process_capex_breakdown(syn: dict, capacity_kt_y: float) -> list[dict]:
    if capacity_kt_y <= 0.0:
        raise ValueError("Synthesis nameplate capacity must be positive")
    reference_capacity_kg_nh3_per_h = (
        SYNTHESIS_COST_REFERENCE_KT_PER_Y * 1_000_000.0 / N_HOURS
    )
    capacity_ratio = capacity_kt_y / SYNTHESIS_COST_REFERENCE_KT_PER_Y
    reference_loop_capex_usd_2025 = (
        SYNTHESIS_LOOP_CAPEX_USD_PER_KG_NH3_PER_H
        * reference_capacity_kg_nh3_per_h
    )
    rows = []
    for component, bare_musd in JANO_HB_BARE_COMPONENTS_MUSD.items():
        equipment_share = bare_musd / JANO_HB_TOTAL_BARE_MUSD
        factor = route_component_factor(syn["syn_id"], component)
        scale_exponent = SYNTHESIS_COMPONENT_SCALE_EXPONENTS[component]
        scale_factor = capacity_ratio**scale_exponent
        reference_component_capex_usd_2025 = (
            reference_loop_capex_usd_2025 * equipment_share * factor
        )
        capex_usd_2025 = reference_component_capex_usd_2025 * scale_factor
        scaling_source_id = (
            "YOSHIDA2021"
            if component
            in {"HB reactor", "HB heat exchangers", "HB compressors"}
            else "JANO2024"
        )
        rows.append(
            {
                "syn_id": syn["syn_id"],
                "capacity_kt_per_y": capacity_kt_y,
                "component": component,
                "cost_basis": "300 kt/y reference CAPEX with equipment-level scaling",
                "reference_specific_capex_USD_per_kg_per_h": SYNTHESIS_LOOP_CAPEX_USD_PER_KG_NH3_PER_H,
                "reference_equipment_share": equipment_share,
                "route_equipment_factor": factor,
                "reference_capacity_kt_per_y": SYNTHESIS_COST_REFERENCE_KT_PER_Y,
                "capacity_ratio_to_reference": capacity_ratio,
                "scale_exponent": scale_exponent,
                "scale_factor_from_reference": scale_factor,
                "scaling_source_id": scaling_source_id,
                "installed_capex_USD_2022": np.nan,
                "cost_escalation_2022_to_2025": 1.0,
                "installed_capex_USD_2025": capex_usd_2025,
                "installed_capex_CNY_2025": capex_usd_2025 * USD_TO_CNY_2025,
                "installed_capex_CNY": capex_usd_2025 * USD_TO_CNY_2025,
                "source_id": f"MINGOLLA2026; JANO2024; {scaling_source_id}",
            }
        )

    n2_kg_per_kg_nh3 = syn["fresh_N2_kg_per_h"] / syn["rated_NH3_kg_per_h"]
    reference_n2_capacity_kg_per_h = (
        reference_capacity_kg_nh3_per_h * n2_kg_per_kg_nh3
    )
    reference_asu_capex_usd_2025 = (
        ASU_CAPEX_USD_PER_KG_N2_PER_H * reference_n2_capacity_kg_per_h
    )
    asu_scale_factor = capacity_ratio**ASU_SCALE_EXPONENT
    asu_capex_usd_2025 = reference_asu_capex_usd_2025 * asu_scale_factor
    rows.append(
        {
            "syn_id": syn["syn_id"],
            "capacity_kt_per_y": capacity_kt_y,
            "component": "Air separation unit",
            "cost_basis": "300 kt/y reference cryogenic ASU CAPEX with scaling",
            "reference_specific_capex_USD_per_kg_per_h": ASU_CAPEX_USD_PER_KG_N2_PER_H,
            "reference_equipment_share": np.nan,
            "route_equipment_factor": n2_kg_per_kg_nh3,
            "reference_capacity_kt_per_y": SYNTHESIS_COST_REFERENCE_KT_PER_Y,
            "capacity_ratio_to_reference": capacity_ratio,
            "scale_exponent": ASU_SCALE_EXPONENT,
            "scale_factor_from_reference": asu_scale_factor,
            "scaling_source_id": "DEAN_ASU_SCALE2009",
            "installed_capex_USD_2022": np.nan,
            "cost_escalation_2022_to_2025": 1.0,
            "installed_capex_USD_2025": asu_capex_usd_2025,
            "installed_capex_CNY_2025": asu_capex_usd_2025 * USD_TO_CNY_2025,
            "installed_capex_CNY": asu_capex_usd_2025 * USD_TO_CNY_2025,
            "source_id": (
                f"MINGOLLA2026; DEAN_ASU_SCALE2009; "
                f"ASPEN_{syn['syn_id'].upper()}"
            ),
        }
    )
    return rows

def process_costs(syn: dict, capacity_kt_y: float) -> dict:
    rows = process_capex_breakdown(syn, capacity_kt_y)
    process_equipment_capex_cny = sum(
        row["installed_capex_CNY"] for row in rows
    )
    process_land_capex_cny = (
        land_only.process_land_capex_cny(capacity_kt_y)
        if LAND_ONLY_ACTIVE
        else 0.0
    )
    process_capex_cny = process_equipment_capex_cny + process_land_capex_cny
    capacity_tpd = capacity_kt_y * 1000.0 / 365.0
    catalyst_inventory_kg = (
        syn["catalyst_t_per_100tpd"] * 1000.0 * capacity_tpd / 100.0
    )
    catalyst_capex_cny = catalyst_inventory_kg * syn["catalyst_CNY_per_kg"]
    annual_process_equipment = process_equipment_capex_cny * (
        annualized_capital_factor(SYNTHESIS_LIFE_Y)
        + SYNTHESIS_FIXED_OM_RATIO
    )
    annual_process_land = process_land_capex_cny * annualized_capital_factor(
        SYNTHESIS_LIFE_Y
    )
    annual_process = annual_process_equipment + annual_process_land
    annual_catalyst = catalyst_capex_cny * annualized_capital_factor(
        CATALYST_LIFE_Y,
        replacement_ratio=1.0,
    )
    return {
        "process_capex_CNY": process_capex_cny,
        "process_equipment_capex_CNY": process_equipment_capex_cny,
        "process_land_capex_CNY": process_land_capex_cny,
        "catalyst_inventory_kg": catalyst_inventory_kg,
        "catalyst_capex_CNY": catalyst_capex_cny,
        "annual_process_CNY": annual_process,
        "annual_process_equipment_CNY": annual_process_equipment,
        "annual_process_land_CNY": annual_process_land,
        "annual_catalyst_CNY": annual_catalyst,
        "annual_total_CNY": annual_process + annual_catalyst,
    }

@dataclass
class FixedCapacities:
    wind_kW: float
    electrolyzer_kW: float
    plant_option_index: int
    battery_kWh: float | None = None
    h2_tank_Nm3: float | None = None

class Index:
    def __init__(self, n: int, n_plants: int, n_proxy_segments: int = 0):
        self.n = n
        self.n_plants = n_plants
        self.n_proxy_segments = n_proxy_segments
        self.bat_ch = 0
        self.bat_dis = self.bat_ch + n
        self.bat_soc = self.bat_dis + n
        self.p_el = self.bat_soc + n
        self.p_syn = self.p_el + n
        self.h2_soc = self.p_syn + n
        self.el_on = self.h2_soc + n
        self.el_start = self.el_on + n
        self.el_stop = self.el_start + n
        self.bat_charge_mode = self.el_stop + n
        self.h2_prod = self.bat_charge_mode + n
        self.water_use = self.h2_prod + n
        self.nh3_prod = self.water_use + n
        self.h2_use = self.nh3_prod + n
        self.proxy_segment = self.h2_use + n
        self.proxy_theta = self.proxy_segment + n * n_proxy_segments
        self.el_cap = self.proxy_theta + n * n_proxy_segments
        self.bat_cap = self.el_cap + 1
        self.h2_cap = self.bat_cap + 1
        self.wind_cap = self.h2_cap + 1
        self.plant_y = self.wind_cap + 1
        self.el_modules = self.plant_y + n_plants
        self.wind_units = self.el_modules + 1
        self.size = self.wind_units + 1

    def at(self, start: int, t: int) -> int:
        return start + t

def build_problem(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    scenario: str,
    *,
    lcoa_parameter: float = 0.0,
    fixed: FixedCapacities | None = None,
    plant_option_index: int | None = None,
    enforce_electrolyzer_commitment: bool = ELECTROLYZER_FLEXIBILITY_ENABLED,
    enforce_battery_exclusivity: bool = True,
    production_target_t: float | None = None,
    commitment_block_hours: int = 1,
    curtailment_rate_limit: float | None = None,
    wind_utilization_target: float | None = None,
    battery_charge_mode_fixed: np.ndarray | None = None,
    electrolyzer_on_fixed: np.ndarray | None = None,
    dispatch_coefficients: dict | None = None,
    global_design_integrality: bool = False,
    continuous_design_relaxation: bool = False,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
    wind_capacity_min_kw: float | None = None,
    wind_capacity_max_kw: float | None = None,
):
    n = len(wind_per_kw)
    fixed_el_module_count: int | None = None
    if fixed is not None and fixed.electrolyzer_kW is not None:
        raw_module_count = (
            float(fixed.electrolyzer_kW) / float(el["unit_power_kW"])
        )
        rounded_module_count = int(round(raw_module_count))
        if abs(raw_module_count - rounded_module_count) <= 1e-6:
            fixed_el_module_count = rounded_module_count
        elif enforce_electrolyzer_commitment:
            raise ValueError(
                "An engineering commitment rerun requires fixed electrolyzer "
                "capacity to be an integer number of modules"
            )
    design_module_count = bool(
        (global_design_integrality or continuous_design_relaxation)
        and fixed is None
    )
    module_count_dispatch = bool(
        fixed_el_module_count is not None or design_module_count
    )
    modular_commitment = bool(
        enforce_electrolyzer_commitment and module_count_dispatch
    )
    if curtailment_rate_limit is not None and not (
        0.0 <= float(curtailment_rate_limit) <= 1.0
    ):
        raise ValueError("curtailment_rate_limit must be between 0 and 1")
    if wind_utilization_target is not None and not (
        0.0 <= float(wind_utilization_target) <= 1.0
    ):
        raise ValueError("wind_utilization_target must be between 0 and 1")
    if (
        curtailment_rate_limit is not None
        and wind_utilization_target is not None
    ):
        raise ValueError(
            "Use either curtailment_rate_limit or wind_utilization_target, "
            "not both"
        )
    fixed_battery_mode = None
    if battery_charge_mode_fixed is not None:
        if not enforce_battery_exclusivity:
            raise ValueError(
                "battery_charge_mode_fixed requires battery exclusivity"
            )
        fixed_battery_mode = np.rint(
            np.asarray(battery_charge_mode_fixed, dtype=float)
        ).astype(int)
        if fixed_battery_mode.size != n:
            raise ValueError("battery_charge_mode_fixed must contain one value per hour")
        if np.any((fixed_battery_mode < 0) | (fixed_battery_mode > 1)):
            raise ValueError("battery_charge_mode_fixed values must be 0 or 1")
    fixed_electrolyzer_on = None
    if electrolyzer_on_fixed is not None:
        if not enforce_electrolyzer_commitment:
            raise ValueError(
                "electrolyzer_on_fixed requires electrolyzer commitment"
            )
        fixed_electrolyzer_on = np.rint(
            np.asarray(electrolyzer_on_fixed, dtype=float)
        ).astype(int)
        if fixed_electrolyzer_on.size != n:
            raise ValueError(
                "electrolyzer_on_fixed must contain one value per hour"
            )
        maximum_online = (
            fixed_el_module_count if module_count_dispatch else 1
        )
        if np.any(
            (fixed_electrolyzer_on < 0)
            | (fixed_electrolyzer_on > maximum_online)
        ):
            raise ValueError(
                "electrolyzer_on_fixed values exceed installed module count"
            )
    options = np.asarray(
        PLANT_OPTIONS_KT_PER_Y
        if plant_options_kt_per_y is None
        else plant_options_kt_per_y,
        dtype=float,
    )
    if options.ndim != 1 or options.size == 0:
        raise ValueError("plant_options_kt_per_y must be a non-empty vector")
    if np.any(~np.isfinite(options)) or np.any(options <= 0.0):
        raise ValueError(
            "plant_options_kt_per_y must contain finite positive capacities"
        )
    if np.any(np.diff(options) <= 0.0):
        raise ValueError(
            "plant_options_kt_per_y must be strictly increasing"
        )
    direct_surrogate_milp = bool(
        SURROGATE_COUPLING_ENABLED
        and (
            fixed is not None
            or global_design_integrality
            or continuous_design_relaxation
        )
    )
    fe_pwl = (
        fe_surrogate_pwl_spec()
        if direct_surrogate_milp and syn["syn_id"] == "Fe"
        else None
    )
    # The Fe curve is convex in electrical input and concave in NH3 output
    # when H2 throughput is used as the common coordinate. Its exact six-piece
    # interpolation can therefore be represented by epigraph/hypograph rows,
    # without one segment-selection binary per piece and hour.
    n_proxy_segments = 0
    ix = Index(n, len(options), n_proxy_segments=n_proxy_segments)

    if dispatch_coefficients is None:
        dispatch_coefficients = initial_dispatch_coefficients(el, syn, n)

    def hourly_coefficient(key: str) -> np.ndarray:
        values = np.asarray(dispatch_coefficients[key], dtype=float)
        if values.ndim == 0:
            values = np.full(n, float(values), dtype=float)
        if values.size != n:
            raise ValueError(f"{key} must contain one value per dispatch hour")
        if np.any(~np.isfinite(values)) or np.any(values <= 0.0):
            raise ValueError(f"{key} must be finite and strictly positive")
        return values

    el_h2_per_kwh = hourly_coefficient("el_h2_per_kwh")
    el_water_per_kwh = hourly_coefficient("el_water_per_kwh")
    syn_nh3_t_per_kwh = hourly_coefficient("syn_nh3_t_per_kwh")
    syn_h2_per_kwh = hourly_coefficient("syn_h2_per_kwh")

    reference_syn_nh3_t_per_kwh = (
        float(syn["rated_NH3_kg_per_h"])
        / 1000.0
        / float(syn["rated_power_kW"])
    )
    plant_power_kw = (
        options * 1000.0 / N_HOURS / reference_syn_nh3_t_per_kwh
    )
    plant_nh3_t_per_h = options * 1000.0 / N_HOURS
    el_rated_h2_per_kw = (
        float(el["h2_prod_Nm3_per_h"]) / float(el["unit_power_kW"])
    )
    plant_annual_cost = np.array(
        [process_costs(syn, float(cap))["annual_total_CNY"] for cap in options]
    )

    c = np.zeros(ix.size)
    wind_land_annual_cny_per_kw = (
        land_only.annual_wind_land_cost_cny_per_kw()
        if LAND_ONLY_ACTIVE
        else 0.0
    )
    c[ix.wind_cap] = WIND_CAPEX_CNY_PER_KW * (
        annualized_capital_factor(WIND_LIFE_Y) + WIND_FIXED_OM_RATIO
    ) + wind_land_annual_cny_per_kw
    c[ix.el_cap] = el["installed_capex_CNY_per_kW"] * (
        annualized_capital_factor(ELECTROLYZER_LIFE_Y)
        + el["fixed_om_ratio"]
    )
    c[ix.bat_cap] = BESS_CAPEX_CNY_PER_KWH * (
        annualized_capital_factor(BESS_LIFE_Y) + BESS_FIXED_OM_RATIO
    )
    c[ix.h2_cap] = H2_TANK_CAPEX_CNY_PER_NM3 * (
        annualized_capital_factor(H2_TANK_LIFE_Y)
        + H2_TANK_FIXED_OM_RATIO
    )
    c[ix.plant_y : ix.plant_y + len(options)] = plant_annual_cost

    stack_replacement_cny_per_kwh = (
        STACK_REPLACEMENT_FRACTION
        * el["installed_capex_CNY_per_kW"]
        / el["stack_life_h"]
    )
    el_variable_cny_per_kwh = (
        el_water_per_kwh * WATER_PRICE_CNY_PER_M3
        + stack_replacement_cny_per_kwh
    )
    c[ix.p_el : ix.p_el + n] = stack_replacement_cny_per_kwh
    c[ix.water_use : ix.water_use + n] = WATER_PRICE_CNY_PER_M3
    c[ix.bat_ch : ix.bat_ch + n] = BESS_FLOW_TIEBREAKER_CNY_PER_KWH
    c[ix.bat_dis : ix.bat_dis + n] = BESS_FLOW_TIEBREAKER_CNY_PER_KWH
    c[ix.el_start : ix.el_start + n] = (
        ELECTROLYZER_TRANSITION_TIEBREAKER_CNY_PER_MODULE
    )
    c[ix.el_stop : ix.el_stop + n] = (
        ELECTROLYZER_TRANSITION_TIEBREAKER_CNY_PER_MODULE
    )
    if fe_pwl is not None:
        c[ix.p_syn : ix.p_syn + n] += (
            SURROGATE_POWER_TIEBREAKER_CNY_PER_KWH
        )
    if scenario in {"fixed_wind", "variable_scale"}:
        c[ix.nh3_prod : ix.nh3_prod + n] = -lcoa_parameter

    lb = np.zeros(ix.size)
    ub = np.full(ix.size, np.inf)
    ub[ix.el_cap] = MAX_ELECTROLYZER_KW
    ub[ix.bat_cap] = MAX_BESS_KWH
    ub[ix.h2_cap] = MAX_H2_TANK_NM3
    ub[ix.wind_cap] = MAX_WIND_KW
    maximum_el_modules = int(
        math.floor(MAX_ELECTROLYZER_KW / float(el["unit_power_kW"]))
    )
    maximum_wind_units = int(
        math.floor(MAX_WIND_KW / WIND_TURBINE_KW)
    )
    capacity_quantization_active = bool(
        fixed is not None
        or global_design_integrality
        or continuous_design_relaxation
    )
    ub[ix.el_modules] = (
        float(maximum_el_modules) if capacity_quantization_active else 0.0
    )
    ub[ix.wind_units] = (
        float(maximum_wind_units) if capacity_quantization_active else 0.0
    )
    commitment_upper_bound = float(
        fixed_el_module_count
        if fixed_el_module_count is not None
        else maximum_el_modules
        if design_module_count
        else 1
    )
    ub[ix.el_on : ix.el_on + n] = commitment_upper_bound
    ub[ix.el_start : ix.el_start + n] = commitment_upper_bound
    ub[ix.el_stop : ix.el_stop + n] = commitment_upper_bound
    if n_proxy_segments:
        ub[
            ix.proxy_segment :
            ix.proxy_segment + n * n_proxy_segments
        ] = 1.0
        ub[
            ix.proxy_theta :
            ix.proxy_theta + n * n_proxy_segments
        ] = 1.0
    if fixed_electrolyzer_on is not None:
        lb[ix.el_on : ix.el_on + n] = fixed_electrolyzer_on
        ub[ix.el_on : ix.el_on + n] = fixed_electrolyzer_on
    ub[ix.bat_charge_mode : ix.bat_charge_mode + n] = (
        1.0 if enforce_battery_exclusivity else 0.0
    )
    if fixed_battery_mode is not None:
        lb[ix.bat_charge_mode : ix.bat_charge_mode + n] = fixed_battery_mode
        ub[ix.bat_charge_mode : ix.bat_charge_mode + n] = fixed_battery_mode
    ub[ix.plant_y : ix.plant_y + len(options)] = 1.0
    if not enforce_electrolyzer_commitment:
        ub[ix.el_start : ix.el_start + n] = 0.0
        ub[ix.el_stop : ix.el_stop + n] = 0.0

    if scenario == "fixed_wind":
        lb[ix.wind_cap] = FIXED_WIND_KW
        ub[ix.wind_cap] = FIXED_WIND_KW
    elif scenario == "variable_scale":
        lb[ix.wind_cap] = VARIABLE_SCALE_MIN_WIND_KW
        ub[ix.wind_cap] = VARIABLE_SCALE_MAX_WIND_KW
    if wind_capacity_min_kw is not None:
        lb[ix.wind_cap] = float(wind_capacity_min_kw)
    if wind_capacity_max_kw is not None:
        ub[ix.wind_cap] = float(wind_capacity_max_kw)
    if lb[ix.wind_cap] > ub[ix.wind_cap]:
        raise ValueError("wind capacity lower bound exceeds upper bound")

    if fixed is not None:
        for idx, value in [
            (ix.wind_cap, fixed.wind_kW),
            (ix.el_cap, fixed.electrolyzer_kW),
            (ix.bat_cap, fixed.battery_kWh),
            (ix.h2_cap, fixed.h2_tank_Nm3),
        ]:
            if value is None:
                continue
            lb[idx] = value
            ub[idx] = value
        lb[ix.plant_y : ix.plant_y + len(options)] = 0.0
        ub[ix.plant_y : ix.plant_y + len(options)] = 0.0
        selected = ix.plant_y + fixed.plant_option_index
        lb[selected] = 1.0
        ub[selected] = 1.0
        fixed_modules = float(fixed.electrolyzer_kW) / float(
            el["unit_power_kW"]
        )
        fixed_wind_units = float(fixed.wind_kW) / WIND_TURBINE_KW
        if abs(fixed_modules - round(fixed_modules)) > 1e-6:
            raise ValueError(
                "Fixed electrolyzer capacity is not an integer module count"
            )
        if abs(fixed_wind_units - round(fixed_wind_units)) > 1e-6:
            raise ValueError(
                "Fixed wind capacity is not an integer 10 MW turbine count"
            )
        lb[ix.el_modules] = round(fixed_modules)
        ub[ix.el_modules] = round(fixed_modules)
        lb[ix.wind_units] = round(fixed_wind_units)
        ub[ix.wind_units] = round(fixed_wind_units)
    elif plant_option_index is not None:
        lb[ix.plant_y : ix.plant_y + len(options)] = 0.0
        ub[ix.plant_y : ix.plant_y + len(options)] = 0.0
        selected = ix.plant_y + plant_option_index
        lb[selected] = 1.0
        ub[selected] = 1.0

    eq_r: list[int] = []
    eq_c: list[int] = []
    eq_d: list[float] = []
    eq_b: list[float] = []
    ub_r: list[int] = []
    ub_c: list[int] = []
    ub_d: list[float] = []
    ub_b: list[float] = []
    indicator_constraints: list[tuple[int, int, int]] = []

    def add_eq(row: int, col: int, value: float) -> None:
        eq_r.append(row)
        eq_c.append(col)
        eq_d.append(value)

    def add_ub(row: int, col: int, value: float) -> None:
        ub_r.append(row)
        ub_c.append(col)
        ub_d.append(value)

    eq_row = 0
    ub_row = 0

    if LAND_ONLY_ACTIVE:
        add_ub(
            ub_row,
            ix.wind_cap,
            land_only.wind_spacing_control_ha_per_turbine()
            / WIND_TURBINE_KW,
        )
        for j, capacity_kt_y in enumerate(options):
            add_ub(
                ub_row,
                ix.plant_y + j,
                land_only.process_site_ha(float(capacity_kt_y)),
            )
        ub_b.append(land_only.SITE_ENVELOPE_HA)
        ub_row += 1

    if capacity_quantization_active:
        # Exact equipment-size links. These two integer variables make wind
        # capacity a count of 10 MW turbines and electrolyzer capacity a count
        # of technology-specific modules in the design-level MILP.
        add_eq(eq_row, ix.el_cap, 1.0)
        add_eq(eq_row, ix.el_modules, -float(el["unit_power_kW"]))
        eq_b.append(0.0)
        eq_row += 1

        add_eq(eq_row, ix.wind_cap, 1.0)
        add_eq(eq_row, ix.wind_units, -WIND_TURBINE_KW)
        eq_b.append(0.0)
        eq_row += 1

    surrogate_model_error = 0.0
    if direct_surrogate_milp and el["tech_id"] == "ALK":
        alk_curve = load_surrogate_curve("ALK")
        # Aspen was validated on its native ~0.895 MW basis. Scale both the
        # affine intercept and rated outputs to the selected 10 MW engineering
        # module; the slope is already expressed per kW and remains unchanged.
        alk_online_intercept_scale = float(el["surrogate_module_scale"])
        el_h2_slope, el_h2_intercept, el_h2_error = (
            surrogate_affine_parameters("ALK", "h2_Nm3_per_h")
        )
        el_water_slope, el_water_intercept, el_water_error = (
            surrogate_affine_parameters("ALK", "water_m3_per_h")
        )
        surrogate_model_error = max(
            surrogate_model_error,
            el_h2_error,
            el_water_error,
        )
        for t in range(n):
            add_eq(eq_row, ix.at(ix.h2_prod, t), 1.0)
            add_eq(eq_row, ix.at(ix.p_el, t), -el_h2_slope)
            add_eq(
                eq_row,
                ix.at(ix.el_on, t),
                -el_h2_intercept * alk_online_intercept_scale,
            )
            eq_b.append(0.0)
            eq_row += 1

            add_eq(eq_row, ix.at(ix.water_use, t), 1.0)
            add_eq(eq_row, ix.at(ix.p_el, t), -el_water_slope)
            add_eq(
                eq_row,
                ix.at(ix.el_on, t),
                -el_water_intercept * alk_online_intercept_scale,
            )
            eq_b.append(0.0)
            eq_row += 1
    else:
        for t in range(n):
            add_eq(eq_row, ix.at(ix.h2_prod, t), 1.0)
            add_eq(
                eq_row,
                ix.at(ix.p_el, t),
                -float(el_h2_per_kwh[t]),
            )
            eq_b.append(0.0)
            eq_row += 1

            add_eq(eq_row, ix.at(ix.water_use, t), 1.0)
            add_eq(
                eq_row,
                ix.at(ix.p_el, t),
                -float(el_water_per_kwh[t]),
            )
            eq_b.append(0.0)
            eq_row += 1

    if fe_pwl is not None:
        fe_reference_nh3_t_per_h = float(
            np.asarray(load_surrogate_curve("Fe")["nh3_t_per_h"])[-1]
        )
        plant_scale = (
            np.asarray(plant_nh3_t_per_h, dtype=float)
            / fe_reference_nh3_t_per_h
        )
        knot_power = np.asarray(fe_pwl["power_kW"], dtype=float)
        knot_nh3 = np.asarray(fe_pwl["nh3_t_per_h"], dtype=float)
        knot_h2 = np.asarray(fe_pwl["h2_Nm3_per_h"], dtype=float)
        surrogate_model_error = max(
            surrogate_model_error,
            float(fe_pwl["max_relative_error"]),
        )
        power_slopes = np.diff(knot_power) / np.diff(knot_h2)
        power_intercepts = knot_power[:-1] - power_slopes * knot_h2[:-1]
        nh3_slopes = np.diff(knot_nh3) / np.diff(knot_h2)
        nh3_intercepts = knot_nh3[:-1] - nh3_slopes * knot_h2[:-1]
        for t in range(n):
            h2_index = ix.at(ix.h2_use, t)

            # The Aspen curve scales linearly with selected nameplate. The
            # scale is an exact linear expression of the one-hot plant choice,
            # so no fixed capacity or big-M segment selector is required.
            add_ub(ub_row, h2_index, -1.0)
            for j, scale in enumerate(plant_scale):
                add_ub(
                    ub_row,
                    ix.plant_y + j,
                    float(knot_h2[0]) * float(scale),
                )
            ub_b.append(0.0)
            ub_row += 1

            add_ub(ub_row, h2_index, 1.0)
            for j, scale in enumerate(plant_scale):
                add_ub(
                    ub_row,
                    ix.plant_y + j,
                    -float(knot_h2[-1]) * float(scale),
                )
            ub_b.append(0.0)
            ub_row += 1

            for segment in range(len(power_slopes)):
                # Convex electrical-input epigraph:
                # p_syn includes native process electricity and common-gate
                # product conditioning.
                add_ub(ub_row, ix.at(ix.p_syn, t), -1.0)
                add_ub(ub_row, h2_index, float(power_slopes[segment]))
                add_ub(
                    ub_row,
                    ix.at(ix.nh3_prod, t),
                    float(
                        syn["product_conditioning_electric_kWh_per_kgNH3"]
                    )
                    * 1000.0,
                )
                for j, scale in enumerate(plant_scale):
                    add_ub(
                        ub_row,
                        ix.plant_y + j,
                        float(power_intercepts[segment]) * float(scale),
                    )
                ub_b.append(0.0)
                ub_row += 1

                # Concave NH3-output hypograph:
                # nh3_prod <= slope * h2_use + intercept * plant_scale.
                add_ub(ub_row, ix.at(ix.nh3_prod, t), 1.0)
                add_ub(ub_row, h2_index, -float(nh3_slopes[segment]))
                for j, scale in enumerate(plant_scale):
                    add_ub(
                        ub_row,
                        ix.plant_y + j,
                        -float(nh3_intercepts[segment]) * float(scale),
                    )
                ub_b.append(0.0)
                ub_row += 1
    elif direct_surrogate_milp and syn["syn_id"] == "Ru":
        ru_curve = load_surrogate_curve("Ru")
        ru_reference_nh3_t_per_h = float(
            np.asarray(ru_curve["nh3_t_per_h"])[-1]
        )
        plant_scale = (
            np.asarray(plant_nh3_t_per_h, dtype=float)
            / ru_reference_nh3_t_per_h
        )
        syn_nh3_slope, syn_nh3_intercept, syn_nh3_error = (
            surrogate_affine_parameters("Ru", "nh3_t_per_h")
        )
        syn_h2_slope, syn_h2_intercept, syn_h2_error = (
            surrogate_affine_parameters("Ru", "h2_Nm3_per_h")
        )
        surrogate_model_error = max(
            surrogate_model_error,
            syn_nh3_error,
            syn_h2_error,
        )
        for t in range(n):
            add_eq(eq_row, ix.at(ix.nh3_prod, t), 1.0)
            add_eq(eq_row, ix.at(ix.p_syn, t), -syn_nh3_slope)
            for j, scale in enumerate(plant_scale):
                add_eq(
                    eq_row,
                    ix.plant_y + j,
                    -syn_nh3_intercept * float(scale),
                )
            eq_b.append(0.0)
            eq_row += 1

            add_eq(eq_row, ix.at(ix.h2_use, t), 1.0)
            add_eq(eq_row, ix.at(ix.p_syn, t), -syn_h2_slope)
            for j, scale in enumerate(plant_scale):
                add_eq(
                    eq_row,
                    ix.plant_y + j,
                    -syn_h2_intercept * float(scale),
                )
            eq_b.append(0.0)
            eq_row += 1
    else:
        for t in range(n):
            add_eq(eq_row, ix.at(ix.nh3_prod, t), 1.0)
            add_eq(
                eq_row,
                ix.at(ix.p_syn, t),
                -float(syn_nh3_t_per_kwh[t]),
            )
            eq_b.append(0.0)
            eq_row += 1

            add_eq(eq_row, ix.at(ix.h2_use, t), 1.0)
            add_eq(
                eq_row,
                ix.at(ix.p_syn, t),
                -float(syn_h2_per_kwh[t]),
            )
            eq_b.append(0.0)
            eq_row += 1

    for t in range(n):
        add_eq(eq_row, ix.at(ix.bat_soc, t), 1.0)
        add_eq(eq_row, ix.at(ix.bat_ch, t), -BESS_CHARGE_EFFICIENCY)
        add_eq(eq_row, ix.at(ix.bat_dis, t), 1.0 / BESS_DISCHARGE_EFFICIENCY)
        if t == 0:
            add_eq(eq_row, ix.bat_cap, -0.50)
        else:
            add_eq(eq_row, ix.at(ix.bat_soc, t - 1), -1.0)
        eq_b.append(0.0)
        eq_row += 1

    for t in range(n):
        add_eq(eq_row, ix.at(ix.h2_soc, t), 1.0)
        add_eq(eq_row, ix.at(ix.h2_prod, t), -1.0)
        add_eq(eq_row, ix.at(ix.h2_use, t), 1.0)
        if t == 0:
            add_eq(eq_row, ix.h2_cap, -0.50)
        else:
            add_eq(eq_row, ix.at(ix.h2_soc, t - 1), -1.0)
        eq_b.append(0.0)
        eq_row += 1

    add_eq(eq_row, ix.at(ix.bat_soc, n - 1), 1.0)
    add_eq(eq_row, ix.bat_cap, -0.50)
    eq_b.append(0.0)
    eq_row += 1
    add_eq(eq_row, ix.at(ix.h2_soc, n - 1), 1.0)
    add_eq(eq_row, ix.h2_cap, -0.50)
    eq_b.append(0.0)
    eq_row += 1

    if enforce_electrolyzer_commitment and commitment_block_hours > 1:
        block_hours = int(commitment_block_hours)
        for t in range(n):
            block_start = (t // block_hours) * block_hours
            if t == block_start:
                continue
            add_eq(eq_row, ix.at(ix.el_on, t), 1.0)
            add_eq(eq_row, ix.at(ix.el_on, block_start), -1.0)
            eq_b.append(0.0)
            eq_row += 1

    if enforce_electrolyzer_commitment:
        # Exact hourly transition identity: on[t] - on[t-1] = start[t] - stop[t].
        # The horizon is cyclic, matching the cyclic battery and H2 inventories.
        for t in range(n):
            previous = n - 1 if t == 0 else t - 1
            add_eq(eq_row, ix.at(ix.el_on, t), 1.0)
            add_eq(eq_row, ix.at(ix.el_on, previous), -1.0)
            add_eq(eq_row, ix.at(ix.el_start, t), -1.0)
            add_eq(eq_row, ix.at(ix.el_stop, t), 1.0)
            eq_b.append(0.0)
            eq_row += 1

    for j in range(len(options)):
        add_eq(eq_row, ix.plant_y + j, 1.0)
    eq_b.append(1.0)
    eq_row += 1

    horizon_fraction = n / N_HOURS
    effective_production_target_t = (
        float(production_target_t)
        if production_target_t is not None
        else TARGET_NH3_T_PER_Y * horizon_fraction
        if scenario == "fixed_300kt"
        else None
    )
    if effective_production_target_t is not None:
        for t in range(n):
            add_eq(eq_row, ix.at(ix.nh3_prod, t), 1.0)
        eq_b.append(float(effective_production_target_t))
        eq_row += 1

    if wind_utilization_target is not None:
        # Exact annual utilization remains linear when wind capacity is a
        # decision variable because the hourly per-kW wind profile is fixed.
        # Net process use includes charging and excludes battery discharge.
        add_eq(
            eq_row,
            ix.wind_cap,
            -float(wind_utilization_target) * float(np.sum(wind_per_kw)),
        )
        for t in range(n):
            add_eq(eq_row, ix.at(ix.p_el, t), 1.0)
            add_eq(eq_row, ix.at(ix.p_syn, t), 1.0)
            add_eq(eq_row, ix.at(ix.bat_ch, t), 1.0)
            add_eq(eq_row, ix.at(ix.bat_dis, t), -1.0)
        eq_b.append(0.0)
        eq_row += 1

    electrolyzer_big_m = (
        float(fixed.electrolyzer_kW)
        if fixed is not None and fixed.electrolyzer_kW is not None
        else MAX_ELECTROLYZER_KW
    )

    def finish_installed_module_limit(row: int) -> None:
        if design_module_count:
            add_ub(row, ix.el_modules, -1.0)
            ub_b.append(0.0)
        else:
            ub_b.append(commitment_upper_bound)

    for t in range(n):
        # Available wind is an upper bound. Curtailment is the nonnegative
        # slack and is calculated after the solve, so no hourly curtailment
        # decision variable is needed.
        add_ub(ub_row, ix.at(ix.p_el, t), 1.0)
        add_ub(ub_row, ix.at(ix.p_syn, t), 1.0)
        add_ub(ub_row, ix.at(ix.bat_ch, t), 1.0)
        add_ub(ub_row, ix.at(ix.bat_dis, t), -1.0)
        add_ub(ub_row, ix.wind_cap, -float(wind_per_kw[t]))
        ub_b.append(0.0)
        ub_row += 1

        add_ub(ub_row, ix.at(ix.p_el, t), 1.0)
        add_ub(ub_row, ix.el_cap, -1.0)
        ub_b.append(0.0)
        ub_row += 1

        if design_module_count:
            add_ub(ub_row, ix.at(ix.el_on, t), 1.0)
            add_ub(ub_row, ix.el_modules, -1.0)
            ub_b.append(0.0)
            ub_row += 1

        # Electrolyzer unit commitment: Gurobi receives exact indicator rows,
        # which are much tighter than big-M over 8760 hours. The continuous
        # sizing relaxation retains the corresponding big-M convex relaxation.
        if module_count_dispatch:
            # Both fixed-capacity and design-global models use exact online
            # module counts, linked to installed modules when size is endogenous.
            online_power_quantum = float(el["unit_power_kW"])
            add_ub(ub_row, ix.at(ix.p_el, t), 1.0)
            add_ub(
                ub_row,
                ix.at(ix.el_on, t),
                -online_power_quantum,
            )
            ub_b.append(0.0)
            ub_row += 1

            # Minimum load is defined on total electrical input, not hydrogen
            # throughput. This distinction matters for the Aspen ALK model,
            # whose fixed balance-of-plant duty makes the two fractions differ.
            add_ub(ub_row, ix.at(ix.p_el, t), -1.0)
            add_ub(
                ub_row,
                ix.at(ix.el_on, t),
                float(el["unit_min_load_ratio"])
                * online_power_quantum,
            )
            ub_b.append(0.0)
            ub_row += 1
        elif enforce_electrolyzer_commitment and GUROBI_CL.exists():
            add_ub(ub_row, ix.at(ix.p_el, t), 1.0)
            ub_b.append(0.0)
            indicator_constraints.append((ub_row, ix.at(ix.el_on, t), 0))
            ub_row += 1

            add_ub(ub_row, ix.at(ix.p_el, t), -1.0)
            add_ub(ub_row, ix.el_cap, float(el["unit_min_load_ratio"]))
            ub_b.append(0.0)
            indicator_constraints.append((ub_row, ix.at(ix.el_on, t), 1))
            ub_row += 1
        else:
            add_ub(ub_row, ix.at(ix.p_el, t), 1.0)
            add_ub(ub_row, ix.at(ix.el_on, t), -electrolyzer_big_m)
            ub_b.append(0.0)
            ub_row += 1

            add_ub(ub_row, ix.at(ix.p_el, t), -1.0)
            add_ub(ub_row, ix.el_cap, float(el["unit_min_load_ratio"]))
            add_ub(ub_row, ix.at(ix.el_on, t), electrolyzer_big_m)
            ub_b.append(electrolyzer_big_m)
            ub_row += 1

        el_previous = n - 1 if t == 0 else t - 1
        if enforce_electrolyzer_commitment:
            # Tight start/stop definitions around the online module count.
            add_ub(ub_row, ix.at(ix.el_start, t), 1.0)
            add_ub(ub_row, ix.at(ix.el_stop, t), 1.0)
            finish_installed_module_limit(ub_row)
            ub_row += 1

            add_ub(ub_row, ix.at(ix.el_start, t), 1.0)
            add_ub(ub_row, ix.at(ix.el_on, t), -1.0)
            ub_b.append(0.0)
            ub_row += 1

            add_ub(ub_row, ix.at(ix.el_start, t), 1.0)
            add_ub(ub_row, ix.at(ix.el_on, el_previous), 1.0)
            finish_installed_module_limit(ub_row)
            ub_row += 1

            add_ub(ub_row, ix.at(ix.el_stop, t), 1.0)
            add_ub(ub_row, ix.at(ix.el_on, el_previous), -1.0)
            ub_b.append(0.0)
            ub_row += 1

            add_ub(ub_row, ix.at(ix.el_stop, t), 1.0)
            add_ub(ub_row, ix.at(ix.el_on, t), 1.0)
            finish_installed_module_limit(ub_row)
            ub_row += 1

        # Two-sided hourly endpoint ramp envelope. A genuine start or stop is
        # exempted by its explicit transition variable.
        if enforce_electrolyzer_commitment:
            startup_ramp_exemption_kw = (
                float(el["unit_power_kW"])
                if module_count_dispatch
                else electrolyzer_big_m
            )
            add_ub(ub_row, ix.at(ix.p_el, t), 1.0)
            add_ub(ub_row, ix.at(ix.p_el, el_previous), -1.0)
            add_ub(ub_row, ix.el_cap, -float(el["ramp_up_ratio_per_h"]))
            add_ub(
                ub_row,
                ix.at(ix.el_start, t),
                -startup_ramp_exemption_kw,
            )
            ub_b.append(0.0)
            ub_row += 1

            add_ub(ub_row, ix.at(ix.p_el, t), -1.0)
            add_ub(ub_row, ix.at(ix.p_el, el_previous), 1.0)
            add_ub(ub_row, ix.el_cap, -float(el["ramp_down_ratio_per_h"]))
            add_ub(
                ub_row,
                ix.at(ix.el_stop, t),
                -startup_ramp_exemption_kw,
            )
            ub_b.append(0.0)
            ub_row += 1

        # Standard clustered minimum-down/restart-preparation inequality:
        # sum(start[t-D+1:t]) + on[t-D] <= installed modules. It prevents a
        # module count that was online at t-D, or restarted more recently,
        # from being counted as available for another restart at t.
        cold_start_h = int(el["cold_start_hours_model"])
        if enforce_electrolyzer_commitment and cold_start_h >= 1:
            for lag in range(cold_start_h):
                restart_hour = (t - lag) % n
                add_ub(
                    ub_row,
                    ix.at(ix.el_start, restart_hour),
                    1.0,
                )
            prior_eligibility_hour = (t - cold_start_h) % n
            add_ub(
                ub_row,
                ix.at(ix.el_on, prior_eligibility_hour),
                1.0,
            )
            finish_installed_module_limit(ub_row)
            ub_row += 1

        add_ub(ub_row, ix.at(ix.p_syn, t), 1.0)
        for j, power in enumerate(plant_power_kw):
            add_ub(ub_row, ix.plant_y + j, -float(power))
        ub_b.append(0.0)
        ub_row += 1

        add_ub(
            ub_row,
            ix.at(ix.nh3_prod, t),
            -1.0,
        )
        for j, rated_output in enumerate(plant_nh3_t_per_h):
            add_ub(
                ub_row,
                ix.plant_y + j,
                float(syn["min_load_ratio"]) * float(rated_output),
            )
        ub_b.append(0.0)
        ub_row += 1

        add_ub(ub_row, ix.at(ix.bat_soc, t), 1.0)
        add_ub(ub_row, ix.bat_cap, -BESS_MAX_SOC)
        ub_b.append(0.0)
        ub_row += 1

        add_ub(ub_row, ix.at(ix.bat_soc, t), -1.0)
        add_ub(ub_row, ix.bat_cap, BESS_MIN_SOC)
        ub_b.append(0.0)
        ub_row += 1

        add_ub(ub_row, ix.at(ix.h2_soc, t), 1.0)
        add_ub(ub_row, ix.h2_cap, -H2_MAX_SOC)
        ub_b.append(0.0)
        ub_row += 1

        add_ub(ub_row, ix.at(ix.h2_soc, t), -1.0)
        add_ub(ub_row, ix.h2_cap, H2_MIN_SOC)
        ub_b.append(0.0)
        ub_row += 1

        add_ub(ub_row, ix.at(ix.bat_ch, t), 1.0)
        add_ub(ub_row, ix.bat_cap, -1.0 / BESS_DURATION_H)
        ub_b.append(0.0)
        ub_row += 1

        add_ub(ub_row, ix.at(ix.bat_dis, t), 1.0)
        add_ub(ub_row, ix.bat_cap, -1.0 / BESS_DURATION_H)
        ub_b.append(0.0)
        ub_row += 1

        if enforce_battery_exclusivity:
            battery_power_big_m = MAX_BESS_KWH / BESS_DURATION_H
            if fixed_battery_mode is not None:
                if fixed_battery_mode[t] == 1:
                    add_ub(ub_row, ix.at(ix.bat_dis, t), 1.0)
                else:
                    add_ub(ub_row, ix.at(ix.bat_ch, t), 1.0)
                ub_b.append(0.0)
                ub_row += 1
            elif GUROBI_CL.exists():
                add_ub(ub_row, ix.at(ix.bat_ch, t), 1.0)
                ub_b.append(0.0)
                indicator_constraints.append(
                    (ub_row, ix.at(ix.bat_charge_mode, t), 0)
                )
                ub_row += 1

                add_ub(ub_row, ix.at(ix.bat_dis, t), 1.0)
                ub_b.append(0.0)
                indicator_constraints.append(
                    (ub_row, ix.at(ix.bat_charge_mode, t), 1)
                )
                ub_row += 1
            else:
                add_ub(ub_row, ix.at(ix.bat_ch, t), 1.0)
                add_ub(
                    ub_row,
                    ix.at(ix.bat_charge_mode, t),
                    -battery_power_big_m,
                )
                ub_b.append(0.0)
                ub_row += 1

                add_ub(ub_row, ix.at(ix.bat_dis, t), 1.0)
                add_ub(
                    ub_row,
                    ix.at(ix.bat_charge_mode, t),
                    battery_power_big_m,
                )
                ub_b.append(battery_power_big_m)
                ub_row += 1

        previous = n - 1 if t == 0 else t - 1
        # Keep both endpoint-ramp rows explicit even when the cap is 100%/h.
        # At that value the rows are redundant with the nameplate bounds, but
        # retaining them makes the active Ru assumption directly auditable.
        add_ub(
            ub_row,
            ix.at(ix.nh3_prod, t),
            1.0,
        )
        add_ub(
            ub_row,
            ix.at(ix.nh3_prod, previous),
            -1.0,
        )
        for j, rated_output in enumerate(plant_nh3_t_per_h):
            add_ub(
                ub_row,
                ix.plant_y + j,
                -float(syn["ramp_ratio_per_h"]) * float(rated_output),
            )
        ub_b.append(0.0)
        ub_row += 1

        add_ub(
            ub_row,
            ix.at(ix.nh3_prod, t),
            -1.0,
        )
        add_ub(
            ub_row,
            ix.at(ix.nh3_prod, previous),
            1.0,
        )
        for j, rated_output in enumerate(plant_nh3_t_per_h):
            add_ub(
                ub_row,
                ix.plant_y + j,
                -float(syn["ramp_ratio_per_h"]) * float(rated_output),
            )
        ub_b.append(0.0)
        ub_row += 1

    if curtailment_rate_limit is not None:
        # Annual curtailment is the slack in the hourly wind-power balance:
        # sum(wind) - sum(net use).  The epsilon constraint below is exactly
        # equivalent to curtailment / annual wind generation <= epsilon and
        # remains linear even when wind capacity is an endogenous variable.
        add_ub(
            ub_row,
            ix.wind_cap,
            (1.0 - float(curtailment_rate_limit)) * float(np.sum(wind_per_kw)),
        )
        for t in range(n):
            add_ub(ub_row, ix.at(ix.p_el, t), -1.0)
            add_ub(ub_row, ix.at(ix.p_syn, t), -1.0)
            add_ub(ub_row, ix.at(ix.bat_ch, t), -1.0)
            add_ub(ub_row, ix.at(ix.bat_dis, t), 1.0)
        ub_b.append(0.0)
        ub_row += 1

    if (
        scenario in {"fixed_wind", "variable_scale"}
        and production_target_t is None
    ):
        for t in range(n):
            add_ub(ub_row, ix.at(ix.nh3_prod, t), -1.0)
        ub_b.append(-MIN_FIXED_WIND_NH3_T_PER_Y * horizon_fraction)
        ub_row += 1

    a_eq = coo_matrix((eq_d, (eq_r, eq_c)), shape=(eq_row, ix.size)).tocsr()
    a_ub = coo_matrix((ub_d, (ub_r, ub_c)), shape=(ub_row, ix.size)).tocsr()
    constraints = [
        LinearConstraint(a_eq, np.asarray(eq_b), np.asarray(eq_b)),
        LinearConstraint(a_ub, -np.inf, np.asarray(ub_b)),
    ]
    integrality = np.zeros(ix.size, dtype=int)
    if enforce_electrolyzer_commitment and fixed_electrolyzer_on is None:
        integrality[ix.el_on : ix.el_on + n] = 1
        # Start/stop are exact differences of adjacent integer online-module
        # counts. Their transition identity and tight bounds make them integer
        # automatically, avoiding another 17,520 integer declarations.
    if enforce_battery_exclusivity and fixed_battery_mode is None:
        integrality[ix.bat_charge_mode : ix.bat_charge_mode + n] = 1
    if n_proxy_segments:
        integrality[
            ix.proxy_segment :
            ix.proxy_segment + n * n_proxy_segments
        ] = 1
    if (
        fixed is None
        and plant_option_index is None
        and not continuous_design_relaxation
    ):
        integrality[ix.plant_y : ix.plant_y + len(options)] = 1
    if global_design_integrality:
        integrality[ix.el_modules] = 1
        integrality[ix.wind_units] = 1

    terms = {
        "el_h2_per_kwh": el_h2_per_kwh,
        "el_water_per_kwh": el_water_per_kwh,
        "syn_nh3_t_per_kwh": syn_nh3_t_per_kwh,
        "syn_h2_per_kwh": syn_h2_per_kwh,
        "plant_power_kw": plant_power_kw,
        "plant_nh3_t_per_h": plant_nh3_t_per_h,
        "plant_annual_cost": plant_annual_cost,
        "land_only_active": bool(LAND_ONLY_ACTIVE),
        "land_constraint_row_count": 1 if LAND_ONLY_ACTIVE else 0,
        "land_contract_sha256": (
            land_only.land_contract_payload()["land_contract_sha256"]
            if LAND_ONLY_ACTIVE
            else ""
        ),
        "wind_land_annual_CNY_per_kW": wind_land_annual_cny_per_kw,
        "process_land_cost_active": bool(LAND_ONLY_ACTIVE),
        "stack_replacement_cny_per_kwh": stack_replacement_cny_per_kwh,
        "el_variable_cny_per_kwh": el_variable_cny_per_kwh,
        "horizon_fraction": horizon_fraction,
        "electrolyzer_commitment_enforced": enforce_electrolyzer_commitment,
        "electrolyzer_commitment_mode": (
            "integer_online_and_installed_module_count"
            if global_design_integrality
            else "integer_online_module_count"
            if modular_commitment
            else "continuous_online_module_count_relaxation"
            if module_count_dispatch
            else "binary_aggregate_or_relaxed"
        ),
        "fixed_electrolyzer_module_count": (
            fixed_el_module_count
            if fixed_el_module_count is not None
            else np.nan
        ),
        "global_design_integrality": bool(global_design_integrality),
        "continuous_design_relaxation": bool(continuous_design_relaxation),
        "design_global_certificate_eligible": bool(
            global_design_integrality
            and enforce_electrolyzer_commitment
        ),
        "capacity_quantization_active": bool(capacity_quantization_active),
        "plant_options_kt_per_y": options.copy(),
        "wind_capacity_min_kw": float(lb[ix.wind_cap]),
        "wind_capacity_max_kw": float(ub[ix.wind_cap]),
        "battery_charge_discharge_exclusivity_enforced": (
            enforce_battery_exclusivity
        ),
        "production_target_t": (
            float(effective_production_target_t)
            if effective_production_target_t is not None
            else np.nan
        ),
        "production_target_source": (
            "explicit_argument"
            if production_target_t is not None
            else "fixed_300kt_scenario_default"
            if scenario == "fixed_300kt"
            else "none"
        ),
        "curtailment_rate_limit": curtailment_rate_limit,
        "wind_utilization_target": wind_utilization_target,
        "battery_charge_mode_fixed_for_screening": fixed_battery_mode is not None,
        "commitment_block_hours": int(commitment_block_hours),
        "electrolyzer_transition_rows": n if enforce_electrolyzer_commitment else 0,
        "electrolyzer_ramp_rows": 2 * n if enforce_electrolyzer_commitment else 0,
        "synthesis_ramp_rows": 2 * n,
        "indicator_constraints": indicator_constraints,
        "surrogate_coupling_enabled": bool(
            dispatch_coefficients.get("surrogate_coupling_enabled", False)
        ),
        "surrogate_models": str(
            dispatch_coefficients.get("surrogate_models", "")
        ),
        "surrogate_iteration": int(
            dispatch_coefficients.get("surrogate_iteration", 0)
        ),
        "surrogate_fixed_point_residual": float(
            dispatch_coefficients.get(
                "surrogate_fixed_point_residual", np.nan
            )
        ),
        "surrogate_direct_milp": direct_surrogate_milp,
        "surrogate_pwl_segments": (
            len(np.asarray(fe_pwl["power_kW"], dtype=float)) - 1
            if fe_pwl is not None
            else n_proxy_segments
        ),
        "surrogate_model_max_relative_error": surrogate_model_error,
        "surrogate_source_sha256": surrogate_source_signature(),
    }
    return c, integrality, Bounds(lb, ub), constraints, ix, terms

def solve_problem(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    scenario: str,
    *,
    lcoa_parameter: float = 0.0,
    fixed: FixedCapacities | None = None,
    plant_option_index: int | None = None,
    enforce_electrolyzer_commitment: bool = ELECTROLYZER_FLEXIBILITY_ENABLED,
    enforce_battery_exclusivity: bool = True,
    production_target_t: float | None = None,
    electrolyzer_on_start: np.ndarray | None = None,
    solution_start: np.ndarray | None = None,
    commitment_block_hours: int = 1,
    curtailment_rate_limit: float | None = None,
    wind_utilization_target: float | None = None,
    battery_charge_mode_fixed: np.ndarray | None = None,
    electrolyzer_on_fixed: np.ndarray | None = None,
    accept_feasible: bool = False,
    mip_rel_gap_override: float | None = None,
    time_limit_override_s: float | None = None,
    dispatch_coefficients: dict | None = None,
    global_design_integrality: bool = False,
    continuous_design_relaxation: bool = False,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
    wind_capacity_min_kw: float | None = None,
    wind_capacity_max_kw: float | None = None,
):
    c, integrality, bounds, constraints, ix, terms = build_problem(
        el,
        syn,
        wind_per_kw,
        scenario,
        lcoa_parameter=lcoa_parameter,
        fixed=fixed,
        plant_option_index=plant_option_index,
        enforce_electrolyzer_commitment=enforce_electrolyzer_commitment,
        enforce_battery_exclusivity=enforce_battery_exclusivity,
        production_target_t=production_target_t,
        commitment_block_hours=commitment_block_hours,
        curtailment_rate_limit=curtailment_rate_limit,
        wind_utilization_target=wind_utilization_target,
        battery_charge_mode_fixed=battery_charge_mode_fixed,
        electrolyzer_on_fixed=electrolyzer_on_fixed,
        dispatch_coefficients=dispatch_coefficients,
        global_design_integrality=global_design_integrality,
        continuous_design_relaxation=continuous_design_relaxation,
        plant_options_kt_per_y=plant_options_kt_per_y,
        wind_capacity_min_kw=wind_capacity_min_kw,
        wind_capacity_max_kw=wind_capacity_max_kw,
    )
    mip_start = None
    if solution_start is not None:
        start_values = np.asarray(solution_start, dtype=float)
        if start_values.size != ix.size:
            raise ValueError("solution_start must contain one value per model variable")
        mip_start = {
            column: float(value)
            for column, value in enumerate(start_values)
            if np.isfinite(value)
        }
    if electrolyzer_on_start is not None:
        maximum_online_modules = int(
            round(float(np.max(bounds.ub[ix.el_on : ix.el_on + ix.n])))
        )
        on_start = np.clip(
            np.rint(np.asarray(electrolyzer_on_start)).astype(int),
            0,
            maximum_online_modules,
        )
        if on_start.size != ix.n:
            raise ValueError("electrolyzer_on_start must contain one value per hour")
        previous_on = np.roll(on_start, 1)
        start_start = np.maximum(on_start - previous_on, 0)
        stop_start = np.maximum(previous_on - on_start, 0)
        if mip_start is None:
            mip_start = {}
        for t in range(ix.n):
            mip_start[ix.at(ix.el_on, t)] = float(on_start[t])
            mip_start[ix.at(ix.el_start, t)] = float(start_start[t])
            mip_start[ix.at(ix.el_stop, t)] = float(stop_start[t])
    start = time.time()
    selected_mip_gap = (
        float(mip_rel_gap_override)
        if mip_rel_gap_override is not None
        else MIP_REL_GAP
        if enforce_electrolyzer_commitment
        else SIZING_MIP_REL_GAP
    )
    result = solve_matrix_problem(
        c,
        integrality,
        bounds,
        constraints,
        indicator_constraints=terms["indicator_constraints"],
        mip_start=mip_start,
        accept_feasible=accept_feasible,
        mip_rel_gap=selected_mip_gap,
        time_limit_s=(
            float(time_limit_override_s)
            if time_limit_override_s is not None
            else MIP_TIME_LIMIT_S
        ),
    )
    elapsed = time.time() - start
    result.allowed_mip_gap = selected_mip_gap
    if not result.success:
        raise RuntimeError(
            f"{scenario} {syn['syn_id']}+{el['tech_id']} failed: "
            f"status={result.status}, message={result.message}"
        )
    return result, ix, terms, elapsed

def gurobi_attempt_profiles(
    *,
    continuous_large_model: bool,
    small_design_mip: bool,
) -> list[tuple[str, list[str]]]:
    """Return deterministic solver profiles without changing model tolerances."""
    if continuous_large_model:
        return [
            (
                "homogeneous_barrier",
                ["Method=2", "BarHomogeneous=1", "Crossover=0"],
            ),
            (
                "dual_simplex_fallback",
                ["Method=1", "DualReductions=0", "Crossover=-1"],
            ),
        ]
    if small_design_mip:
        return [("barrier_root_mip", ["Method=2", "Crossover=-1"])]
    return [("dual_simplex", ["Method=1", "Crossover=-1"])]

def solve_matrix_problem(
    c,
    integrality,
    bounds,
    constraints,
    *,
    indicator_constraints=(),
    mip_start=None,
    accept_feasible=False,
    mip_rel_gap=MIP_REL_GAP,
    time_limit_s=MIP_TIME_LIMIT_S,
):
    """Solve the assembled sparse model, preferring the licensed Gurobi CLI."""
    if GUROBI_CL.exists():
        equality = constraints[0]
        a_eq = equality.A.tocsr()
        b_eq = np.asarray(equality.ub, dtype=float)
        inequality = constraints[1]
        a_ub = inequality.A.tocsr()
        b_ub = np.asarray(inequality.ub, dtype=float)
        a_all = vstack([a_eq, a_ub], format="csc")
        n_eq = a_eq.shape[0]

        with tempfile.TemporaryDirectory(prefix="wind_h2_nh3_") as temp_name:
            temp_dir = Path(temp_name)
            model_path = temp_dir / "dispatch.mps"
            solution_path = temp_dir / "dispatch.sol"
            start_path = temp_dir / "dispatch.mst"
            log_path = temp_dir / "gurobi.log"

            with model_path.open("w", encoding="ascii", newline="\n") as handle:
                handle.write("NAME          WINDH2NH3\n")
                handle.write("ROWS\n")
                handle.write(" N  OBJ\n")
                for row in range(n_eq):
                    handle.write(f" E  E{row}\n")
                for row in range(a_ub.shape[0]):
                    handle.write(f" L  L{row}\n")

                handle.write("COLUMNS\n")
                integer_mode = False
                marker = 0
                for col in range(len(c)):
                    is_integer = int(integrality[col]) == 1
                    if is_integer != integer_mode:
                        marker_name = f"M{marker}"
                        marker += 1
                        marker_type = "INTORG" if is_integer else "INTEND"
                        handle.write(
                            f"    {marker_name:<8}  'MARKER'                 "
                            f"'{marker_type}'\n"
                        )
                        integer_mode = is_integer
                    variable = f"X{col}"
                    objective = float(c[col])
                    start, end = a_all.indptr[col], a_all.indptr[col + 1]
                    if objective != 0.0:
                        handle.write(f"    {variable:<12} OBJ       {objective:.16g}\n")
                    elif start == end:
                        # Indicator-only binaries still need a COLUMNS entry.
                        handle.write(f"    {variable:<12} OBJ       0\n")
                    for row, value in zip(
                        a_all.indices[start:end], a_all.data[start:end]
                    ):
                        row_name = f"E{row}" if row < n_eq else f"L{row - n_eq}"
                        handle.write(
                            f"    {variable:<12} {row_name:<10} {float(value):.16g}\n"
                        )
                if integer_mode:
                    handle.write(
                        f"    M{marker:<7}  'MARKER'                 'INTEND'\n"
                    )

                handle.write("RHS\n")
                for row, value in enumerate(b_eq):
                    if float(value) != 0.0:
                        handle.write(f"    RHS1         E{row:<9} {float(value):.16g}\n")
                for row, value in enumerate(b_ub):
                    if float(value) != 0.0:
                        handle.write(f"    RHS1         L{row:<9} {float(value):.16g}\n")

                handle.write("BOUNDS\n")
                for col in range(len(c)):
                    variable = f"X{col}"
                    lower = float(bounds.lb[col])
                    upper = float(bounds.ub[col])
                    is_binary = (
                        int(integrality[col]) == 1
                        and lower == 0.0
                        and upper == 1.0
                    )
                    if is_binary:
                        handle.write(f" BV BND1        {variable}\n")
                    elif np.isfinite(lower) and np.isfinite(upper) and lower == upper:
                        handle.write(f" FX BND1        {variable:<12} {lower:.16g}\n")
                    else:
                        if np.isfinite(lower) and lower != 0.0:
                            handle.write(f" LO BND1        {variable:<12} {lower:.16g}\n")
                        elif not np.isfinite(lower):
                            handle.write(f" MI BND1        {variable}\n")
                        if np.isfinite(upper):
                            handle.write(f" UP BND1        {variable:<12} {upper:.16g}\n")
                if indicator_constraints:
                    handle.write("INDICATORS\n")
                    for row, binary_col, binary_value in indicator_constraints:
                        handle.write(
                            f" IF L{int(row)}      X{int(binary_col)}        "
                            f"{int(binary_value)}\n"
                        )
                handle.write("ENDATA\n")

            if mip_start is not None:
                # A partial integer start or a complete engineering solution can
                # be supplied. Preserve continuous values when a complete
                # solution is available; rounding them would destroy feasibility.
                with start_path.open("w", encoding="ascii", newline="\n") as handle:
                    handle.write("# Dispatch MIP start\n")
                    for column, raw_value in sorted(mip_start.items()):
                        value = float(raw_value)
                        formatted = (
                            f"{value:.0f}"
                            if int(integrality[column]) == 1
                            else f"{value:.16g}"
                        )
                        handle.write(f"X{column} {formatted}\n")

            proof_priority = float(mip_rel_gap) <= MIP_REL_GAP + 1e-12
            integer_variable_count = int(np.count_nonzero(integrality))
            continuous_large_model = bool(
                integer_variable_count == 0
                and not indicator_constraints
                and a_all.shape[0] >= 100_000
            )
            small_design_mip = bool(
                0 < integer_variable_count <= 50
                and a_all.shape[0] >= 100_000
            )
            gurobi_threads = int(
                os.environ.get("WIND_NH3_GUROBI_THREADS", "20")
            )
            has_integer_variables = bool(
                np.any(np.asarray(integrality) == 1)
            )
            profiles = gurobi_attempt_profiles(
                continuous_large_model=continuous_large_model,
                small_design_mip=small_design_mip,
            )
            attempt_outputs: list[str] = []
            attempted_profiles: list[str] = []
            completed = None
            output_text = ""
            selected_profile = profiles[-1][0]
            for attempt_number, (profile_name, method_parameters) in enumerate(
                profiles,
                start=1,
            ):
                attempt_solution_path = (
                    temp_dir / f"dispatch_{attempt_number}_{profile_name}.sol"
                )
                attempt_log_path = (
                    temp_dir / f"gurobi_{attempt_number}_{profile_name}.log"
                )
                command = [
                    str(GUROBI_CL),
                    f"TimeLimit={time_limit_s}",
                    f"MIPGap={mip_rel_gap}",
                    f"Threads={gurobi_threads}",
                    f"MIPFocus={2 if proof_priority else 1}",
                    f"Heuristics={0.20 if proof_priority else 0.50}",
                    "Cuts=3",
                    "Presolve=2",
                    *method_parameters,
                    "NodeMethod=1",
                    "NumericFocus=3",
                    "ScaleFlag=2",
                    f"ResultFile={attempt_solution_path}",
                    f"LogFile={attempt_log_path}",
                    *(
                        [f"InputFile={start_path}"]
                        if start_path.exists()
                        else []
                    ),
                    str(model_path),
                ]
                completed_attempt = subprocess.run(
                    command,
                    cwd=temp_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                attempt_log_text = (
                    attempt_log_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                    if attempt_log_path.exists()
                    else ""
                )
                attempt_output = (
                    f"{completed_attempt.stdout}\n"
                    f"{completed_attempt.stderr}\n{attempt_log_text}"
                )
                attempt_outputs.append(
                    f"===== Gurobi attempt {attempt_number}: "
                    f"{profile_name} =====\n{attempt_output}"
                )
                attempted_profiles.append(profile_name)
                attempt_infeasible = bool(
                    "Infeasible model" in attempt_output
                    or "Model is infeasible" in attempt_output
                )
                attempt_optimal = bool(
                    not attempt_infeasible
                    and (
                        "Optimal solution found" in attempt_output
                        or (
                            not has_integer_variables
                            and re.search(
                                r"Optimal objective\s", attempt_output
                            )
                        )
                    )
                )
                completed = completed_attempt
                output_text = attempt_output
                solution_path = attempt_solution_path
                selected_profile = profile_name
                if attempt_solution_path.exists() and (
                    attempt_optimal or accept_feasible
                ):
                    break
            if completed is None:
                raise RuntimeError("No Gurobi solver profile was executed")
            audit_output_text = "\n\n".join(attempt_outputs)
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            log_tag = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "_",
                os.environ.get("WIND_NH3_GUROBI_LOG_TAG", ""),
            ).strip("_")
            shared_log_name = (
                f"latest_gurobi_{log_tag}.log"
                if log_tag
                else "latest_gurobi.log"
            )
            (AUDIT_DIR / shared_log_name).write_text(
                audit_output_text,
                encoding="utf-8",
            )
            # ``Optimal objective`` also appears for a MIP root relaxation.
            # It proves the complete model only for a genuinely continuous LP.
            infeasible = bool(
                "Infeasible model" in output_text
                or "Model is infeasible" in output_text
            )
            optimal = bool(
                not infeasible
                and (
                    "Optimal solution found" in output_text
                    or (
                        not has_integer_variables
                        and re.search(r"Optimal objective\s", output_text)
                    )
                )
            )
            has_solution = solution_path.exists()
            x = np.zeros(len(c), dtype=float) if has_solution else None
            parsed_values = 0
            if has_solution:
                for line in solution_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    if not line or line.startswith("#"):
                        continue
                    name, value = line.split()[:2]
                    if name.startswith("X"):
                        x[int(name[1:])] = float(value)
                        parsed_values += 1
            solution_count_match = re.findall(
                r"Solution count\s+([0-9]+)", output_text
            )
            reported_solution_count = (
                int(solution_count_match[-1]) if solution_count_match else 0
            )
            # Gurobi's LP log reports ``Optimal objective`` and does not emit
            # the MIP-only ``Solution count`` line.  The sizing stage is an LP
            # for a fixed synthesis option, so require that line only when the
            # exported model actually contains integer variables.
            solver_reported_solution = bool(
                reported_solution_count > 0 or not has_integer_variables
            )
            has_solution = bool(
                has_solution and parsed_values > 0 and solver_reported_solution
            )
            if not has_solution:
                x = None
            numeric_pattern = (
                r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
                r"(?:[eE][-+]?\d+)?"
            )
            gap_match = re.findall(
                rf"gap\s+({numeric_pattern})%",
                output_text,
                flags=re.IGNORECASE,
            )
            mip_gap = float(gap_match[-1]) / 100.0 if gap_match else (0.0 if optimal else np.nan)
            bound_matches = re.findall(
                rf"Best objective\s+({numeric_pattern}),\s+best bound\s+"
                rf"({numeric_pattern}),\s+gap",
                output_text,
                flags=re.IGNORECASE,
            )
            if bound_matches:
                solver_incumbent_objective = float(bound_matches[-1][0])
                solver_best_bound = float(bound_matches[-1][1])
            else:
                solver_incumbent_objective = (
                    float(np.dot(c, x)) if has_solution else np.nan
                )
                solver_best_bound = (
                    solver_incumbent_objective if optimal else np.nan
            )
            if not optimal or not has_solution:
                os.makedirs(_filesystem_path(AUDIT_DIR), exist_ok=True)
                nonoptimal_log_name = (
                    f"latest_gurobi_nonoptimal_{log_tag}.log"
                    if log_tag
                    else "latest_gurobi_nonoptimal.log"
                )
                with open(_filesystem_path(AUDIT_DIR / nonoptimal_log_name), "w",
                          encoding="utf-8", newline="\n") as stream:
                    stream.write(audit_output_text)
            parsed_gap_within_tolerance = bool(
                (
                    not has_integer_variables
                    and optimal
                )
                or (
                    np.isfinite(mip_gap)
                    and mip_gap <= float(mip_rel_gap) + 1e-7
                )
            )
            solution_is_acceptable = bool(
                has_solution and (optimal or accept_feasible)
            )
            return SimpleNamespace(
                success=solution_is_acceptable,
                status=(
                    0
                    if solution_is_acceptable
                    else 2
                    if infeasible
                    else completed.returncode or 1
                ),
                message=(
                    (
                        "Gurobi CLI: optimal"
                        if completed.returncode == 0
                        else (
                            "Gurobi CLI: optimal solution parsed; "
                            f"process return code {completed.returncode} ignored"
                        )
                    )
                    if optimal
                    else "Gurobi CLI: infeasible"
                    if infeasible
                    else "Gurobi CLI: feasible incumbent accepted for warm start"
                    if has_solution and accept_feasible
                    else "Gurobi CLI did not prove optimality; inspect solver log"
                ),
                x=x,
                fun=float(np.dot(c, x)) if has_solution else np.nan,
                mip_gap=mip_gap,
                incumbent_objective=solver_incumbent_objective,
                best_bound=solver_best_bound,
                solver_backend=(
                    "Gurobi 10.0.1 command-line sparse MPS; "
                    f"profile={selected_profile}"
                ),
                solver_attempt_count=len(attempted_profiles),
                solver_attempt_profiles=";".join(attempted_profiles),
                proven_optimal=bool(optimal and parsed_gap_within_tolerance),
                solver_returncode=int(completed.returncode),
            )

    result = milp(
        c=c,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options={"time_limit": time_limit_s, "mip_rel_gap": mip_rel_gap},
    )
    result.solver_backend = "HiGHS via scipy.optimize.milp"
    result.incumbent_objective = float(result.fun) if result.x is not None else np.nan
    result.best_bound = float(
        getattr(result, "mip_dual_bound", result.fun)
    )
    result.proven_optimal = bool(
        result.success
        and (
            not np.any(np.asarray(integrality) == 1)
            or float(getattr(result, "mip_gap", np.inf))
            <= float(mip_rel_gap) + 1e-7
        )
    )
    return result

def extract_result(
    result,
    ix: Index,
    terms: dict,
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    scenario: str,
    stage: str,
    solve_seconds: float,
) -> tuple[dict, pd.DataFrame]:
    x = np.asarray(result.x, dtype=float)
    n = ix.n

    def block(start: int) -> np.ndarray:
        return x[start : start + n]

    bat_ch_raw = block(ix.bat_ch)
    bat_dis_raw = block(ix.bat_dis)
    # Retain exact SOC-preserving netting as numerical cleanup. The engineering
    # MILP uses an exact cost-minimizing battery-direction relaxation plus a
    # negligible throughput tie-breaker, and the raw result is audited below.
    battery_soc_change = (
        BESS_CHARGE_EFFICIENCY * bat_ch_raw
        - bat_dis_raw / BESS_DISCHARGE_EFFICIENCY
    )
    bat_ch = np.maximum(battery_soc_change / BESS_CHARGE_EFFICIENCY, 0.0)
    bat_dis = np.maximum(
        -battery_soc_change * BESS_DISCHARGE_EFFICIENCY,
        0.0,
    )
    bat_soc = block(ix.bat_soc)
    p_el = block(ix.p_el)
    p_syn = block(ix.p_syn)
    h2_soc = block(ix.h2_soc)
    h2_production_nm3_per_h = block(ix.h2_prod)
    water_m3_per_h = block(ix.water_use)
    nh3_production_t_per_h = block(ix.nh3_prod)
    h2_consumption_nm3_per_h = block(ix.h2_use)
    el_on_raw = block(ix.el_on)
    el_start_raw = block(ix.el_start)
    el_stop_raw = block(ix.el_stop)
    el_on = np.rint(el_on_raw).astype(int)
    el_start = np.rint(el_start_raw).astype(int)
    el_stop = np.rint(el_stop_raw).astype(int)
    wind_kW = float(x[ix.wind_cap])
    el_kW = float(x[ix.el_cap])
    battery_kWh = float(x[ix.bat_cap])
    h2_tank_Nm3 = float(x[ix.h2_cap])
    plant_values = x[ix.plant_y : ix.plant_y + ix.n_plants]
    plant_options = np.asarray(
        terms.get("plant_options_kt_per_y", PLANT_OPTIONS_KT_PER_Y),
        dtype=float,
    )
    if bool(terms.get("continuous_design_relaxation", False)):
        plant_kt_y = float(np.dot(plant_values, plant_options))
        plant_index = int(np.argmin(np.abs(plant_options - plant_kt_y)))
        option_process_costs = [
            process_costs(syn, float(option)) for option in plant_options
        ]
        selected_process = {
            key: float(
                sum(
                    float(weight) * float(costs[key])
                    for weight, costs in zip(
                        plant_values,
                        option_process_costs,
                    )
                )
            )
            for key in option_process_costs[0]
        }
    else:
        plant_index = int(np.argmax(plant_values))
        plant_kt_y = float(plant_options[plant_index])
        selected_process = process_costs(syn, plant_kt_y)

    wind_generation = wind_per_kw * wind_kW
    available_minus_use = wind_generation - p_el - p_syn - bat_ch + bat_dis
    curt = np.maximum(available_minus_use, 0.0)
    annual_nh3 = float(nh3_production_t_per_h.sum())
    annual_h2_production = float(h2_production_nm3_per_h.sum())
    annual_h2_use = float(h2_consumption_nm3_per_h.sum())
    annual_water = float(water_m3_per_h.sum())

    wind_capex = wind_kW * WIND_CAPEX_CNY_PER_KW
    el_capex = el_kW * el["installed_capex_CNY_per_kW"]
    battery_capex = battery_kWh * BESS_CAPEX_CNY_PER_KWH
    h2_capex = h2_tank_Nm3 * H2_TANK_CAPEX_CNY_PER_NM3

    annual_wind_equipment = wind_capex * (
        annualized_capital_factor(WIND_LIFE_Y) + WIND_FIXED_OM_RATIO
    )
    annual_wind_land = (
        wind_kW * land_only.annual_wind_land_cost_cny_per_kw()
        if bool(terms.get("land_only_active", False))
        else 0.0
    )
    annual_wind = annual_wind_equipment + annual_wind_land
    annual_el = el_capex * (
        annualized_capital_factor(ELECTROLYZER_LIFE_Y)
        + el["fixed_om_ratio"]
    )
    annual_battery = battery_capex * (
        annualized_capital_factor(BESS_LIFE_Y) + BESS_FIXED_OM_RATIO
    )
    annual_h2_tank = h2_capex * (
        annualized_capital_factor(H2_TANK_LIFE_Y)
        + H2_TANK_FIXED_OM_RATIO
    )
    annual_stack = float(p_el.sum() * terms["stack_replacement_cny_per_kwh"])
    annual_water_cost = annual_water * WATER_PRICE_CNY_PER_M3
    annual_battery_tiebreaker = float(
        (bat_ch_raw.sum() + bat_dis_raw.sum())
        * BESS_FLOW_TIEBREAKER_CNY_PER_KWH
    )
    annual_transition_tiebreaker = float(
        (el_start_raw.sum() + el_stop_raw.sum())
        * ELECTROLYZER_TRANSITION_TIEBREAKER_CNY_PER_MODULE
    )
    annual_surrogate_power_tiebreaker = float(
        p_syn.sum() * SURROGATE_POWER_TIEBREAKER_CNY_PER_KWH
        if bool(terms.get("surrogate_direct_milp", False))
        and syn["syn_id"] == "Fe"
        else 0.0
    )
    annual_total = (
        annual_wind
        + annual_el
        + annual_battery
        + annual_h2_tank
        + selected_process["annual_process_CNY"]
        + selected_process["annual_catalyst_CNY"]
        + annual_stack
        + annual_water_cost
        + annual_battery_tiebreaker
        + annual_transition_tiebreaker
        + annual_surrogate_power_tiebreaker
    )

    land_audit = (
        land_only.build_land_audit(
            wind_capacity_MW=wind_kW / 1_000.0,
            synthesis_nameplate_kt_per_y=plant_kt_y,
        )
        if bool(terms.get("land_only_active", False))
        else {}
    )
    if land_audit:
        land_audit["annual_process_land_cost_CNY"] = float(
            selected_process["annual_process_land_CNY"]
        )

    power_residual = available_minus_use - curt
    annual_generation = float(wind_generation.sum())
    annual_consumption = float((p_el + p_syn + bat_ch - bat_dis).sum())
    annual_direct_process_load = float((p_el + p_syn).sum())
    electrolyzer_energy_share = float(
        p_el.sum() / max(annual_direct_process_load, 1e-12)
    )
    annual_electricity_supply_cost = (
        annual_wind + annual_battery + annual_battery_tiebreaker
    )
    annual_h2_electricity_allocation = (
        annual_electricity_supply_cost * electrolyzer_energy_share
    )
    annual_ammonia_electricity_allocation = (
        annual_electricity_supply_cost - annual_h2_electricity_allocation
    )
    annual_h2_subsystem_cost = (
        annual_h2_electricity_allocation
        + annual_el
        + annual_h2_tank
        + annual_stack
        + annual_water_cost
        + annual_transition_tiebreaker
    )
    annual_ammonia_conversion_cost = (
        annual_ammonia_electricity_allocation
        + selected_process["annual_process_CNY"]
        + selected_process["annual_catalyst_CNY"]
        + annual_surrogate_power_tiebreaker
    )
    allocation_reconciliation_residual = (
        annual_h2_subsystem_cost
        + annual_ammonia_conversion_cost
        - annual_total
    )
    raw_simultaneous_battery_kw = np.minimum(bat_ch_raw, bat_dis_raw)
    simultaneous_battery_kw = np.minimum(bat_ch, bat_dis)
    battery_netting_energy_kwh = float(
        np.sum((bat_ch_raw - bat_dis_raw) - (bat_ch - bat_dis))
    )
    selected_plant_power_kw = (
        float(np.dot(plant_values, terms["plant_power_kw"]))
        if bool(terms.get("continuous_design_relaxation", False))
        else float(terms["plant_power_kw"][plant_index])
    )
    selected_plant_nh3_t_per_h = float(
        terms["plant_nh3_t_per_h"][plant_index]
    )
    synthesis_ramp_t_per_h = np.diff(
        np.concatenate(
            [[nh3_production_t_per_h[-1]], nh3_production_t_per_h]
        )
    )
    el_previous_on = np.roll(el_on, 1)
    el_previous_power = np.roll(p_el, 1)
    expected_el_start = np.maximum(el_on - el_previous_on, 0)
    expected_el_stop = np.maximum(el_previous_on - el_on, 0)
    transition_residual = np.maximum(
        np.abs(el_start - expected_el_start),
        np.abs(el_stop - expected_el_stop),
    )
    el_running = el_on > 0
    el_continuously_running = (el_on > 0) & (el_previous_on > 0)
    installed_el_modules = int(
        round(el_kW / max(float(el["unit_power_kW"]), 1e-12))
    )
    online_h2_nameplate = (
        el_on.astype(float) * float(el["h2_prod_Nm3_per_h"])
    )
    el_running_load = (
        h2_production_nm3_per_h[el_running]
        / np.maximum(online_h2_nameplate[el_running], 1e-12)
        if np.any(el_running)
        else np.array([], dtype=float)
    )
    online_power_nameplate = (
        el_on.astype(float) * float(el["unit_power_kW"])
    )
    el_running_power_load = (
        p_el[el_running]
        / np.maximum(online_power_nameplate[el_running], 1e-12)
        if np.any(el_running)
        else np.array([], dtype=float)
    )
    el_continuous_ramp = p_el - el_previous_power
    min_load_violation_kw = (
        float(
            np.max(
                np.maximum(
                    float(el["unit_min_load_ratio"])
                    * online_power_nameplate[el_running]
                    - p_el[el_running],
                    0.0,
                )
            )
        )
        if np.any(el_running)
        else 0.0
    )
    off_power_violation_kw = (
        float(np.max(np.abs(p_el[~el_running])))
        if np.any(~el_running)
        else 0.0
    )
    up_violation = np.maximum(
        el_continuous_ramp
        - float(el["ramp_up_ratio_per_h"]) * el_kW
        - el_start * float(el["unit_power_kW"]),
        0.0,
    )
    down_violation = np.maximum(
        -el_continuous_ramp
        - float(el["ramp_down_ratio_per_h"]) * el_kW
        - el_stop * float(el["unit_power_kW"]),
        0.0,
    )
    ramp_violation_kw = float(max(np.max(up_violation), np.max(down_violation)))

    cold_start_h = int(el["cold_start_hours_model"])
    clustered_cold_start_violation = np.zeros(n, dtype=float)
    if cold_start_h >= 1:
        for t in range(n):
            recent_starts = sum(
                float(el_start[(t - lag) % n])
                for lag in range(cold_start_h)
            )
            clustered_cold_start_violation[t] = max(
                recent_starts
                + float(el_on[(t - cold_start_h) % n])
                - installed_el_modules,
                0.0,
            )
    raw_gap = getattr(result, "mip_gap", None)
    mip_gap = float(raw_gap) if raw_gap is not None else np.nan
    global_design_certified = bool(
        terms.get("design_global_certificate_eligible", False)
        and getattr(result, "proven_optimal", False)
        and (
            scenario != "fixed_wind"
            or getattr(result, "fractional_global_certified", False)
        )
    )

    summary = {
        "model_configuration_id": MODEL_CONFIGURATION_ID,
        "model_source_sha256": MODEL_SOURCE_SHA256,
        "route_input_signature": route_input_signature(
            el, syn, wind_per_kw, scenario
        ),
        "scenario": scenario,
        "stage": stage,
        "route": f"{syn['syn_id']}+{el['tech_id']}",
        "syn_id": syn["syn_id"],
        "tech_id": el["tech_id"],
        "status": (
            "Design-global solution certified within the declared gap"
            if global_design_certified
            else
            "Capacity-screening solution; not a full-design optimality certificate"
            if stage == "capacity_screening_relaxation"
            else (
                "Two-stage engineering solution; fixed-capacity dispatch "
                "certified within the declared gap"
            )
            if bool(getattr(result, "proven_optimal", False))
            else "Feasible incumbent; fixed-capacity optimality not proven"
        ),
        "solver": (
            f"{result.solver_backend}; globally bounded design MILFP via Dinkelbach"
            if terms.get("global_design_integrality", False)
            and scenario in {"fixed_wind", "variable_scale"}
            else f"{result.solver_backend}; globally bounded design-and-dispatch MILP"
            if terms.get("global_design_integrality", False)
            else f"{result.solver_backend}; relaxed capacity screening + Dinkelbach"
            if scenario in {"fixed_wind", "variable_scale"}
            and stage == "capacity_screening_relaxation"
            else f"{result.solver_backend}; relaxed capacity screening"
            if stage == "capacity_screening_relaxation"
            else f"{result.solver_backend}; fixed-capacity engineering MILP"
        ),
        "solver_message": result.message,
        "solver_attempt_count": int(
            getattr(result, "solver_attempt_count", 1)
        ),
        "solver_attempt_profiles": str(
            getattr(result, "solver_attempt_profiles", result.solver_backend)
        ),
        "mip_gap": mip_gap,
        "fixed_capacity_dispatch_gap": mip_gap,
        "fixed_capacity_dispatch_optimality_certified": bool(
            getattr(result, "proven_optimal", False)
        ),
        "design_and_dispatch_optimality_certified": (
            global_design_certified
        ),
        "full_design_global_near_optimality_certified": (
            global_design_certified
        ),
        "exact_global_optimum_proven": bool(
            global_design_certified and mip_gap <= 1.0e-9
        ),
        # Legacy compatibility field. Here, True means a valid global
        # lower/upper-bound certificate within the declared gap, not zero gap.
        "full_design_global_optimality_proven": global_design_certified,
        "optimality_scope_note": (
            "Wind turbine count, electrolyzer module count, synthesis nameplate, "
            "storage capacities and all hourly decisions are optimized in one "
            "mixed-integer design-and-dispatch formulation."
            if global_design_certified
            else "The reported gap certifies the fixed-capacity hourly dispatch "
            "stage. Capacity selection is a documented two-stage screening, "
            "rounding and engineering-rerun procedure; a global mixed-integer "
            "design optimum is not claimed."
        ),
        "solver_objective_incumbent": float(
            getattr(result, "incumbent_objective", result.fun)
        ),
        "solver_objective_best_bound": float(
            getattr(result, "best_bound", np.nan)
        ),
        "fractional_LCOA_lower_bound_CNY_per_tNH3": float(
            getattr(result, "fractional_lcoa_lower_bound", np.nan)
        ),
        "fractional_LCOA_upper_bound_CNY_per_tNH3": float(
            getattr(result, "fractional_lcoa_upper_bound", np.nan)
        ),
        "fractional_global_relative_gap": float(
            getattr(result, "fractional_global_gap", np.nan)
        ),
        "fractional_relative_residual": float(
            getattr(result, "fractional_residual", np.nan)
        ),
        "allowed_mip_gap": float(
            getattr(result, "allowed_mip_gap", MIP_REL_GAP)
        ),
        "solve_seconds": solve_seconds,
        "annual_NH3_t": annual_nh3,
        "annual_NH3_target_t": terms.get("production_target_t", np.nan),
        "annual_total_cost_CNY": annual_total,
        "LCOA_CNY_per_tNH3": annual_total / annual_nh3,
        "curtailment_rate": float(curt.sum() / annual_generation),
        "annual_wind_generation_GWh": annual_generation / 1e6,
        "wind_full_load_hours": annual_generation / max(wind_kW, 1e-12),
        "annual_net_power_consumption_GWh": annual_consumption / 1e6,
        "annual_direct_process_load_GWh": annual_direct_process_load / 1e6,
        "wind_LCOE_CNY_per_kWh_generated": (
            annual_wind / max(annual_generation, 1e-12)
        ),
        "park_electricity_supply_cost_CNY_per_kWh_direct_process_load": (
            annual_electricity_supply_cost
            / max(annual_direct_process_load, 1e-12)
        ),
        "electricity_cost_allocation_basis": (
            "Annual wind plus battery cost allocated between electrolysis and "
            "ammonia synthesis in proportion to their direct hourly electricity use"
        ),
        "annual_H2_subsystem_cost_CNY": annual_h2_subsystem_cost,
        "levelized_H2_supply_cost_CNY_per_kgH2": (
            annual_h2_subsystem_cost
            / max(annual_h2_production * H2_KG_PER_NM3, 1e-12)
        ),
        "H2_subsystem_cost_CNY_per_tNH3": (
            annual_h2_subsystem_cost / max(annual_nh3, 1e-12)
        ),
        "H2_subsystem_share_of_LCOA": (
            annual_h2_subsystem_cost / max(annual_total, 1e-12)
        ),
        "annual_ammonia_conversion_cost_CNY": annual_ammonia_conversion_cost,
        "ammonia_conversion_cost_CNY_per_tNH3": (
            annual_ammonia_conversion_cost / max(annual_nh3, 1e-12)
        ),
        "economic_allocation_reconciliation_residual_CNY_per_y": (
            allocation_reconciliation_residual
        ),
        "wind_capacity_MW": wind_kW / 1000.0,
        "wind_turbines_10MW": wind_kW / WIND_TURBINE_KW,
        "electrolyzer_capacity_MW": el_kW / 1000.0,
        "electrolyzer_modules": el_kW / el["unit_power_kW"],
        "electrolyzer_installed_modules_solver_variable": float(
            x[ix.el_modules]
        ),
        "wind_turbines_solver_variable": float(x[ix.wind_units]),
        "electrolyzer_min_load_ratio_constraint": el["unit_min_load_ratio"],
        "electrolyzer_target_min_power_ratio": el[
            "target_min_power_ratio"
        ],
        "electrolyzer_min_load_basis": el["minimum_load_basis"],
        "electrolyzer_min_load_curve_basis": el["minimum_load_curve_basis"],
        "electrolyzer_effective_min_power_ratio": el[
            "effective_min_power_ratio"
        ],
        "electrolyzer_aspen_min_feasible_power_ratio": el[
            "aspen_min_feasible_power_ratio"
        ],
        "electrolyzer_target_min_power_represented_in_validated_envelope": el[
            "target_min_power_represented_in_validated_envelope"
        ],
        "electrolyzer_target_min_power_represented_in_active_curve": el[
            "target_min_power_represented_in_active_curve"
        ],
        "electrolyzer_target_min_power_feasible_in_aspen": el[
            "target_min_power_feasible_in_aspen"
        ],
        "electrolyzer_target_min_power_feasibility_status": el[
            "target_min_power_feasibility_status"
        ],
        "electrolyzer_ramp_up_ratio_per_h_constraint": el["ramp_up_ratio_per_h"],
        "electrolyzer_ramp_down_ratio_per_h_constraint": el["ramp_down_ratio_per_h"],
        "electrolyzer_reported_ramp_percent_per_min": el[
            "reported_ramp_percent_per_min"
        ],
        "electrolyzer_hourly_model_ramp_equivalent_percent_per_min": (
            float(el["ramp_up_ratio_per_h"]) * 100.0 / 60.0
        ),
        "electrolyzer_ramp_lower_bound_ratio_per_h": -float(el["ramp_down_ratio_per_h"]),
        "electrolyzer_ramp_upper_bound_ratio_per_h": float(el["ramp_up_ratio_per_h"]),
        "electrolyzer_cold_start_hours_constraint": el["cold_start_hours_model"],
        "electrolyzer_commitment_enforced": bool(
            terms["electrolyzer_commitment_enforced"]
        ),
        "electrolyzer_commitment_mode": str(
            terms.get(
                "electrolyzer_commitment_mode",
                "binary_aggregate_or_relaxed",
            )
        ),
        "global_design_integrality": bool(
            terms.get("global_design_integrality", False)
        ),
        "battery_charge_discharge_exclusivity_enforced": bool(
            terms["battery_charge_discharge_exclusivity_enforced"]
        ),
        "electrolyzer_transition_rows": terms["electrolyzer_transition_rows"],
        "electrolyzer_ramp_rows": terms["electrolyzer_ramp_rows"],
        "electrolyzer_on_hours": int(np.count_nonzero(el_on)),
        "electrolyzer_online_module_hours": int(np.sum(el_on)),
        "electrolyzer_max_online_modules": int(np.max(el_on)),
        "electrolyzer_start_count": int(np.sum(el_start)),
        "electrolyzer_stop_count": int(np.sum(el_stop)),
        "electrolyzer_capacity_factor": float(p_el.sum() / max(el_kW * n, 1e-12)),
        "electrolyzer_min_observed_running_load_ratio": (
            float(np.min(el_running_load)) if el_running_load.size else np.nan
        ),
        "electrolyzer_min_observed_running_power_ratio": (
            float(np.min(el_running_power_load))
            if el_running_power_load.size
            else np.nan
        ),
        "electrolyzer_max_observed_continuous_ramp_ratio_per_h": (
            float(
                np.max(np.abs(el_continuous_ramp[el_continuously_running]))
                / max(el_kW, 1e-12)
            )
            if np.any(el_continuously_running)
            else 0.0
        ),
        "electrolyzer_clustered_cold_start_max_violation_modules": float(
            np.max(clustered_cold_start_violation)
        ),
        "electrolyzer_clustered_min_down_max_violation_modules": float(
            np.max(clustered_cold_start_violation)
        ),
        "electrolyzer_min_off_hours_before_start": np.nan,
        "electrolyzer_binary_max_residual": float(
            max(
                np.max(np.abs(el_on_raw - np.rint(el_on_raw))),
                np.max(np.abs(el_start_raw - np.rint(el_start_raw))),
                np.max(np.abs(el_stop_raw - np.rint(el_stop_raw))),
            )
        ),
        "electrolyzer_integer_module_count_max_residual": float(
            max(
                np.max(np.abs(el_on_raw - np.rint(el_on_raw))),
                np.max(np.abs(el_start_raw - np.rint(el_start_raw))),
                np.max(np.abs(el_stop_raw - np.rint(el_stop_raw))),
            )
        ),
        "electrolyzer_start_stop_transition_max_residual": float(
            np.max(transition_residual)
        ),
        "electrolyzer_min_load_max_violation_kW": min_load_violation_kw,
        "electrolyzer_min_load_max_violation_Nm3_per_h": np.nan,
        "electrolyzer_off_state_max_power_kW": off_power_violation_kw,
        "electrolyzer_continuous_ramp_max_violation_kW": ramp_violation_kw,
        "battery_capacity_MWh": battery_kWh / 1000.0,
        "battery_power_MW": battery_kWh / BESS_DURATION_H / 1000.0,
        "h2_tank_capacity_Nm3": h2_tank_Nm3,
        "synthesis_nameplate_kt_per_y": plant_kt_y,
        "synthesis_capacity_option_count": int(plant_options.size),
        "synthesis_capacity_option_min_kt_per_y": float(
            np.min(plant_options)
        ),
        "synthesis_capacity_option_max_kt_per_y": float(
            np.max(plant_options)
        ),
        "synthesis_nameplate_power_MW": float(
            selected_plant_power_kw / 1000.0
        ),
        "synthesis_min_load_ratio_constraint": syn["min_load_ratio"],
        "synthesis_ramp_ratio_per_h_constraint": syn["ramp_ratio_per_h"],
        "synthesis_ramp_rows": terms["synthesis_ramp_rows"],
        "synthesis_reported_ramp_percent_per_min": syn[
            "reported_ramp_percent_per_min"
        ],
        "synthesis_min_observed_load_ratio": float(
            nh3_production_t_per_h.min()
            / max(selected_plant_nh3_t_per_h, 1e-12)
        ),
        "synthesis_max_observed_load_ratio": float(
            nh3_production_t_per_h.max()
            / max(selected_plant_nh3_t_per_h, 1e-12)
        ),
        "synthesis_max_abs_ramp_ratio_per_h": float(
            np.max(np.abs(synthesis_ramp_t_per_h))
            / max(selected_plant_nh3_t_per_h, 1e-12)
        ),
        "electrolyzer_energy_kWh_per_kgH2": el["system_energy_kWh_per_kgH2"],
        "synthesis_energy_kWh_per_kgNH3": syn["specific_power_kWh_per_kgNH3"],
        "actual_electrolyzer_energy_kWh_per_kgH2": float(
            p_el.sum()
            / max(annual_h2_production * H2_KG_PER_NM3, 1e-12)
        ),
        "actual_synthesis_energy_kWh_per_kgNH3": float(
            p_syn.sum() / max(annual_nh3 * 1000.0, 1e-12)
        ),
        "native_ammonia_product_state": syn["native_product_state"],
        "common_ammonia_delivery_phase": "liquid",
        "common_ammonia_delivery_phase_basis": "Model assumption, not a validated mixture flash",
        "common_ammonia_delivery_phase_validated": False,
        "NH3_denominator_basis": "Native outlet NH3 component mass; no downstream recovery correction",
        "common_ammonia_delivery_pressure_bar": (
            COMMON_NH3_PRODUCT_PRESSURE_BAR
        ),
        "common_ammonia_delivery_temperature_C": (
            COMMON_NH3_PRODUCT_TEMPERATURE_C
        ),
        "product_conditioning_electric_kWh_per_kgNH3": syn[
            "product_conditioning_electric_kWh_per_kgNH3"
        ],
        "annual_H2_production_Nm3": annual_h2_production,
        "annual_H2_use_Nm3": annual_h2_use,
        "annual_water_m3": annual_water,
        "wind_capex_CNY": wind_capex,
        "electrolyzer_capex_CNY": el_capex,
        "battery_capex_CNY": battery_capex,
        "h2_tank_capex_CNY": h2_capex,
        "synthesis_process_capex_CNY": selected_process["process_capex_CNY"],
        "synthesis_process_equipment_capex_CNY": selected_process[
            "process_equipment_capex_CNY"
        ],
        "process_land_capex_CNY": selected_process[
            "process_land_capex_CNY"
        ],
        "catalyst_capex_CNY": selected_process["catalyst_capex_CNY"],
        "annual_wind_CNY": annual_wind,
        "annual_wind_equipment_CNY": annual_wind_equipment,
        "annual_wind_land_cost_CNY": annual_wind_land,
        "annual_electrolyzer_CNY": annual_el,
        "annual_battery_CNY": annual_battery,
        "annual_h2_tank_CNY": annual_h2_tank,
        "annual_synthesis_process_CNY": selected_process["annual_process_CNY"],
        "annual_synthesis_process_equipment_CNY": selected_process[
            "annual_process_equipment_CNY"
        ],
        "annual_process_land_cost_CNY": selected_process[
            "annual_process_land_CNY"
        ],
        "annual_catalyst_CNY": selected_process["annual_catalyst_CNY"],
        "annual_stack_replacement_CNY": annual_stack,
        "annual_water_CNY": annual_water_cost,
        "annual_battery_tiebreaker_CNY": annual_battery_tiebreaker,
        "annual_electrolyzer_transition_tiebreaker_CNY": (
            annual_transition_tiebreaker
        ),
        "annual_surrogate_power_tiebreaker_CNY": (
            annual_surrogate_power_tiebreaker
        ),
        "annual_numerical_tiebreaker_total_CNY": (
            annual_battery_tiebreaker
            + annual_transition_tiebreaker
            + annual_surrogate_power_tiebreaker
        ),
        "max_simultaneous_battery_charge_discharge_kW": float(
            simultaneous_battery_kw.max()
        ),
        "raw_solver_max_simultaneous_battery_charge_discharge_kW": float(
            raw_simultaneous_battery_kw.max()
        ),
        "battery_netting_to_curtailment_GWh": battery_netting_energy_kwh / 1e6,
        "max_abs_power_balance_residual_kW": float(np.max(np.abs(power_residual))),
        "cyclic_battery_residual_kWh": float(bat_soc[-1] - 0.5 * battery_kWh),
        "cyclic_h2_residual_Nm3": float(h2_soc[-1] - 0.5 * h2_tank_Nm3),
        "wind_guard_fraction": wind_kW / MAX_WIND_KW,
        "electrolyzer_guard_fraction": el_kW / MAX_ELECTROLYZER_KW,
        "battery_guard_fraction": battery_kWh / MAX_BESS_KWH,
        "h2_tank_guard_fraction": h2_tank_Nm3 / MAX_H2_TANK_NM3,
        "wind_turbine_integer_residual": abs(
            wind_kW / WIND_TURBINE_KW - round(wind_kW / WIND_TURBINE_KW)
        ),
        "electrolyzer_module_integer_residual": abs(
            el_kW / el["unit_power_kW"] - round(el_kW / el["unit_power_kW"])
        ),
        "selected_plant_option_index": plant_index,
        "aspen_surrogate_coupling_enabled": bool(
            terms.get("surrogate_coupling_enabled", False)
        ),
        "aspen_surrogate_models": str(terms.get("surrogate_models", "")),
        "aspen_surrogate_iteration": int(
            terms.get("surrogate_iteration", 0)
        ),
        "aspen_surrogate_fixed_point_residual": float(
            terms.get("surrogate_fixed_point_residual", np.nan)
        ),
        "aspen_surrogate_direct_milp": bool(
            terms.get("surrogate_direct_milp", False)
        ),
        "aspen_surrogate_pwl_segments": int(
            terms.get("surrogate_pwl_segments", 0)
        ),
        "aspen_surrogate_model_max_relative_error": float(
            terms.get("surrogate_model_max_relative_error", np.nan)
        ),
        "aspen_surrogate_source_sha256": str(
            terms.get("surrogate_source_sha256", "")
        ),
        "dispatch_row_count": int(n),
        "land_only_active": bool(terms.get("land_only_active", False)),
        "land_contract_sha256": str(
            terms.get("land_contract_sha256", "")
        ),
        "land_hard_constraint_passed": bool(
            land_audit.get("land_hard_constraint_passed", False)
        ),
        "pv_capacity_MW": float(land_audit.get("pv_capacity_MW", 0.0)),
        "wind_equipment_and_land_cost_separated": bool(
            terms.get("land_only_active", False)
        ),
        "process_equipment_and_land_cost_separated": bool(
            terms.get("land_only_active", False)
        ),
        **land_audit,
    }

    battery_soc_percent = (
        100.0 * bat_soc / battery_kWh
        if battery_kWh > 1e-9
        else np.zeros_like(bat_soc)
    )
    h2_soc_percent = (
        100.0 * h2_soc / h2_tank_Nm3
        if h2_tank_Nm3 > 1e-9
        else np.zeros_like(h2_soc)
    )
    dispatch = pd.DataFrame(
        {
            "hour": np.arange(1, n + 1),
            "wind_generation_kW": wind_generation,
            "electrolyzer_power_kW": p_el,
            "electrolyzer_on": el_on,
            "electrolyzer_online_modules": el_on,
            "electrolyzer_operating": (el_on > 0).astype(int),
            "electrolyzer_start": el_start,
            "electrolyzer_modules_started": el_start,
            "electrolyzer_stop": el_stop,
            "electrolyzer_modules_stopped": el_stop,
            "synthesis_power_kW": p_syn,
            "battery_charge_kW": bat_ch,
            "battery_discharge_kW": bat_dis,
            "battery_SOC_kWh": bat_soc,
            "battery_SOC_percent": battery_soc_percent,
            "H2_storage_Nm3": h2_soc,
            "H2_SOC_percent": h2_soc_percent,
            "curtailment_kW": curt,
            "NH3_production_t_per_h": nh3_production_t_per_h,
            "H2_production_Nm3_per_h": h2_production_nm3_per_h,
            "H2_consumption_Nm3_per_h": h2_consumption_nm3_per_h,
            "water_consumption_m3_per_h": water_m3_per_h,
            "electrolyzer_H2_Nm3_per_kWh": np.divide(
                h2_production_nm3_per_h,
                p_el,
                out=np.full(
                    n,
                    float(el["h2_prod_Nm3_per_h"])
                    / float(el["unit_power_kW"]),
                ),
                where=p_el > 1e-10,
            ),
            "electrolyzer_water_m3_per_kWh": np.divide(
                water_m3_per_h,
                p_el,
                out=np.full(
                    n,
                    float(el["water_m3_per_h"])
                    / float(el["unit_power_kW"]),
                ),
                where=p_el > 1e-10,
            ),
            "synthesis_NH3_t_per_kWh": np.divide(
                nh3_production_t_per_h,
                p_syn,
                out=np.full(
                    n,
                    float(syn["rated_NH3_kg_per_h"])
                    / 1000.0
                    / float(syn["rated_power_kW"]),
                ),
                where=p_syn > 1e-10,
            ),
            "synthesis_H2_Nm3_per_kWh": np.divide(
                h2_consumption_nm3_per_h,
                p_syn,
                out=np.full(
                    n,
                    float(syn["h2_consumption_Nm3_per_h"])
                    / float(syn["rated_power_kW"]),
                ),
                where=p_syn > 1e-10,
            ),
            "power_balance_residual_kW": power_residual,
        }
    )
    return summary, dispatch

def _fill_short_circular_off_runs(
    raw_schedule: np.ndarray,
    installed_modules: int,
    minimum_down_hours: int,
) -> np.ndarray:
    """Keep exchangeable modules online through off-runs shorter than min-down."""
    schedule = np.clip(
        np.rint(raw_schedule).astype(int),
        0,
        installed_modules,
    )
    if minimum_down_hours <= 1 or installed_modules <= 0:
        return schedule

    repaired = np.zeros_like(schedule)
    n = len(schedule)
    for module_index in range(installed_modules):
        state = (schedule > module_index).astype(int)
        if state.any() and not state.all():
            off_run_starts = np.flatnonzero(
                (state == 0) & (np.roll(state, 1) == 1)
            )
            for start in off_run_starts:
                run: list[int] = []
                index = int(start)
                while state[index] == 0 and len(run) < n:
                    run.append(index)
                    index = (index + 1) % n
                if len(run) < minimum_down_hours:
                    state[run] = 1
        repaired += state

    starts = np.maximum(repaired - np.roll(repaired, 1), 0)
    for t in range(n):
        recent_starts = sum(
            int(starts[(t - lag) % n])
            for lag in range(minimum_down_hours)
        )
        if (
            recent_starts
            + int(repaired[(t - minimum_down_hours) % n])
            > installed_modules
        ):
            raise RuntimeError("Cold-start fill repair produced an invalid schedule")
    return repaired

def _commitment_schedule_candidates(
    el: dict,
    fixed: FixedCapacities,
    electrolyzer_on_start: np.ndarray | None,
    electrolyzer_power_start: np.ndarray | None,
) -> list[tuple[str, np.ndarray]]:
    """Build a small, deterministic set of physically valid annual schedules."""
    n = (
        len(electrolyzer_power_start)
        if electrolyzer_power_start is not None
        else len(electrolyzer_on_start)
    )
    installed_modules = int(
        round(float(fixed.electrolyzer_kW) / float(el["unit_power_kW"]))
    )
    candidates: list[tuple[str, np.ndarray]] = []
    if electrolyzer_on_start is not None:
        relaxed_online = np.asarray(electrolyzer_on_start, dtype=float)
        for rounding_name, rounding in (
            ("round", np.rint),
            ("floor", np.floor),
            ("ceil", np.ceil),
        ):
            candidates.append(
                (
                    f"{rounding_name}_relaxed_online_modules",
                    np.clip(
                        rounding(relaxed_online).astype(int),
                        0,
                        installed_modules,
                    ),
                )
            )
        positive_online = relaxed_online[relaxed_online > 1e-9]
        if positive_online.size:
            for quantile in (0.25, 0.50, 0.75, 0.90):
                constant_modules = int(
                    np.clip(
                        np.rint(np.quantile(positive_online, quantile)),
                        1,
                        installed_modules,
                    )
                )
                candidates.append(
                    (
                        f"constant_relaxed_q{int(100 * quantile):02d}",
                        np.full(n, constant_modules, dtype=int),
                    )
                )
    if electrolyzer_power_start is not None:
        relaxed_power = np.asarray(electrolyzer_power_start, dtype=float)
        load_fraction = np.divide(
            relaxed_power,
            max(float(fixed.electrolyzer_kW), 1e-12),
        )
        base_online_modules = np.clip(
            np.ceil(
                relaxed_power / max(float(el["unit_power_kW"]), 1e-12)
                - 1e-10
            ).astype(int),
            0,
            installed_modules,
        )
        candidates.append(
            ("module_count_from_relaxed_power", base_online_modules)
        )
        minimum_load = float(el["unit_min_load_ratio"])
        threshold_fractions = (
            0.25 * minimum_load,
            0.50 * minimum_load,
            0.75 * minimum_load,
            1.00 * minimum_load,
            1.25 * minimum_load,
        )
        for threshold in threshold_fractions:
            candidates.append(
                (
                    f"relaxed_power_ge_{threshold:.6f}",
                    np.where(
                        load_fraction >= threshold,
                        base_online_modules,
                        0,
                    ).astype(int),
                )
            )
    candidates.append(
        (
            "all_installed_modules_online",
            np.full(n, installed_modules, dtype=int),
        )
    )

    # Repair positive count changes against the standard clustered minimum-
    # down inequality. This is deterministic and only reduces online counts,
    # so it cannot create an off-state power violation in a subsequently
    # reoptimized fixed-schedule LP.
    cold_start_h = int(el["cold_start_hours_model"])
    if cold_start_h >= 1:
        original_candidates = list(candidates)
        for label, raw_schedule in original_candidates:
            candidates.append(
                (
                    f"{label}_cold_filled",
                    _fill_short_circular_off_runs(
                        raw_schedule,
                        installed_modules,
                        cold_start_h,
                    ),
                )
            )
        for label, raw_schedule in original_candidates:
            repaired = np.clip(
                np.rint(raw_schedule).astype(int),
                0,
                installed_modules,
            )
            for _ in range(max(4, 2 * cold_start_h + 2)):
                changed = False
                starts = np.maximum(repaired - np.roll(repaired, 1), 0)
                for t in range(n):
                    previous = int(repaired[(t - 1) % n])
                    requested_start = int(starts[t])
                    if requested_start == 0:
                        continue
                    earlier_starts = sum(
                        int(starts[(t - lag) % n])
                        for lag in range(1, cold_start_h)
                    )
                    maximum_start = max(
                        int(
                            installed_modules
                            - int(repaired[(t - cold_start_h) % n])
                            - earlier_starts
                        ),
                        0,
                    )
                    if requested_start > maximum_start:
                        repaired[t] = previous + maximum_start
                        changed = True
                        starts = np.maximum(
                            repaired - np.roll(repaired, 1),
                            0,
                        )
                if not changed:
                    break
            candidates.append((f"{label}_cold_repaired", repaired))

    unique: list[tuple[str, np.ndarray]] = []
    seen: set[bytes] = set()
    for label, schedule in candidates:
        bounded = np.clip(
            np.rint(schedule).astype(int),
            0,
            installed_modules,
        )
        if not bounded.any():
            continue
        signature = bounded.tobytes()
        if signature in seen:
            continue
        seen.add(signature)
        unique.append((label, bounded))
    return unique

def _solve_certified_fixed_capacity_commitment(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    scenario: str,
    fixed: FixedCapacities,
    *,
    production_target_t: float | None,
    lcoa_parameter: float,
    electrolyzer_on_start: np.ndarray | None,
    electrolyzer_power_start: np.ndarray | None,
    synthesis_power_start: np.ndarray | None,
    curtailment_rate_limit: float | None,
    wind_utilization_target: float | None,
    mip_rel_gap_override: float | None,
    time_limit_override_s: float | None,
    plant_options_kt_per_y: np.ndarray | list[float] | None,
    allow_engineering_feasible_gap: bool = False,
) -> tuple[SimpleNamespace, Index, dict, float, dict]:
    """Find a feasible schedule and certify it against the exact MILP LP bound."""
    selected_gap = (
        float(mip_rel_gap_override)
        if mip_rel_gap_override is not None
        else MIP_REL_GAP
    )
    c, integrality, bounds, constraints, ix, terms = build_problem(
        el,
        syn,
        wind_per_kw,
        scenario,
        lcoa_parameter=lcoa_parameter,
        fixed=fixed,
        enforce_electrolyzer_commitment=True,
        enforce_battery_exclusivity=False,
        production_target_t=production_target_t,
        curtailment_rate_limit=curtailment_rate_limit,
        wind_utilization_target=wind_utilization_target,
        plant_options_kt_per_y=plant_options_kt_per_y,
    )
    started = time.time()
    engineering_time_limit_s = min(
        float(time_limit_override_s)
        if time_limit_override_s is not None
        else 120.0,
        120.0,
    )
    lower_bound_result = solve_matrix_problem(
        c,
        np.zeros_like(integrality),
        bounds,
        constraints,
        indicator_constraints=terms["indicator_constraints"],
        mip_start=None,
        accept_feasible=False,
        mip_rel_gap=selected_gap,
        time_limit_s=min(
            float(time_limit_override_s)
            if time_limit_override_s is not None
            else MIP_TIME_LIMIT_S,
            120.0 if allow_engineering_feasible_gap else 300.0,
        ),
    )
    if not lower_bound_result.success:
        raise RuntimeError(
            f"{scenario} {syn['syn_id']}+{el['tech_id']} LP relaxation failed"
        )
    lower_bound = float(lower_bound_result.fun)

    relaxed_online = np.asarray(
        lower_bound_result.x[ix.el_on : ix.el_on + ix.n],
        dtype=float,
    )
    relaxed_power = np.asarray(
        lower_bound_result.x[ix.p_el : ix.p_el + ix.n],
        dtype=float,
    )
    candidate_inputs: list[
        tuple[str, np.ndarray | None, np.ndarray | None]
    ] = []
    if electrolyzer_on_start is not None or electrolyzer_power_start is not None:
        candidate_inputs.append(
            (
                "capacity_screen",
                electrolyzer_on_start,
                electrolyzer_power_start,
            )
        )
    candidate_inputs.append(
        (
            "direct_proxy_lp",
            relaxed_online,
            relaxed_power,
        )
    )

    commitment_candidates: list[tuple[str, np.ndarray]] = []
    seen_candidate_schedules: set[bytes] = set()
    for source_label, online_values, power_values in candidate_inputs:
        for label, schedule in _commitment_schedule_candidates(
            el,
            fixed,
            online_values,
            power_values,
        ):
            signature = np.asarray(schedule, dtype=int).tobytes()
            if signature in seen_candidate_schedules:
                continue
            seen_candidate_schedules.add(signature)
            commitment_candidates.append(
                (f"{source_label}:{label}", schedule)
            )

    candidate_records: list[dict] = []
    best_result: SimpleNamespace | None = None
    best_terms: dict | None = None
    best_label = ""
    for label, schedule in commitment_candidates:
        # Preserve the established result when the capacity-screen schedule
        # already yields a feasible certified incumbent. The direct-proxy LP
        # schedule is a deterministic fallback for routes where the older
        # linear-screen schedule cannot be repaired.
        if label.startswith("direct_proxy_lp:") and best_result is not None:
            break
        try:
            candidate, candidate_ix, candidate_terms, _ = solve_problem(
                el,
                syn,
                wind_per_kw,
                scenario,
                lcoa_parameter=lcoa_parameter,
                fixed=fixed,
                production_target_t=production_target_t,
                enforce_electrolyzer_commitment=True,
                enforce_battery_exclusivity=False,
                electrolyzer_on_fixed=schedule,
                curtailment_rate_limit=curtailment_rate_limit,
                wind_utilization_target=wind_utilization_target,
                accept_feasible=allow_engineering_feasible_gap,
                mip_rel_gap_override=1.0e-6,
                time_limit_override_s=(
                    engineering_time_limit_s
                    if allow_engineering_feasible_gap
                    else 180.0
                ),
                plant_options_kt_per_y=plant_options_kt_per_y,
            )
        except RuntimeError as exc:
            candidate_records.append(
                {
                    "label": label,
                    "feasible": False,
                    "online_hours": int(schedule.sum()),
                    "objective_CNY": np.nan,
                    "error": str(exc),
                }
            )
            continue
        if candidate_ix.size != ix.size:
            raise RuntimeError("Commitment candidate changed the model dimension")
        candidate_records.append(
            {
                "label": label,
                "feasible": True,
                "online_hours": int(schedule.sum()),
                "objective_CNY": float(candidate.fun),
                "error": "",
            }
        )
        if best_result is None or float(candidate.fun) < float(best_result.fun):
            best_result = candidate
            best_terms = candidate_terms
            best_label = label
        if allow_engineering_feasible_gap:
            break
        candidate_gap = max(
            0.0,
            (float(candidate.fun) - lower_bound)
            / max(abs(float(candidate.fun)), 1.0),
        )
        if candidate_gap <= min(selected_gap, 5.0e-3) + 1.0e-7:
            break

    if best_result is None or best_terms is None:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        failure_audit_path = (
            AUDIT_DIR
            / (
                f"failed_commitment_candidates_{scenario}_"
                f"{syn['syn_id']}_{el['tech_id']}.json"
            )
        )
        failure_audit_path.write_text(
            json.dumps(
                {
                    "scenario": scenario,
                    "route": f"{syn['syn_id']}+{el['tech_id']}",
                    "fixed_capacities": {
                        "wind_kW": float(fixed.wind_kW),
                        "electrolyzer_kW": float(fixed.electrolyzer_kW),
                        "plant_option_index": int(
                            fixed.plant_option_index
                        ),
                    },
                    "candidate_count": len(candidate_records),
                    "candidates": candidate_records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise RuntimeError(
            f"{scenario} {syn['syn_id']}+{el['tech_id']} has no feasible "
            "repaired annual commitment schedule; inspect "
            f"{failure_audit_path}"
        )

    def certified_gap(objective: float, valid_lower_bound: float) -> float:
        return max(
            0.0,
            (float(objective) - float(valid_lower_bound))
            / max(abs(float(objective)), 1.0),
        )

    certificate_lower_bound = lower_bound
    full_milp_best_bound = np.nan
    full_milp_solver_gap = np.nan
    gap = certified_gap(float(best_result.fun), certificate_lower_bound)
    final_result = best_result
    final_terms = best_terms
    certificate_method = "fixed_schedule_incumbent_plus_exact_LP_bound"

    if gap > selected_gap + 1e-7 and not allow_engineering_feasible_gap:
        final_result, final_ix, final_terms, _ = solve_problem(
            el,
            syn,
            wind_per_kw,
            scenario,
            lcoa_parameter=lcoa_parameter,
            fixed=fixed,
            production_target_t=production_target_t,
            enforce_electrolyzer_commitment=True,
            enforce_battery_exclusivity=False,
            solution_start=np.asarray(best_result.x, dtype=float),
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=wind_utilization_target,
            accept_feasible=True,
            mip_rel_gap_override=selected_gap,
            time_limit_override_s=time_limit_override_s,
            plant_options_kt_per_y=plant_options_kt_per_y,
        )
        if final_ix.size != ix.size:
            raise RuntimeError("Full commitment solve changed the model dimension")
        full_milp_best_bound = float(
            getattr(final_result, "best_bound", np.nan)
        )
        full_milp_solver_gap = float(
            getattr(final_result, "mip_gap", np.nan)
        )
        if np.isfinite(full_milp_best_bound):
            # The full-MILP bound remains valid after fixing the incumbent's
            # battery direction because that rerun only adds constraints.
            certificate_lower_bound = max(
                certificate_lower_bound,
                full_milp_best_bound,
            )
        gap = certified_gap(
            float(final_result.fun),
            certificate_lower_bound,
        )
        certificate_method = "full_MILP_with_feasible_start_and_valid_lower_bound"

    battery_net_soc_change = (
        BESS_CHARGE_EFFICIENCY
        * np.asarray(
            final_result.x[ix.bat_ch : ix.bat_ch + ix.n],
            dtype=float,
        )
        - np.asarray(
            final_result.x[ix.bat_dis : ix.bat_dis + ix.n],
            dtype=float,
        )
        / BESS_DISCHARGE_EFFICIENCY
    )
    battery_mode = (battery_net_soc_change >= 0.0).astype(int)
    exact_online_schedule = np.rint(
        final_result.x[ix.el_on : ix.el_on + ix.n]
    ).astype(int)
    final_result, final_ix, final_terms, _ = solve_problem(
        el,
        syn,
        wind_per_kw,
        scenario,
        lcoa_parameter=lcoa_parameter,
        fixed=fixed,
        production_target_t=production_target_t,
        enforce_electrolyzer_commitment=True,
        enforce_battery_exclusivity=True,
        electrolyzer_on_fixed=exact_online_schedule,
        battery_charge_mode_fixed=battery_mode,
        curtailment_rate_limit=curtailment_rate_limit,
        wind_utilization_target=wind_utilization_target,
        accept_feasible=allow_engineering_feasible_gap,
        mip_rel_gap_override=1.0e-6,
        time_limit_override_s=(
            engineering_time_limit_s
            if allow_engineering_feasible_gap
            else 300.0
        ),
        plant_options_kt_per_y=plant_options_kt_per_y,
    )
    if final_ix.size != ix.size:
        raise RuntimeError("Battery-direction rerun changed the model dimension")
    ix = final_ix
    gap = certified_gap(
        float(final_result.fun),
        certificate_lower_bound,
    )
    certificate_method = (
        "engineering_feasible_fixed_schedule_plus_exact_battery_direction"
        if allow_engineering_feasible_gap
        else certificate_method + "_plus_fixed_exact_battery_direction"
    )

    if (
        not np.isfinite(gap)
        or (
            gap > selected_gap + 1e-7
            and not allow_engineering_feasible_gap
        )
    ):
        raise RuntimeError(
            f"{scenario} {syn['syn_id']}+{el['tech_id']} feasible schedule "
            f"could not be certified within {selected_gap:.2%}; "
            f"best valid gap={gap:.2%}"
        )

    final_result.mip_gap = float(gap)
    final_result.best_bound = float(certificate_lower_bound)
    final_result.incumbent_objective = float(final_result.fun)
    final_result.allowed_mip_gap = float(selected_gap)
    final_result.proven_optimal = bool(gap <= selected_gap + 1e-7)
    final_result.success = True
    final_result.status = 0
    final_result.message = (
        (
            f"Engineering-feasible hourly commitment retained at "
            f"{gap:.4%} actual gap by {certificate_method}"
        )
        if allow_engineering_feasible_gap
        else (
            f"Feasible hourly commitment certified within {gap:.4%} by "
            f"{certificate_method}"
        )
    )
    final_result.solver_backend = (
        f"{getattr(final_result, 'solver_backend', 'Gurobi 10.0.1')}; "
        + (
            "engineering-feasible fixed schedule plus exact battery direction"
            if allow_engineering_feasible_gap
            else "schedule incumbent plus exact LP-bound certificate"
        )
    )
    elapsed = time.time() - started
    certificate = {
        "commitment_certificate_method": certificate_method,
        "commitment_LP_lower_bound_CNY": lower_bound,
        "commitment_full_MILP_best_bound_CNY": full_milp_best_bound,
        "commitment_full_MILP_solver_gap": full_milp_solver_gap,
        "commitment_effective_lower_bound_CNY": certificate_lower_bound,
        "commitment_certified_gap": float(gap),
        "commitment_candidate_count": len(candidate_records),
        "commitment_feasible_candidate_count": int(
            sum(bool(item["feasible"]) for item in candidate_records)
        ),
        "commitment_best_candidate": best_label,
        "commitment_candidates": candidate_records,
    }
    return final_result, ix, final_terms, elapsed, certificate

def solve_surrogate_coupled_fixed_capacity(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    scenario: str,
    fixed: FixedCapacities,
    *,
    production_target_t: float | None,
    lcoa_parameter: float = 0.0,
    electrolyzer_on_start: np.ndarray | None = None,
    electrolyzer_power_start: np.ndarray | None = None,
    synthesis_power_start: np.ndarray | None = None,
    curtailment_rate_limit: float | None = None,
    wind_utilization_target: float | None = None,
    accept_feasible: bool = False,
    mip_rel_gap_override: float | None = None,
    time_limit_override_s: float | None = None,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
    allow_engineering_feasible_gap: bool = False,
) -> tuple[SimpleNamespace, Index, dict, float, list[dict]]:
    """Solve a fixed-capacity MILP with the Aspen proxy equations embedded."""
    n = len(wind_per_kw)
    if (electrolyzer_power_start is None) != (synthesis_power_start is None):
        raise ValueError(
            "electrolyzer_power_start and synthesis_power_start must be "
            "provided together"
        )
    if electrolyzer_power_start is not None:
        p_el_start = np.asarray(electrolyzer_power_start, dtype=float)
        p_syn_start = np.asarray(synthesis_power_start, dtype=float)
        if p_el_start.size != n or p_syn_start.size != n:
            raise ValueError(
                "Surrogate power starts must contain one value per hour"
            )
    if n == N_HOURS:
        result, ix, terms, seconds, certificate = (
            _solve_certified_fixed_capacity_commitment(
                el,
                syn,
                wind_per_kw,
                scenario,
                fixed,
                production_target_t=production_target_t,
                lcoa_parameter=lcoa_parameter,
                electrolyzer_on_start=electrolyzer_on_start,
                electrolyzer_power_start=electrolyzer_power_start,
                synthesis_power_start=synthesis_power_start,
                curtailment_rate_limit=curtailment_rate_limit,
                wind_utilization_target=wind_utilization_target,
                mip_rel_gap_override=mip_rel_gap_override,
                time_limit_override_s=time_limit_override_s,
                plant_options_kt_per_y=plant_options_kt_per_y,
                allow_engineering_feasible_gap=(
                    allow_engineering_feasible_gap
                ),
            )
        )
    else:
        result, ix, terms, seconds = solve_problem(
            el,
            syn,
            wind_per_kw,
            scenario,
            lcoa_parameter=lcoa_parameter,
            fixed=fixed,
            production_target_t=production_target_t,
            enforce_electrolyzer_commitment=True,
            enforce_battery_exclusivity=False,
            electrolyzer_on_start=electrolyzer_on_start,
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=wind_utilization_target,
            accept_feasible=accept_feasible,
            mip_rel_gap_override=mip_rel_gap_override,
            time_limit_override_s=time_limit_override_s,
            plant_options_kt_per_y=plant_options_kt_per_y,
        )
        battery_net_soc_change = (
            BESS_CHARGE_EFFICIENCY
            * np.asarray(
                result.x[ix.bat_ch : ix.bat_ch + ix.n],
                dtype=float,
            )
            - np.asarray(
                result.x[ix.bat_dis : ix.bat_dis + ix.n],
                dtype=float,
            )
            / BESS_DISCHARGE_EFFICIENCY
        )
        battery_mode = (battery_net_soc_change >= 0.0).astype(int)
        exact_online_schedule = np.rint(
            result.x[ix.el_on : ix.el_on + ix.n]
        ).astype(int)
        exact_result, exact_ix, exact_terms, exact_seconds = solve_problem(
            el,
            syn,
            wind_per_kw,
            scenario,
            lcoa_parameter=lcoa_parameter,
            fixed=fixed,
            production_target_t=production_target_t,
            enforce_electrolyzer_commitment=True,
            enforce_battery_exclusivity=True,
            electrolyzer_on_fixed=exact_online_schedule,
            battery_charge_mode_fixed=battery_mode,
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=wind_utilization_target,
            mip_rel_gap_override=1.0e-6,
            time_limit_override_s=time_limit_override_s,
            plant_options_kt_per_y=plant_options_kt_per_y,
        )
        result, ix, terms = exact_result, exact_ix, exact_terms
        seconds += exact_seconds
        certificate = {
            "commitment_certificate_method": "direct_short_horizon_MILP",
            "commitment_LP_lower_bound_CNY": np.nan,
            "commitment_certified_gap": float(
                getattr(result, "mip_gap", np.nan)
            ),
            "commitment_candidate_count": 0,
            "commitment_feasible_candidate_count": 0,
            "commitment_best_candidate": "",
            "commitment_candidates": [],
        }
    active_models = [str(syn["syn_id"])]
    if el["tech_id"] == "ALK":
        active_models.insert(0, "ALK")
    terms["surrogate_coupling_enabled"] = True
    terms["surrogate_models"] = ";".join(active_models)
    terms["surrogate_iteration"] = 1
    terms["surrogate_fixed_point_residual"] = 0.0
    model_error = float(
        terms.get("surrogate_model_max_relative_error", np.nan)
    )
    history = [
        {
            "scenario": scenario,
            "route": f"{syn['syn_id']}+{el['tech_id']}",
            "surrogate_iteration": 1,
            "surrogate_fixed_point_residual": 0.0,
            "surrogate_model_max_relative_error": model_error,
            "surrogate_direct_milp": True,
            "solve_seconds": seconds,
            "mip_gap": float(getattr(result, "mip_gap", np.nan)),
            **{
                key: value
                for key, value in certificate.items()
                if key != "commitment_candidates"
            },
            "converged": (
                np.isfinite(model_error)
                and model_error <= SURROGATE_PWL_MAX_REL_ERROR
            ),
        }
    ]
    if not history[0]["converged"]:
        raise RuntimeError(
            "Aspen surrogate direct-MILP validation failed for "
            f"{syn['syn_id']}+{el['tech_id']}: error={model_error:.6g}"
        )
    return result, ix, terms, seconds, history

def _solve_fixed_wind_plant_dinkelbach(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    *,
    plant_option_index: int | None = None,
    fixed: FixedCapacities | None = None,
    initial_lcoa: float = 9000.0,
    enforce_electrolyzer_commitment: bool = True,
    electrolyzer_on_start: np.ndarray | None = None,
    electrolyzer_on_fixed: np.ndarray | None = None,
    battery_charge_mode_fixed: np.ndarray | None = None,
    dispatch_coefficients: dict | None = None,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
):
    lcoa = initial_lcoa
    total_seconds = 0.0
    history = []
    final = None
    on_start = electrolyzer_on_start
    for iteration in range(1, DINKELBACH_MAX_ITER + 1):
        result, ix, terms, seconds = solve_problem(
            el,
            syn,
            wind_per_kw,
            "fixed_wind",
            lcoa_parameter=lcoa,
            fixed=fixed,
            plant_option_index=plant_option_index,
            enforce_electrolyzer_commitment=(
                enforce_electrolyzer_commitment
                or electrolyzer_on_fixed is not None
            ),
            enforce_battery_exclusivity=(
                battery_charge_mode_fixed is not None
            ),
            electrolyzer_on_start=on_start,
            electrolyzer_on_fixed=electrolyzer_on_fixed,
            battery_charge_mode_fixed=battery_charge_mode_fixed,
            dispatch_coefficients=dispatch_coefficients,
            plant_options_kt_per_y=plant_options_kt_per_y,
        )
        if enforce_electrolyzer_commitment or electrolyzer_on_fixed is not None:
            on_start = np.rint(
                result.x[ix.el_on : ix.el_on + ix.n]
            ).astype(int)
        total_seconds += seconds
        summary, _ = extract_result(
            result,
            ix,
            terms,
            el,
            syn,
            wind_per_kw,
            "fixed_wind",
            "integer_engineering_rerun"
            if enforce_electrolyzer_commitment
            else "continuous_sizing_relaxation",
            seconds,
        )
        updated = summary["LCOA_CNY_per_tNH3"]
        residual = summary["annual_total_cost_CNY"] - lcoa * summary["annual_NH3_t"]
        relative = abs(residual) / max(summary["annual_total_cost_CNY"], 1.0)
        history.append(
            {
                "iteration": iteration,
                "input_LCOA_CNY_per_tNH3": lcoa,
                "output_LCOA_CNY_per_tNH3": updated,
                "relative_residual": relative,
                "solve_seconds": seconds,
            }
        )
        final = (result, ix, terms, updated)
        dinkelbach_tolerance = (
            DINKELBACH_TOL
            if enforce_electrolyzer_commitment
            and electrolyzer_on_fixed is None
            else SIZING_MIP_REL_GAP
        )
        if relative <= dinkelbach_tolerance:
            break
        lcoa = updated
    else:
        raise RuntimeError(
            f"Dinkelbach failed for {syn['syn_id']}+{el['tech_id']} "
            f"at plant option {plant_option_index}"
        )
    final[0].fractional_lcoa = float(final[3])
    return final[0], final[1], final[2], total_seconds, final[3], history

def solve_global_fixed_production_design(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    *,
    production_target_t: float | None = None,
    plant_option_index: int | None = None,
    scenario: str = "fixed_300kt",
    curtailment_rate_limit: float | None = None,
    wind_utilization_target: float | None = None,
    mip_rel_gap: float = MIP_REL_GAP,
    time_limit_s: float = MIP_TIME_LIMIT_S,
    solution_start: np.ndarray | None = None,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
    wind_capacity_min_kw: float | None = None,
    wind_capacity_max_kw: float | None = None,
    enforce_hourly_commitment: bool = True,
    enforce_battery_exclusivity: bool = True,
    continuous_design_relaxation: bool = False,
) -> tuple[SimpleNamespace, Index, dict, float]:
    """Globally optimize discrete design and hourly dispatch for fixed output."""
    result, ix, terms, seconds = solve_problem(
        el,
        syn,
        wind_per_kw,
        scenario,
        plant_option_index=plant_option_index,
        enforce_electrolyzer_commitment=enforce_hourly_commitment,
        enforce_battery_exclusivity=enforce_battery_exclusivity,
        production_target_t=production_target_t,
        solution_start=solution_start,
        curtailment_rate_limit=curtailment_rate_limit,
        wind_utilization_target=wind_utilization_target,
        mip_rel_gap_override=mip_rel_gap,
        time_limit_override_s=time_limit_s,
        global_design_integrality=not continuous_design_relaxation,
        continuous_design_relaxation=continuous_design_relaxation,
        plant_options_kt_per_y=plant_options_kt_per_y,
        wind_capacity_min_kw=wind_capacity_min_kw,
        wind_capacity_max_kw=wind_capacity_max_kw,
    )
    if not bool(getattr(result, "proven_optimal", False)):
        raise RuntimeError(
            "Design-global fixed-production solve did not reach the declared "
            f"{100.0 * mip_rel_gap:.3g}% gap"
        )
    result.fractional_global_certified = True
    result.design_global_certificate = True
    return result, ix, terms, seconds

def solve_global_fractional_design(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    *,
    scenario: str,
    fixed: FixedCapacities | None = None,
    plant_option_index: int | None = None,
    initial_lcoa: float = 5000.0,
    production_target_t: float | None = None,
    curtailment_rate_limit: float | None = None,
    mip_rel_gap: float = MIP_REL_GAP,
    dinkelbach_tolerance: float = DINKELBACH_TOL,
    time_limit_s: float = MIP_TIME_LIMIT_S,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
    wind_capacity_min_kw: float | None = None,
    wind_capacity_max_kw: float | None = None,
    enforce_hourly_commitment: bool = True,
    enforce_battery_exclusivity: bool = True,
    continuous_design_relaxation: bool = False,
    solution_start: np.ndarray | None = None,
    electrolyzer_on_start: np.ndarray | None = None,
) -> tuple[SimpleNamespace, Index, dict, float, list[dict]]:
    """Solve the design-level MILFP through globally solved Dinkelbach MILPs."""
    if scenario not in {"fixed_wind", "variable_scale"}:
        raise ValueError(
            "Global fractional design requires fixed_wind or variable_scale"
        )
    lcoa = float(initial_lcoa)
    total_seconds = 0.0
    current_solution_start = solution_start
    history: list[dict] = []
    final: tuple[SimpleNamespace, Index, dict] | None = None

    for iteration in range(1, DINKELBACH_MAX_ITER + 1):
        result, ix, terms, seconds = solve_problem(
            el,
            syn,
            wind_per_kw,
            scenario,
            lcoa_parameter=lcoa,
            fixed=fixed,
            plant_option_index=plant_option_index,
            enforce_electrolyzer_commitment=enforce_hourly_commitment,
            enforce_battery_exclusivity=enforce_battery_exclusivity,
            production_target_t=production_target_t,
            electrolyzer_on_start=electrolyzer_on_start,
            solution_start=current_solution_start,
            curtailment_rate_limit=curtailment_rate_limit,
            mip_rel_gap_override=mip_rel_gap,
            time_limit_override_s=time_limit_s,
            global_design_integrality=not continuous_design_relaxation,
            continuous_design_relaxation=continuous_design_relaxation,
            plant_options_kt_per_y=plant_options_kt_per_y,
            wind_capacity_min_kw=wind_capacity_min_kw,
            wind_capacity_max_kw=wind_capacity_max_kw,
        )
        current_solution_start = np.asarray(result.x, dtype=float)
        total_seconds += seconds
        interim_summary, _ = extract_result(
            result,
            ix,
            terms,
            el,
            syn,
            wind_per_kw,
            scenario,
            "design_global_fractional_iteration",
            seconds,
        )
        updated_lcoa = float(interim_summary["LCOA_CNY_per_tNH3"])
        residual_cny = float(
            interim_summary["annual_total_cost_CNY"]
            - lcoa * interim_summary["annual_NH3_t"]
        )
        relative_residual = abs(residual_cny) / max(
            float(interim_summary["annual_total_cost_CNY"]),
            1.0,
        )
        objective_best_bound = float(
            getattr(result, "best_bound", np.nan)
        )
        horizon_fraction = float(terms["horizon_fraction"])
        production_min_t = (
            MIN_FIXED_WIND_NH3_T_PER_Y * horizon_fraction
        )
        production_max_t = (
            float(np.max(terms["plant_options_kt_per_y"]))
            * 1000.0
            * horizon_fraction
        )
        if np.isfinite(objective_best_bound):
            denominator = (
                production_max_t
                if objective_best_bound >= 0.0
                else production_min_t
            )
            fractional_lower_bound = (
                lcoa + objective_best_bound / max(denominator, 1e-12)
            )
            fractional_upper_bound = updated_lcoa
            fractional_global_gap = max(
                fractional_upper_bound - fractional_lower_bound,
                0.0,
            ) / max(abs(fractional_upper_bound), 1.0)
        else:
            fractional_lower_bound = np.nan
            fractional_upper_bound = updated_lcoa
            fractional_global_gap = np.inf
        history.append(
            {
                "iteration": iteration,
                "input_LCOA_CNY_per_tNH3": lcoa,
                "output_LCOA_CNY_per_tNH3": updated_lcoa,
                "parametric_objective_incumbent_CNY": float(
                    getattr(result, "incumbent_objective", result.fun)
                ),
                "parametric_objective_best_bound_CNY": objective_best_bound,
                "subproblem_mip_gap": float(
                    getattr(result, "mip_gap", np.nan)
                ),
                "relative_fractional_residual": relative_residual,
                "fractional_LCOA_lower_bound_CNY_per_tNH3": (
                    fractional_lower_bound
                ),
                "fractional_LCOA_upper_bound_CNY_per_tNH3": (
                    fractional_upper_bound
                ),
                "fractional_global_relative_gap": fractional_global_gap,
                "solve_seconds": seconds,
                "subproblem_gap_certified": bool(
                    getattr(result, "proven_optimal", False)
                ),
            }
        )
        final = (result, ix, terms)
        if (
            bool(getattr(result, "proven_optimal", False))
            and relative_residual <= float(dinkelbach_tolerance)
            and fractional_global_gap <= float(mip_rel_gap) + 1.0e-7
        ):
            result.fractional_global_certified = True
            result.design_global_certificate = True
            result.fractional_lcoa = updated_lcoa
            result.fractional_residual = relative_residual
            result.fractional_global_gap = fractional_global_gap
            result.fractional_lcoa_lower_bound = fractional_lower_bound
            result.fractional_lcoa_upper_bound = fractional_upper_bound
            result.dinkelbach_iterations = iteration
            return result, ix, terms, total_seconds, history
        lcoa = updated_lcoa

    if final is None:
        raise RuntimeError("Global fractional design produced no solve")
    raise RuntimeError(
        f"Global Dinkelbach solve for {syn['syn_id']}+{el['tech_id']} "
        f"did not meet the {100.0 * dinkelbach_tolerance:.3g}% residual "
        f"within {DINKELBACH_MAX_ITER} iterations"
    )

def _rounded_design_from_continuous_relaxation(
    result: SimpleNamespace,
    ix: Index,
    terms: dict,
    el: dict,
) -> FixedCapacities:
    options = np.asarray(terms["plant_options_kt_per_y"], dtype=float)
    plant_weights = np.asarray(
        result.x[ix.plant_y : ix.plant_y + ix.n_plants],
        dtype=float,
    )
    relaxed_plant = float(np.dot(plant_weights, options))
    admissible = np.flatnonzero(options >= relaxed_plant - 1.0e-8)
    plant_index = int(admissible[0]) if admissible.size else len(options) - 1

    relaxed_wind_kw = float(result.x[ix.wind_cap])
    wind_units = int(math.ceil(relaxed_wind_kw / WIND_TURBINE_KW - 1.0e-9))
    relaxed_el_kw = float(result.x[ix.el_cap])
    el_modules = int(
        math.ceil(relaxed_el_kw / float(el["unit_power_kW"]) - 1.0e-9)
    )
    return FixedCapacities(
        wind_kW=float(max(wind_units, 0) * WIND_TURBINE_KW),
        electrolyzer_kW=float(max(el_modules, 0) * el["unit_power_kW"]),
        plant_option_index=plant_index,
    )

def solver_failure_is_proven_infeasible(error: BaseException) -> bool:
    """Return true only for the solver's explicit infeasibility certificate."""
    message = str(error).lower()
    return "status=2" in message and (
        "gurobi cli: infeasible" in message
        or "model is infeasible" in message
        or "infeasible model" in message
    )

def _fixed_plant_screen_worker(payload: dict) -> dict:
    option_index = int(payload["option_index"])
    option = float(payload["plant_kt_per_y"])
    route_tag = str(payload["route"]).replace("+", "_")
    production_target_t = payload["production_target_t"]
    if (
        production_target_t is not None
        and option * 1000.0 < float(production_target_t) - 1.0e-6
    ):
        return {
            "option_index": option_index,
            "plant_kt_per_y": option,
            "feasible": False,
            "exclusion_certified": True,
            "screen_status": "structurally_infeasible_nameplate_below_target",
            "lower_bound": np.nan,
            "objective": np.nan,
            "wind_capacity_MW": np.nan,
            "electrolyzer_capacity_MW": np.nan,
            "annual_NH3_t": np.nan,
            "solve_seconds": 0.0,
            "dinkelbach_iterations": 0,
            "error": (
                f"{option:.6g} kt/y nameplate is below the exact "
                f"{float(production_target_t):.6g} t/y production target"
            ),
        }
    os.environ["WIND_NH3_GUROBI_THREADS"] = str(payload["threads"])
    os.environ["WIND_NH3_GUROBI_LOG_TAG"] = (
        f"screen_{route_tag}_{option_index:02d}"
    )
    try:
        if payload["fractional"]:
            result, ix, terms, seconds, history = (
                solve_global_fractional_design(
                    payload["el"],
                    payload["syn"],
                    payload["wind_per_kw"],
                    scenario=payload["scenario"],
                    plant_option_index=0,
                    initial_lcoa=float(payload["initial_lcoa"]),
                    production_target_t=payload["production_target_t"],
                    curtailment_rate_limit=payload[
                        "curtailment_rate_limit"
                    ],
                    mip_rel_gap=1.0e-4,
                    dinkelbach_tolerance=1.0e-4,
                    time_limit_s=MIP_TIME_LIMIT_S,
                    plant_options_kt_per_y=np.array([option], dtype=float),
                    wind_capacity_min_kw=payload["wind_capacity_min_kw"],
                    wind_capacity_max_kw=payload["wind_capacity_max_kw"],
                    enforce_hourly_commitment=False,
                    enforce_battery_exclusivity=False,
                    continuous_design_relaxation=True,
                )
            )
            summary, _ = extract_result(
                result,
                ix,
                terms,
                payload["el"],
                payload["syn"],
                payload["wind_per_kw"],
                payload["scenario"],
                "fixed_plant_fractional_relaxation_screen",
                seconds,
            )
            lower_bound = float(
                getattr(result, "fractional_lcoa_lower_bound", np.nan)
            )
            objective = float(summary["LCOA_CNY_per_tNH3"])
        else:
            result, ix, terms, seconds = (
                solve_global_fixed_production_design(
                    payload["el"],
                    payload["syn"],
                    payload["wind_per_kw"],
                    production_target_t=float(
                        payload["production_target_t"]
                    ),
                    plant_option_index=0,
                    scenario=payload["scenario"],
                    curtailment_rate_limit=payload[
                        "curtailment_rate_limit"
                    ],
                    wind_utilization_target=payload[
                        "wind_utilization_target"
                    ],
                    mip_rel_gap=1.0e-4,
                    time_limit_s=MIP_TIME_LIMIT_S,
                    plant_options_kt_per_y=np.array([option], dtype=float),
                    wind_capacity_min_kw=payload["wind_capacity_min_kw"],
                    wind_capacity_max_kw=payload["wind_capacity_max_kw"],
                    enforce_hourly_commitment=False,
                    enforce_battery_exclusivity=False,
                    continuous_design_relaxation=True,
                )
            )
            summary, _ = extract_result(
                result,
                ix,
                terms,
                payload["el"],
                payload["syn"],
                payload["wind_per_kw"],
                payload["scenario"],
                "fixed_plant_cost_relaxation_screen",
                seconds,
            )
            lower_bound = float(getattr(result, "best_bound", np.nan))
            objective = float(summary["annual_total_cost_CNY"])
            history = []
        if not np.isfinite(lower_bound) or not np.isfinite(objective):
            raise RuntimeError(
                "Fixed-plant screen returned a non-finite objective or lower bound"
            )
        return {
            "option_index": option_index,
            "plant_kt_per_y": option,
            "feasible": True,
            "exclusion_certified": True,
            "screen_status": "feasible_certified_lower_bound",
            "lower_bound": lower_bound,
            "objective": objective,
            "wind_capacity_MW": float(summary["wind_capacity_MW"]),
            "electrolyzer_capacity_MW": float(
                summary["electrolyzer_capacity_MW"]
            ),
            "annual_NH3_t": float(summary["annual_NH3_t"]),
            "solve_seconds": float(seconds),
            "dinkelbach_iterations": len(history),
            "error": "",
        }
    except Exception as exc:
        proven_infeasible = solver_failure_is_proven_infeasible(exc)
        return {
            "option_index": option_index,
            "plant_kt_per_y": option,
            "feasible": False,
            "exclusion_certified": proven_infeasible,
            "screen_status": (
                "solver_proven_infeasible"
                if proven_infeasible
                else "solver_failure_or_uncertified"
            ),
            "lower_bound": np.nan,
            "objective": np.nan,
            "wind_capacity_MW": np.nan,
            "electrolyzer_capacity_MW": np.nan,
            "annual_NH3_t": np.nan,
            "solve_seconds": np.nan,
            "dinkelbach_iterations": 0,
            "error": str(exc),
        }

def _screen_fixed_plant_options(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    *,
    scenario: str,
    plant_options_kt_per_y: np.ndarray,
    fractional: bool,
    initial_lcoa: float,
    production_target_t: float | None,
    curtailment_rate_limit: float | None,
    wind_utilization_target: float | None,
    wind_capacity_min_kw: float | None,
    wind_capacity_max_kw: float | None,
) -> list[dict]:
    def screen_flag(value: object) -> bool:
        return str(value).strip().lower() in {"true", "1", "1.0"}

    options = np.asarray(plant_options_kt_per_y, dtype=float)
    screen_payload = {
        "route": f"{syn['syn_id']}+{el['tech_id']}",
        "scenario": scenario,
        "fractional": bool(fractional),
        "plant_options_kt_per_y": options.tolist(),
        "production_target_t": production_target_t,
        "curtailment_rate_limit": curtailment_rate_limit,
        "wind_utilization_target": wind_utilization_target,
        "wind_capacity_min_kw": wind_capacity_min_kw,
        "wind_capacity_max_kw": wind_capacity_max_kw,
        "route_input_signature": route_input_signature(
            el,
            syn,
            np.asarray(wind_per_kw, dtype=float),
            scenario,
        ),
        "screen_formulation": (
            "fixed-plant continuous installed/online module relaxation v2"
        ),
    }
    screen_signature = hashlib.sha256(
        json.dumps(
            screen_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cache_path = (
        AUDIT_DIR
        / (
            f"plant_enumeration_{scenario}_"
            f"{syn['syn_id']}_{el['tech_id']}.csv"
        )
    )
    if cache_path.exists():
        cached = pd.read_csv(cache_path)
        if (
            len(cached) == len(options)
            and "screen_signature" in cached
            and cached["screen_signature"].astype(str).eq(
                screen_signature
            ).all()
            and np.allclose(
                cached["plant_kt_per_y"].to_numpy(dtype=float),
                options,
            )
        ):
            cached_rows = cached.to_dict("records")
            for row in cached_rows:
                row["feasible"] = screen_flag(
                    row.get("feasible", False)
                )
                row["exclusion_certified"] = screen_flag(
                    row.get("exclusion_certified", False)
                )
            unresolved = [
                row
                for row in cached_rows
                if (
                    not screen_flag(row.get("feasible", False))
                    and not screen_flag(
                        row.get("exclusion_certified", False)
                    )
                )
                or (
                    screen_flag(row.get("feasible", False))
                    and not np.isfinite(float(row.get("lower_bound", np.nan)))
                )
            ]
            if not unresolved:
                return cached_rows
    worker_count = min(4, len(options))
    threads = int(os.environ.get("WIND_NH3_GUROBI_THREADS", "8"))
    payloads = [
        {
            "option_index": index,
            "plant_kt_per_y": float(option),
            "route": f"{syn['syn_id']}+{el['tech_id']}",
            "el": el,
            "syn": syn,
            "wind_per_kw": np.asarray(wind_per_kw, dtype=float),
            "scenario": scenario,
            "fractional": fractional,
            "initial_lcoa": initial_lcoa,
            "production_target_t": production_target_t,
            "curtailment_rate_limit": curtailment_rate_limit,
            "wind_utilization_target": wind_utilization_target,
            "wind_capacity_min_kw": wind_capacity_min_kw,
            "wind_capacity_max_kw": wind_capacity_max_kw,
            "threads": threads,
        }
        for index, option in enumerate(options)
    ]
    if len(payloads) == 1:
        rows = [_fixed_plant_screen_worker(payloads[0])]
    else:
        rows = [_fixed_plant_screen_worker(payload) for payload in payloads]
    rows = sorted(rows, key=lambda row: int(row["option_index"]))
    for row in rows:
        row["feasible"] = screen_flag(row.get("feasible", False))
        row["exclusion_certified"] = screen_flag(
            row.get("exclusion_certified", False)
        )
        row["screen_signature"] = screen_signature
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(
        cache_path,
        index=False,
        encoding="utf-8-sig",
    )
    unresolved = [
        row
        for row in rows
        if (
            not screen_flag(row.get("feasible", False))
            and not screen_flag(row.get("exclusion_certified", False))
        )
        or (
            screen_flag(row.get("feasible", False))
            and not np.isfinite(float(row.get("lower_bound", np.nan)))
        )
    ]
    if unresolved:
        details = [
            {
                "plant_kt_per_y": row["plant_kt_per_y"],
                "screen_status": row["screen_status"],
                "error": row["error"],
            }
            for row in unresolved
        ]
        raise RuntimeError(
            "Plant-option global-bound screen is incomplete; an unresolved "
            f"option cannot be omitted from the certificate: {details}"
        )
    return rows

def run_certified_fixed_production_design(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    *,
    production_target_t: float,
    scenario: str,
    final_stage: str,
    plant_option_index: int | None = None,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
    curtailment_rate_limit: float | None = None,
    wind_utilization_target: float | None = None,
    wind_capacity_min_kw: float | None = None,
    wind_capacity_max_kw: float | None = None,
    allow_engineering_feasible_gap: bool = False,
) -> tuple[dict, pd.DataFrame, list[dict]]:
    """Solve fixed output and optionally retain an audited over-gap incumbent."""
    lower, lower_ix, lower_terms, lower_seconds = (
        solve_global_fixed_production_design(
            el,
            syn,
            wind_per_kw,
            production_target_t=production_target_t,
            plant_option_index=plant_option_index,
            scenario=scenario,
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=wind_utilization_target,
            mip_rel_gap=1.0e-4,
            plant_options_kt_per_y=plant_options_kt_per_y,
            wind_capacity_min_kw=wind_capacity_min_kw,
            wind_capacity_max_kw=wind_capacity_max_kw,
            enforce_hourly_commitment=False,
            enforce_battery_exclusivity=False,
            continuous_design_relaxation=True,
        )
    )
    lower_summary, lower_dispatch = extract_result(
        lower,
        lower_ix,
        lower_terms,
        el,
        syn,
        wind_per_kw,
        scenario,
        "design_global_relaxation_lower_bound",
        lower_seconds,
    )
    fixed = _rounded_design_from_continuous_relaxation(
        lower,
        lower_ix,
        lower_terms,
        el,
    )
    (
        summary,
        dispatch,
        commitment_history,
        exact_result,
        exact_ix,
        _,
    ) = (
        _solve_fixed_equipment_commitment_rerun(
            el,
            syn,
            wind_per_kw,
            scenario,
            fixed,
            production_target_t=production_target_t,
            final_stage=final_stage,
            electrolyzer_on_start=lower.x[
                lower_ix.el_on : lower_ix.el_on + lower_ix.n
            ],
            electrolyzer_power_start=lower_dispatch[
                "electrolyzer_power_kW"
            ].to_numpy(dtype=float),
            synthesis_power_start=lower_dispatch[
                "synthesis_power_kW"
            ].to_numpy(dtype=float),
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=wind_utilization_target,
            plant_options_kt_per_y=plant_options_kt_per_y,
            allow_engineering_feasible_gap=allow_engineering_feasible_gap,
            return_raw=True,
        )
    )
    lower_cost_bound = float(getattr(lower, "best_bound", np.nan))
    upper_cost = float(summary["annual_total_cost_CNY"])
    if not np.isfinite(lower_cost_bound):
        raise RuntimeError("Fixed-production design lower bound is missing")
    design_gap = max(
        upper_cost - lower_cost_bound,
        0.0,
    ) / max(abs(upper_cost), 1.0)
    certificate_escalated_to_discrete_design = False
    certificate_escalated_to_integer_primary_design = False
    if (
        design_gap > MIP_REL_GAP + 1.0e-7
        and not allow_engineering_feasible_gap
    ):
        options = np.asarray(
            plant_options_kt_per_y
            if plant_options_kt_per_y is not None
            else PLANT_OPTIONS_KT_PER_Y,
            dtype=float,
        )
        screen_rows = _screen_fixed_plant_options(
            el,
            syn,
            wind_per_kw,
            scenario=scenario,
            plant_options_kt_per_y=options,
            fractional=False,
            initial_lcoa=0.0,
            production_target_t=production_target_t,
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=wind_utilization_target,
            wind_capacity_min_kw=wind_capacity_min_kw,
            wind_capacity_max_kw=wind_capacity_max_kw,
        )
        feasible_rows = [row for row in screen_rows if row["feasible"]]
        if not feasible_rows:
            raise RuntimeError(
                f"{scenario} {syn['syn_id']}+{el['tech_id']} has no "
                "feasible synthesis nameplate in the disclosed grid"
            )
        best_screen = min(feasible_rows, key=lambda row: row["objective"])
        global_screen_lower = min(
            float(row["lower_bound"]) for row in feasible_rows
        )
        best_option_index = int(best_screen["option_index"])
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(screen_rows).to_csv(
            AUDIT_DIR
            / (
                f"plant_enumeration_{scenario}_"
                f"{syn['syn_id']}_{el['tech_id']}.csv"
            ),
            index=False,
            encoding="utf-8-sig",
        )
        lower, lower_ix, lower_terms, discrete_seconds = (
            solve_global_fixed_production_design(
                el,
                syn,
                wind_per_kw,
                production_target_t=production_target_t,
                plant_option_index=best_option_index,
                scenario=scenario,
                curtailment_rate_limit=curtailment_rate_limit,
                wind_utilization_target=wind_utilization_target,
                mip_rel_gap=1.0e-4,
                time_limit_s=MIP_TIME_LIMIT_S,
                plant_options_kt_per_y=options,
                wind_capacity_min_kw=wind_capacity_min_kw,
                wind_capacity_max_kw=wind_capacity_max_kw,
                enforce_hourly_commitment=False,
                enforce_battery_exclusivity=False,
                continuous_design_relaxation=True,
            )
        )
        lower_seconds += discrete_seconds + sum(
            float(row["solve_seconds"])
            for row in feasible_rows
            if np.isfinite(float(row["solve_seconds"]))
        )
        lower_summary, lower_dispatch = extract_result(
            lower,
            lower_ix,
            lower_terms,
            el,
            syn,
            wind_per_kw,
            scenario,
            "enumerated_fixed_plant_relaxation_lower_bound",
            discrete_seconds,
        )
        fixed = _rounded_design_from_continuous_relaxation(
            lower,
            lower_ix,
            lower_terms,
            el,
        )
        (
            summary,
            dispatch,
            discrete_commitment_history,
            exact_result,
            exact_ix,
            _,
        ) = _solve_fixed_equipment_commitment_rerun(
            el,
            syn,
            wind_per_kw,
            scenario,
            fixed,
            production_target_t=production_target_t,
            final_stage=final_stage,
            electrolyzer_on_start=lower.x[
                lower_ix.el_on : lower_ix.el_on + lower_ix.n
            ],
            electrolyzer_power_start=lower_dispatch[
                "electrolyzer_power_kW"
            ].to_numpy(dtype=float),
            synthesis_power_start=lower_dispatch[
                "synthesis_power_kW"
            ].to_numpy(dtype=float),
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=wind_utilization_target,
            plant_options_kt_per_y=plant_options_kt_per_y,
            allow_engineering_feasible_gap=allow_engineering_feasible_gap,
            return_raw=True,
        )
        commitment_history.extend(discrete_commitment_history)
        commitment_history.extend(
            {
                "scenario": scenario,
                "route": f"{syn['syn_id']}+{el['tech_id']}",
                "stage": "plant_option_relaxation_screen",
                **row,
            }
            for row in screen_rows
        )
        lower_cost_bound = global_screen_lower
        upper_cost = float(summary["annual_total_cost_CNY"])
        design_gap = max(
            upper_cost - lower_cost_bound,
            0.0,
        ) / max(abs(upper_cost), 1.0)
        certificate_escalated_to_discrete_design = True
    if (
        design_gap > MIP_REL_GAP + 1.0e-7
        and not allow_engineering_feasible_gap
    ):
        # The plant screen above still relaxes wind-turbine and electrolyzer
        # module counts. Solve those primary design variables as integers before
        # rejecting an otherwise feasible hourly schedule. Hourly commitment and
        # battery direction remain relaxed here, so the best bound is a valid
        # lower bound for the final exact-dispatch rerun.
        options = np.asarray(
            plant_options_kt_per_y
            if plant_options_kt_per_y is not None
            else PLANT_OPTIONS_KT_PER_Y,
            dtype=float,
        )
        integer_lower, integer_ix, integer_terms, integer_seconds = (
            solve_global_fixed_production_design(
                el,
                syn,
                wind_per_kw,
                production_target_t=production_target_t,
                plant_option_index=plant_option_index,
                scenario=scenario,
                curtailment_rate_limit=curtailment_rate_limit,
                wind_utilization_target=wind_utilization_target,
                mip_rel_gap=min(0.005, MIP_REL_GAP / 4.0),
                time_limit_s=MIP_TIME_LIMIT_S,
                plant_options_kt_per_y=options,
                wind_capacity_min_kw=wind_capacity_min_kw,
                wind_capacity_max_kw=wind_capacity_max_kw,
                enforce_hourly_commitment=False,
                enforce_battery_exclusivity=False,
                continuous_design_relaxation=False,
            )
        )
        integer_summary, integer_dispatch = extract_result(
            integer_lower,
            integer_ix,
            integer_terms,
            el,
            syn,
            wind_per_kw,
            scenario,
            "integer_primary_design_relaxation_lower_bound",
            integer_seconds,
        )
        fixed = _rounded_design_from_continuous_relaxation(
            integer_lower,
            integer_ix,
            integer_terms,
            el,
        )
        (
            summary,
            dispatch,
            integer_commitment_history,
            exact_result,
            exact_ix,
            _,
        ) = _solve_fixed_equipment_commitment_rerun(
            el,
            syn,
            wind_per_kw,
            scenario,
            fixed,
            production_target_t=production_target_t,
            final_stage=final_stage,
            electrolyzer_on_start=integer_lower.x[
                integer_ix.el_on : integer_ix.el_on + integer_ix.n
            ],
            electrolyzer_power_start=integer_dispatch[
                "electrolyzer_power_kW"
            ].to_numpy(dtype=float),
            synthesis_power_start=integer_dispatch[
                "synthesis_power_kW"
            ].to_numpy(dtype=float),
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=wind_utilization_target,
            plant_options_kt_per_y=options,
            allow_engineering_feasible_gap=allow_engineering_feasible_gap,
            return_raw=True,
        )
        commitment_history.extend(integer_commitment_history)
        commitment_history.append(
            {
                "scenario": scenario,
                "route": f"{syn['syn_id']}+{el['tech_id']}",
                "stage": "integer_primary_design_relaxation_lower_bound",
                "solve_seconds": integer_seconds,
                "objective_incumbent_CNY": float(integer_lower.fun),
                "objective_best_bound_CNY": float(
                    getattr(integer_lower, "best_bound", np.nan)
                ),
                "solver_gap": float(
                    getattr(integer_lower, "mip_gap", np.nan)
                ),
                "selected_wind_MW": integer_summary["wind_capacity_MW"],
                "selected_electrolyzer_MW": integer_summary[
                    "electrolyzer_capacity_MW"
                ],
                "selected_synthesis_kt_per_y": integer_summary[
                    "synthesis_nameplate_kt_per_y"
                ],
            }
        )
        lower = integer_lower
        lower_ix = integer_ix
        lower_terms = integer_terms
        lower_summary = integer_summary
        lower_dispatch = integer_dispatch
        lower_seconds += integer_seconds
        lower_cost_bound = float(getattr(integer_lower, "best_bound", np.nan))
        upper_cost = float(summary["annual_total_cost_CNY"])
        design_gap = max(
            upper_cost - lower_cost_bound,
            0.0,
        ) / max(abs(upper_cost), 1.0)
        certificate_escalated_to_integer_primary_design = True
    formal_certification = design_gap <= MIP_REL_GAP + 1.0e-7
    if not formal_certification and not allow_engineering_feasible_gap:
        raise RuntimeError(
            f"{scenario} {syn['syn_id']}+{el['tech_id']} exact-design "
            f"gap is {design_gap:.2%}; additional design candidates are required"
        )
    fixed_dispatch_gap = float(summary["mip_gap"])
    summary.update(
        {
            "stage": final_stage,
            "scenario": scenario,
            "status": (
                "Design-global near-optimal solution certified within the "
                "declared gap by a relaxation lower bound and exact hourly "
                "feasible upper bound"
                if formal_certification
                else "Engineering-feasible exact hourly solution retained "
                "for internal Pareto selection; global optimality is not "
                "certified within the declared gap"
            ),
            "solver": (
                "Gurobi 10.0.1; integer-design relaxation lower bound plus "
                "fixed-design exact hourly commitment certificate"
            ),
            "mip_gap": design_gap,
            "allowed_mip_gap": MIP_REL_GAP,
            "fixed_capacity_dispatch_gap": fixed_dispatch_gap,
            "design_and_dispatch_optimality_certified": formal_certification,
            "full_design_global_near_optimality_certified": formal_certification,
            "exact_global_optimum_proven": bool(
                formal_certification and design_gap <= 1.0e-9
            ),
            # Legacy compatibility field; see optimality_scope_note.
            "full_design_global_optimality_proven": formal_certification,
            "formal_certification": formal_certification,
            "global_design_integrality": True,
            "engineering_feasible_selection_eligible": True,
            "solver_return_mode": (
                "formal_certified"
                if formal_certification
                else "engineering_feasible_uncertified"
            ),
            "design_global_certificate_method": (
                "integer_primary_design_relaxation_LB_plus_exact_integer_UB"
                if certificate_escalated_to_integer_primary_design
                else "enumerated_fixed_plant_relaxation_LB_plus_exact_integer_UB"
                if certificate_escalated_to_discrete_design
                else "continuous_design_relaxation_LB_plus_exact_integer_UB"
            ),
            "design_global_lower_bound_CNY": lower_cost_bound,
            "design_global_upper_bound_CNY": upper_cost,
            "design_global_lower_bound_CNY_per_tNH3": (
                lower_cost_bound / production_target_t
            ),
            "design_global_upper_bound_CNY_per_tNH3": (
                upper_cost / production_target_t
            ),
            "design_global_relative_gap": design_gap,
            "certificate_escalated_to_discrete_design": (
                certificate_escalated_to_discrete_design
            ),
            "certificate_escalated_to_integer_primary_design": (
                certificate_escalated_to_integer_primary_design
            ),
            "solver_objective_incumbent": upper_cost,
            "solver_objective_best_bound": lower_cost_bound,
            "optimality_scope_note": (
                "Global certificate over the disclosed integer wind, "
                "electrolyzer-module and synthesis grids. The lower bound "
                + (
                    "retains integer primary-equipment sizing but relaxes "
                    "hourly module commitment and battery direction; "
                    if certificate_escalated_to_integer_primary_design
                    else "relaxes equipment quantization, one-of-N capacity "
                    "selection and hourly module commitment; "
                )
                + "the upper bound restores "
                "integer starts/stops, minimum load, cold-start preparation, "
                "ramps and cyclic storage."
            ),
        }
    )
    summary["continuous_sizing_annual_NH3_t"] = float(
        lower_summary["annual_NH3_t"]
    )
    summary["continuous_target_retained_ratio"] = 1.0
    history = [
        {
            "scenario": scenario,
            "route": summary["route"],
            "stage": "continuous_design_relaxation_lower_bound",
            "solve_seconds": lower_seconds,
            "objective_incumbent_CNY": float(lower.fun),
            "objective_best_bound_CNY": lower_cost_bound,
            "solver_gap": float(getattr(lower, "mip_gap", np.nan)),
            "selected_wind_MW": lower_summary["wind_capacity_MW"],
            "selected_electrolyzer_MW": lower_summary[
                "electrolyzer_capacity_MW"
            ],
            "selected_synthesis_kt_per_y": lower_summary[
                "synthesis_nameplate_kt_per_y"
            ],
            "rounded_wind_MW": fixed.wind_kW / 1000.0,
            "rounded_electrolyzer_MW": fixed.electrolyzer_kW / 1000.0,
            "rounded_synthesis_kt_per_y": float(
                np.asarray(
                    plant_options_kt_per_y
                    if plant_options_kt_per_y is not None
                    else PLANT_OPTIONS_KT_PER_Y,
                    dtype=float,
                )[fixed.plant_option_index]
            ),
        },
        *commitment_history,
        {
            "scenario": scenario,
            "route": summary["route"],
            "stage": "global_bound_certificate",
            "solve_seconds": lower_seconds
            + float(summary["solve_seconds"]),
            "objective_incumbent_CNY": upper_cost,
            "objective_best_bound_CNY": lower_cost_bound,
            "solver_gap": design_gap,
        },
    ]
    return summary, dispatch, history

def run_certified_fractional_design(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    *,
    scenario: str,
    final_stage: str,
    initial_lcoa: float = 5000.0,
    plant_option_index: int | None = None,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
    production_target_t: float | None = None,
    curtailment_rate_limit: float | None = None,
    wind_capacity_min_kw: float | None = None,
    wind_capacity_max_kw: float | None = None,
) -> tuple[dict, pd.DataFrame, list[dict]]:
    """Certify a free-output fractional design by global lower/upper bounds."""
    lower, lower_ix, lower_terms, lower_seconds, lower_history = (
        solve_global_fractional_design(
            el,
            syn,
            wind_per_kw,
            scenario=scenario,
            plant_option_index=plant_option_index,
            initial_lcoa=initial_lcoa,
            production_target_t=production_target_t,
            curtailment_rate_limit=curtailment_rate_limit,
            mip_rel_gap=1.0e-4,
            dinkelbach_tolerance=1.0e-4,
            plant_options_kt_per_y=plant_options_kt_per_y,
            wind_capacity_min_kw=wind_capacity_min_kw,
            wind_capacity_max_kw=wind_capacity_max_kw,
            enforce_hourly_commitment=False,
            enforce_battery_exclusivity=False,
            continuous_design_relaxation=True,
        )
    )
    lower_summary, lower_dispatch = extract_result(
        lower,
        lower_ix,
        lower_terms,
        el,
        syn,
        wind_per_kw,
        scenario,
        "design_global_relaxation_fractional_lower_bound",
        lower_seconds,
    )
    fixed = _rounded_design_from_continuous_relaxation(
        lower,
        lower_ix,
        lower_terms,
        el,
    )
    (
        summary,
        dispatch,
        commitment_history,
        exact_result,
        exact_ix,
        _,
    ) = (
        _solve_fixed_wind_rounded_lcoa_rerun(
            el,
            syn,
            wind_per_kw,
            fixed,
            initial_lcoa=float(lower_summary["LCOA_CNY_per_tNH3"]),
            electrolyzer_on_start=lower.x[
                lower_ix.el_on : lower_ix.el_on + lower_ix.n
            ],
            electrolyzer_power_start=lower_dispatch[
                "electrolyzer_power_kW"
            ].to_numpy(dtype=float),
            synthesis_power_start=lower_dispatch[
                "synthesis_power_kW"
            ].to_numpy(dtype=float),
            plant_options_kt_per_y=plant_options_kt_per_y,
            return_raw=True,
        )
    )
    lower_lcoa_bound = float(
        getattr(lower, "fractional_lcoa_lower_bound", np.nan)
    )
    upper_lcoa = float(summary["LCOA_CNY_per_tNH3"])
    if not np.isfinite(lower_lcoa_bound):
        raise RuntimeError("Fixed-wind fractional design lower bound is missing")
    design_gap = max(
        upper_lcoa - lower_lcoa_bound,
        0.0,
    ) / max(abs(upper_lcoa), 1.0)
    certificate_escalated_to_discrete_design = False
    if design_gap > MIP_REL_GAP + 1.0e-7:
        options = np.asarray(
            plant_options_kt_per_y
            if plant_options_kt_per_y is not None
            else PLANT_OPTIONS_KT_PER_Y,
            dtype=float,
        )
        screen_rows = _screen_fixed_plant_options(
            el,
            syn,
            wind_per_kw,
            scenario=scenario,
            plant_options_kt_per_y=options,
            fractional=True,
            initial_lcoa=upper_lcoa,
            production_target_t=production_target_t,
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=None,
            wind_capacity_min_kw=wind_capacity_min_kw,
            wind_capacity_max_kw=wind_capacity_max_kw,
        )
        feasible_rows = [row for row in screen_rows if row["feasible"]]
        if not feasible_rows:
            raise RuntimeError(
                f"{scenario} {syn['syn_id']}+{el['tech_id']} has no "
                "feasible synthesis nameplate in the disclosed grid"
            )
        best_screen = min(feasible_rows, key=lambda row: row["objective"])
        global_screen_lower = min(
            float(row["lower_bound"]) for row in feasible_rows
        )
        best_option_index = int(best_screen["option_index"])
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(screen_rows).to_csv(
            AUDIT_DIR
            / (
                f"plant_enumeration_{scenario}_"
                f"{syn['syn_id']}_{el['tech_id']}.csv"
            ),
            index=False,
            encoding="utf-8-sig",
        )
        (
            lower,
            lower_ix,
            lower_terms,
            discrete_seconds,
            discrete_history,
        ) = solve_global_fractional_design(
            el,
            syn,
            wind_per_kw,
            scenario=scenario,
            plant_option_index=best_option_index,
            initial_lcoa=float(best_screen["objective"]),
            production_target_t=production_target_t,
            curtailment_rate_limit=curtailment_rate_limit,
            mip_rel_gap=1.0e-4,
            dinkelbach_tolerance=1.0e-4,
            time_limit_s=MIP_TIME_LIMIT_S,
            plant_options_kt_per_y=options,
            wind_capacity_min_kw=wind_capacity_min_kw,
            wind_capacity_max_kw=wind_capacity_max_kw,
            enforce_hourly_commitment=False,
            enforce_battery_exclusivity=False,
            continuous_design_relaxation=True,
        )
        lower_seconds += discrete_seconds + sum(
            float(row["solve_seconds"])
            for row in feasible_rows
            if np.isfinite(float(row["solve_seconds"]))
        )
        lower_history.extend(discrete_history)
        lower_history.extend(
            {
                "iteration": np.nan,
                "stage": "plant_option_relaxation_screen",
                **row,
            }
            for row in screen_rows
        )
        lower_summary, lower_dispatch = extract_result(
            lower,
            lower_ix,
            lower_terms,
            el,
            syn,
            wind_per_kw,
            scenario,
            "enumerated_fixed_plant_fractional_lower_bound",
            discrete_seconds,
        )
        fixed = _rounded_design_from_continuous_relaxation(
            lower,
            lower_ix,
            lower_terms,
            el,
        )
        (
            summary,
            dispatch,
            discrete_commitment_history,
            exact_result,
            exact_ix,
            _,
        ) = _solve_fixed_wind_rounded_lcoa_rerun(
            el,
            syn,
            wind_per_kw,
            fixed,
            initial_lcoa=float(lower_summary["LCOA_CNY_per_tNH3"]),
            electrolyzer_on_start=lower.x[
                lower_ix.el_on : lower_ix.el_on + lower_ix.n
            ],
            electrolyzer_power_start=lower_dispatch[
                "electrolyzer_power_kW"
            ].to_numpy(dtype=float),
            synthesis_power_start=lower_dispatch[
                "synthesis_power_kW"
            ].to_numpy(dtype=float),
            plant_options_kt_per_y=plant_options_kt_per_y,
            return_raw=True,
        )
        commitment_history.extend(discrete_commitment_history)
        lower_lcoa_bound = global_screen_lower
        upper_lcoa = float(summary["LCOA_CNY_per_tNH3"])
        design_gap = max(
            upper_lcoa - lower_lcoa_bound,
            0.0,
        ) / max(abs(upper_lcoa), 1.0)
        certificate_escalated_to_discrete_design = True
    if design_gap > MIP_REL_GAP + 1.0e-7:
        raise RuntimeError(
            f"{scenario} {syn['syn_id']}+{el['tech_id']} exact-design "
            f"gap is {design_gap:.2%}; additional design candidates are required"
        )
    fixed_dispatch_gap = float(summary["mip_gap"])
    summary.update(
        {
            "stage": final_stage,
            "scenario": scenario,
            "status": (
                "Design-global fractional near-optimal solution certified "
                "within the declared gap by a relaxation lower bound and "
                "exact hourly feasible upper bound"
            ),
            "solver": (
                "Gurobi 10.0.1; Dinkelbach integer-design relaxation lower "
                "bound plus fixed-design exact hourly commitment certificate"
            ),
            "mip_gap": design_gap,
            "allowed_mip_gap": MIP_REL_GAP,
            "fixed_capacity_dispatch_gap": fixed_dispatch_gap,
            "design_and_dispatch_optimality_certified": True,
            "full_design_global_near_optimality_certified": True,
            "exact_global_optimum_proven": bool(design_gap <= 1.0e-9),
            # Legacy compatibility field; see optimality_scope_note.
            "full_design_global_optimality_proven": True,
            "global_design_integrality": True,
            "design_global_certificate_method": (
                "enumerated_fixed_plant_fractional_LB_plus_exact_integer_UB"
                if certificate_escalated_to_discrete_design
                else "fractional_continuous_design_relaxation_LB_plus_exact_integer_UB"
            ),
            "design_global_lower_bound_CNY_per_tNH3": lower_lcoa_bound,
            "design_global_upper_bound_CNY_per_tNH3": upper_lcoa,
            "design_global_relative_gap": design_gap,
            "fractional_LCOA_lower_bound_CNY_per_tNH3": lower_lcoa_bound,
            "fractional_LCOA_upper_bound_CNY_per_tNH3": upper_lcoa,
            "fractional_global_relative_gap": design_gap,
            "certificate_escalated_to_discrete_design": (
                certificate_escalated_to_discrete_design
            ),
            "fractional_relative_residual": float(
                getattr(lower, "fractional_residual", np.nan)
            ),
            "solver_objective_incumbent": upper_lcoa,
            "solver_objective_best_bound": lower_lcoa_bound,
            "optimality_scope_note": (
                "Global fractional certificate over the disclosed integer "
                "wind, electrolyzer and synthesis design grid. The lower bound "
                "relaxes equipment quantization, one-of-N capacity selection "
                "and hourly module "
                "commitment; the upper bound restores integer starts/stops, "
                "minimum load, cold-start preparation, ramps and cyclic "
                "storage at the selected primary design."
            ),
        }
    )
    summary["continuous_sizing_annual_NH3_t"] = float(
        lower_summary["annual_NH3_t"]
    )
    summary["continuous_target_retained_ratio"] = float(
        summary["annual_NH3_t"]
        / max(float(lower_summary["annual_NH3_t"]), 1e-12)
    )
    history = []
    for row in lower_history:
        item = dict(row)
        item.update(
            {
                "scenario": scenario,
                "route": summary["route"],
                "stage": "fractional_continuous_design_relaxation_lower_bound",
            }
        )
        history.append(item)
    for row in commitment_history:
        item = dict(row)
        item["scenario"] = scenario
        history.append(item)
    history.append(
        {
            "scenario": scenario,
            "route": summary["route"],
            "stage": "global_fractional_bound_certificate",
            "objective_incumbent_CNY_per_tNH3": upper_lcoa,
            "objective_best_bound_CNY_per_tNH3": lower_lcoa_bound,
            "solver_gap": design_gap,
        }
    )
    return summary, dispatch, history

def _solve_fixed_equipment_commitment_rerun(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    scenario: str,
    fixed: FixedCapacities,
    *,
    production_target_t: float | None,
    final_stage: str,
    electrolyzer_on_start: np.ndarray | None = None,
    electrolyzer_power_start: np.ndarray | None = None,
    synthesis_power_start: np.ndarray | None = None,
    curtailment_rate_limit: float | None = None,
    wind_utilization_target: float | None = None,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
    allow_engineering_feasible_gap: bool = False,
    return_raw: bool = False,
) -> tuple:
    # Solve rounded/fixed primary equipment with the full hourly electrolyzer
    # commitment logic active. Battery direction uses its exact cost-minimizing
    # continuous relaxation and is checked ex post for simultaneous operation.
    final, ix, terms, seconds, surrogate_history = (
        solve_surrogate_coupled_fixed_capacity(
            el,
            syn,
            wind_per_kw,
            scenario,
            fixed,
            production_target_t=production_target_t,
            electrolyzer_on_start=electrolyzer_on_start,
            electrolyzer_power_start=electrolyzer_power_start,
            synthesis_power_start=synthesis_power_start,
            curtailment_rate_limit=curtailment_rate_limit,
            wind_utilization_target=wind_utilization_target,
            plant_options_kt_per_y=plant_options_kt_per_y,
            allow_engineering_feasible_gap=allow_engineering_feasible_gap,
        )
    )
    final_summary, final_dispatch = extract_result(
        final,
        ix,
        terms,
        el,
        syn,
        wind_per_kw,
        scenario,
        final_stage,
        seconds,
    )
    selected_plant_options = np.asarray(
        plant_options_kt_per_y
        if plant_options_kt_per_y is not None
        else PLANT_OPTIONS_KT_PER_Y,
        dtype=float,
    )
    history = list(surrogate_history)
    history.append(
        {
            "scenario": scenario,
            "route": f"{syn['syn_id']}+{el['tech_id']}",
            "plant_kt_per_y": float(
                selected_plant_options[fixed.plant_option_index]
            ),
            "objective_LCOA_CNY_per_tNH3": float(
                final_summary["LCOA_CNY_per_tNH3"]
            ),
            "solve_seconds": seconds,
            "dinkelbach_iterations": 0,
            "final_relative_residual": float(
                getattr(final, "mip_gap", np.nan)
            ),
            "global_discrete_method": (
                "Direct rounded-equipment MILP with hourly electrolyzer "
                "commitment and an ex-post-audited exact battery-direction relaxation"
            ),
            "selected": True,
            "solver_error": "",
        }
    )
    if return_raw:
        return final_summary, final_dispatch, history, final, ix, terms
    return final_summary, final_dispatch, history

def _solve_fixed_wind_rounded_lcoa_rerun(
    el: dict,
    syn: dict,
    wind_per_kw: np.ndarray,
    fixed: FixedCapacities,
    *,
    initial_lcoa: float,
    electrolyzer_on_start: np.ndarray | None,
    electrolyzer_power_start: np.ndarray | None = None,
    synthesis_power_start: np.ndarray | None = None,
    plant_options_kt_per_y: np.ndarray | list[float] | None = None,
    return_raw: bool = False,
) -> tuple:
    if (electrolyzer_power_start is None) != (synthesis_power_start is None):
        raise ValueError(
            "electrolyzer_power_start and synthesis_power_start must be "
            "provided together"
        )
    if electrolyzer_power_start is not None:
        p_el_start = np.asarray(electrolyzer_power_start, dtype=float)
        p_syn_start = np.asarray(synthesis_power_start, dtype=float)
        if p_el_start.size != len(wind_per_kw) or p_syn_start.size != len(
            wind_per_kw
        ):
            raise ValueError(
                "Surrogate power starts must contain one value per hour"
            )
    started = time.time()
    (
        relaxed,
        relaxed_ix,
        relaxed_terms,
        relaxed_seconds,
        relaxed_lcoa,
        relaxed_iterations,
    ) = (
        _solve_fixed_wind_plant_dinkelbach(
            el,
            syn,
            wind_per_kw,
            fixed=fixed,
            plant_option_index=fixed.plant_option_index,
            initial_lcoa=float(initial_lcoa),
            enforce_electrolyzer_commitment=False,
            plant_options_kt_per_y=plant_options_kt_per_y,
        )
    )
    relaxed_online = np.asarray(
        relaxed.x[relaxed_ix.el_on : relaxed_ix.el_on + relaxed_ix.n],
        dtype=float,
    )
    relaxed_power = np.asarray(
        relaxed.x[relaxed_ix.p_el : relaxed_ix.p_el + relaxed_ix.n],
        dtype=float,
    )
    if electrolyzer_on_start is not None:
        supplied_online = np.asarray(electrolyzer_on_start, dtype=float)
        installed_modules = int(
            round(float(fixed.electrolyzer_kW) / float(el["unit_power_kW"]))
        )
        if np.max(supplied_online) <= 1.0 + 1e-9 and installed_modules > 1:
            supplied_online = np.ceil(
                np.maximum(
                    np.asarray(electrolyzer_power_start, dtype=float),
                    0.0,
                )
                / float(el["unit_power_kW"])
                - 1e-10
            )
        relaxed_online = np.maximum(relaxed_online, supplied_online)

    candidate_records: list[dict] = []
    best: tuple | None = None
    for label, schedule in _commitment_schedule_candidates(
        el,
        fixed,
        relaxed_online,
        relaxed_power,
    ):
        try:
            (
                candidate,
                candidate_ix,
                candidate_terms,
                candidate_seconds,
                candidate_lcoa,
                candidate_iterations,
            ) = (
                _solve_fixed_wind_plant_dinkelbach(
                    el,
                    syn,
                    wind_per_kw,
                    fixed=fixed,
                    plant_option_index=fixed.plant_option_index,
                    initial_lcoa=float(relaxed_lcoa),
                    enforce_electrolyzer_commitment=True,
                    electrolyzer_on_fixed=schedule,
                    plant_options_kt_per_y=plant_options_kt_per_y,
                )
            )
            candidate_summary, candidate_dispatch = extract_result(
                candidate,
                candidate_ix,
                candidate_terms,
                el,
                syn,
                wind_per_kw,
                "fixed_wind",
                "integer_primary_equipment_two_stage_lcoa_rerun",
                candidate_seconds,
            )
        except RuntimeError as exc:
            candidate_records.append(
                {
                    "label": label,
                    "feasible": False,
                    "online_module_hours": int(np.sum(schedule)),
                    "LCOA_CNY_per_tNH3": np.nan,
                    "annual_NH3_t": np.nan,
                    "error": str(exc),
                }
            )
            continue
        candidate_records.append(
            {
                "label": label,
                "feasible": True,
                "online_module_hours": int(np.sum(schedule)),
                "LCOA_CNY_per_tNH3": float(
                    candidate_summary["LCOA_CNY_per_tNH3"]
                ),
                "annual_NH3_t": float(candidate_summary["annual_NH3_t"]),
                "error": "",
            }
        )
        if (
            best is None
            or candidate_summary["LCOA_CNY_per_tNH3"]
            < best[0]["LCOA_CNY_per_tNH3"]
        ):
            best = (
                candidate_summary,
                candidate_dispatch,
                candidate,
                candidate_ix,
                candidate_terms,
                label,
            )
        candidate_gap = max(
            0.0,
            (
                float(candidate_summary["LCOA_CNY_per_tNH3"])
                - float(relaxed_lcoa)
            )
            / max(float(candidate_summary["LCOA_CNY_per_tNH3"]), 1.0),
        )
        if candidate_gap <= 5.0e-3:
            break

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    candidate_audit_path = (
        AUDIT_DIR
        / f"fixed_wind_commitment_candidates_{syn['syn_id']}_{el['tech_id']}.json"
    )
    candidate_audit_path.write_text(
        json.dumps(
            {
                "route": f"{syn['syn_id']}+{el['tech_id']}",
                "continuous_module_relaxation_LCOA_CNY_per_tNH3": float(
                    relaxed_lcoa
                ),
                "continuous_module_relaxation_iterations": relaxed_iterations,
                "candidates": candidate_records,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if best is None:
        raise RuntimeError(
            f"fixed_wind {syn['syn_id']}+{el['tech_id']} has no feasible "
            f"integer module schedule; inspect {candidate_audit_path}"
        )

    summary, dispatch, result, ix, terms, best_label = best
    battery_net_soc_change = (
        BESS_CHARGE_EFFICIENCY
        * np.asarray(result.x[ix.bat_ch : ix.bat_ch + ix.n], dtype=float)
        - np.asarray(
            result.x[ix.bat_dis : ix.bat_dis + ix.n],
            dtype=float,
        )
        / BESS_DISCHARGE_EFFICIENCY
    )
    battery_mode = (battery_net_soc_change >= 0.0).astype(int)
    exact_online_schedule = np.rint(
        result.x[ix.el_on : ix.el_on + ix.n]
    ).astype(int)
    (
        result,
        ix,
        terms,
        _,
        _,
        _,
    ) = _solve_fixed_wind_plant_dinkelbach(
        el,
        syn,
        wind_per_kw,
        fixed=fixed,
        plant_option_index=fixed.plant_option_index,
        initial_lcoa=float(summary["LCOA_CNY_per_tNH3"]),
        enforce_electrolyzer_commitment=True,
        electrolyzer_on_fixed=exact_online_schedule,
        battery_charge_mode_fixed=battery_mode,
        plant_options_kt_per_y=plant_options_kt_per_y,
    )
    summary, dispatch = extract_result(
        result,
        ix,
        terms,
        el,
        syn,
        wind_per_kw,
        "fixed_wind",
        "integer_primary_equipment_exact_battery_direction_rerun",
        time.time() - started,
    )
    lower_lcoa = float(relaxed_lcoa)
    certified_gap = max(
        0.0,
        (float(summary["LCOA_CNY_per_tNH3"]) - lower_lcoa)
        / max(float(summary["LCOA_CNY_per_tNH3"]), 1.0),
    )
    exact_commitment_fallback_history: list[dict] = []
    if certified_gap > MIP_REL_GAP + 1e-7:
        selected_plant_options = np.asarray(
            plant_options_kt_per_y
            if plant_options_kt_per_y is not None
            else PLANT_OPTIONS_KT_PER_Y,
            dtype=float,
        )
        selected_plant_kt = float(
            selected_plant_options[fixed.plant_option_index]
        )
        one_plant_options = np.array([selected_plant_kt], dtype=float)
        one_plant_fixed = FixedCapacities(
            wind_kW=fixed.wind_kW,
            electrolyzer_kW=fixed.electrolyzer_kW,
            battery_kWh=fixed.battery_kWh,
            h2_tank_Nm3=fixed.h2_tank_Nm3,
            plant_option_index=0,
        )
        (
            exact_commitment,
            exact_commitment_ix,
            _,
            _,
            exact_commitment_fallback_history,
        ) = solve_global_fractional_design(
            el,
            syn,
            wind_per_kw,
            scenario="fixed_wind",
            fixed=one_plant_fixed,
            initial_lcoa=float(summary["LCOA_CNY_per_tNH3"]),
            mip_rel_gap=MIP_REL_GAP,
            dinkelbach_tolerance=DINKELBACH_TOL,
            time_limit_s=MIP_TIME_LIMIT_S,
            plant_options_kt_per_y=one_plant_options,
            enforce_hourly_commitment=True,
            enforce_battery_exclusivity=False,
            continuous_design_relaxation=False,
            electrolyzer_on_start=exact_online_schedule,
        )
        exact_online_schedule = np.rint(
            exact_commitment.x[
                exact_commitment_ix.el_on :
                exact_commitment_ix.el_on + exact_commitment_ix.n
            ]
        ).astype(int)
        exact_battery_net_soc_change = (
            BESS_CHARGE_EFFICIENCY
            * np.asarray(
                exact_commitment.x[
                    exact_commitment_ix.bat_ch :
                    exact_commitment_ix.bat_ch + exact_commitment_ix.n
                ],
                dtype=float,
            )
            - np.asarray(
                exact_commitment.x[
                    exact_commitment_ix.bat_dis :
                    exact_commitment_ix.bat_dis + exact_commitment_ix.n
                ],
                dtype=float,
            )
            / BESS_DISCHARGE_EFFICIENCY
        )
        battery_mode = (
            exact_battery_net_soc_change >= 0.0
        ).astype(int)
        (
            result,
            ix,
            terms,
            _,
            _,
            _,
        ) = _solve_fixed_wind_plant_dinkelbach(
            el,
            syn,
            wind_per_kw,
            fixed=one_plant_fixed,
            plant_option_index=0,
            initial_lcoa=float(exact_commitment.fractional_lcoa),
            enforce_electrolyzer_commitment=True,
            electrolyzer_on_fixed=exact_online_schedule,
            battery_charge_mode_fixed=battery_mode,
            plant_options_kt_per_y=one_plant_options,
        )
        summary, dispatch = extract_result(
            result,
            ix,
            terms,
            el,
            syn,
            wind_per_kw,
            "fixed_wind",
            "exact_commitment_and_battery_direction_fallback",
            time.time() - started,
        )
        lower_lcoa = float(
            exact_commitment.fractional_lcoa_lower_bound
        )
        certified_gap = max(
            0.0,
            (float(summary["LCOA_CNY_per_tNH3"]) - lower_lcoa)
            / max(float(summary["LCOA_CNY_per_tNH3"]), 1.0),
        )
        best_label = "full_integer_commitment_Dinkelbach_fallback"
        candidate_records.append(
            {
                "label": best_label,
                "feasible": True,
                "online_module_hours": int(
                    np.sum(exact_online_schedule)
                ),
                "LCOA_CNY_per_tNH3": float(
                    summary["LCOA_CNY_per_tNH3"]
                ),
                "annual_NH3_t": float(summary["annual_NH3_t"]),
                "error": "",
                "fractional_lower_bound_CNY_per_tNH3": lower_lcoa,
                "certified_gap": certified_gap,
            }
        )
        candidate_audit_path.write_text(
            json.dumps(
                {
                    "route": f"{syn['syn_id']}+{el['tech_id']}",
                    "continuous_module_relaxation_LCOA_CNY_per_tNH3": (
                        float(relaxed_lcoa)
                    ),
                    "continuous_module_relaxation_iterations": (
                        relaxed_iterations
                    ),
                    "candidates": candidate_records,
                    "exact_commitment_fallback_iterations": (
                        exact_commitment_fallback_history
                    ),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    if certified_gap > MIP_REL_GAP + 1e-7:
        raise RuntimeError(
            f"fixed_wind {syn['syn_id']}+{el['tech_id']} best integer "
            f"schedule is {certified_gap:.2%} above the continuous module "
            f"lower bound; inspect {candidate_audit_path}"
        )

    result.mip_gap = float(certified_gap)
    result.allowed_mip_gap = float(MIP_REL_GAP)
    result.proven_optimal = True
    result.success = True
    result.status = 0
    result.message = (
        "Integer online-module schedule certified against the continuous "
        f"module-count fractional lower bound within {certified_gap:.4%}"
    )
    result.solver_backend = (
        "HiGHS Charnes-Cooper LP; deterministic integer schedule search "
        "with continuous-module lower-bound certificate"
    )
    active_models = [str(syn["syn_id"])]
    if el["tech_id"] == "ALK":
        active_models.insert(0, "ALK")
    terms["surrogate_coupling_enabled"] = True
    terms["surrogate_models"] = ";".join(active_models)
    terms["surrogate_iteration"] = 1
    terms["surrogate_fixed_point_residual"] = 0.0
    model_error = float(
        terms.get("surrogate_model_max_relative_error", np.nan)
    )
    if (
        not np.isfinite(model_error)
        or model_error > SURROGATE_PWL_MAX_REL_ERROR
    ):
        raise RuntimeError(
            "Aspen surrogate direct-MILP validation failed for "
            f"{syn['syn_id']}+{el['tech_id']}: error={model_error:.6g}"
        )
    total_seconds = time.time() - started
    summary, dispatch = extract_result(
        result,
        ix,
        terms,
        el,
        syn,
        wind_per_kw,
        "fixed_wind",
        "integer_primary_equipment_two_stage_lcoa_rerun",
        total_seconds,
    )
    selected_plant_options = np.asarray(
        plant_options_kt_per_y
        if plant_options_kt_per_y is not None
        else PLANT_OPTIONS_KT_PER_Y,
        dtype=float,
    )
    history = [
        {
            "scenario": "fixed_wind",
            "route": f"{syn['syn_id']}+{el['tech_id']}",
            "plant_kt_per_y": float(
                selected_plant_options[fixed.plant_option_index]
            ),
            "objective_LCOA_CNY_per_tNH3": float(
                summary["LCOA_CNY_per_tNH3"]
            ),
            "solve_seconds": total_seconds,
            "dinkelbach_iterations": 0,
            "final_relative_residual": certified_gap,
            "global_discrete_method": (
                "Aspen-surrogate fixed-equipment Charnes-Cooper LPs with "
                "integer module schedules and a continuous-module lower bound"
            ),
            "commitment_certificate_method": (
                "integer_schedule_plus_continuous_module_fractional_bound"
            ),
            "commitment_LP_lower_bound_CNY_per_t": lower_lcoa,
            "commitment_certified_gap": certified_gap,
            "commitment_candidate_count": len(candidate_records),
            "commitment_feasible_candidate_count": int(
                sum(bool(item["feasible"]) for item in candidate_records)
            ),
            "commitment_best_candidate": best_label,
            "selected": True,
            "solver_error": "",
        }
    ]
    history.extend(
        {
            **row,
            "scenario": "fixed_wind",
            "route": f"{syn['syn_id']}+{el['tech_id']}",
            "stage": "exact_integer_commitment_fractional_fallback",
        }
        for row in exact_commitment_fallback_history
    )
    summary["continuous_sizing_annual_NH3_t"] = np.nan
    summary["continuous_target_retained_ratio"] = np.nan
    if return_raw:
        return summary, dispatch, history, result, ix, terms
    return summary, dispatch, history


# Product-recovery terminal, applied once after the native process relations.
_targeted_native_curves = PARAMETERS['native_curves']
native_affine = surrogate_affine_parameters
aq, bq, _ = native_affine('Ru', 'nh3_t_per_h')
selected = {key:value['targeted_terminal'] for key,value in SYNTHESES.items()}
args = SimpleNamespace(terminal='reference')
original_process = process_costs
original_build = build_problem
original_extract = extract_result


def terminal_affine(model_id, output_key):
    a,b,error = native_affine(model_id, output_key)
    if model_id == "Ru" and args.terminal != "off":
        # Algebraic variable substitution in the existing fitted model:
        # q=aq*p_native+bq*S; p_total=p_native+e*q.
        # This preserves the original approximation exactly and does not
        # refit raw Aspen observations or change their rated output scale.
        e = selected["Ru"]["terminal_electricity_kWh_per_t_gross"]
        return a/(1+e*aq), b-a*e*bq/(1+e*aq), error
    return a,b,error

def process_costs(syn, capacity):
    r = original_process(syn, capacity)
    terminal = syn.get("targeted_terminal")
    if terminal:
        if abs(capacity-475.) > 1e-9:
            raise ValueError("Terminal package is rated only at the preserved 475 kt/year nameplate")
        capital = terminal["installed_terminal_capital_CNY"]
        annual = capital*(model.annualized_capital_factor(30.)+terminal["terminal_fixed_OM_fraction"])
        for key in ("process_capex_CNY", "process_equipment_capex_CNY"):
            r[key] += capital
        for key in ("annual_process_CNY", "annual_process_equipment_CNY", "annual_total_CNY"):
            r[key] += annual
        r["targeted_terminal_capital_CNY"] = capital
        r["targeted_terminal_fixed_annual_CNY"] = annual
    return r

def build_problem(el, syn, wind, scenario, **kwargs):
    problem = original_build(el, syn, wind, scenario, **kwargs)
    c, integers, bounds, constraints, ix, terms = problem
    terminal = syn.get("targeted_terminal")
    if terminal:
        c = c.copy()
        c[ix.nh3_prod:ix.nh3_prod+ix.n] += terminal["variable_cost_CNY_per_t_gross"]
        terms["targeted_terminal_contract"] = terminal
    return c, integers, bounds, constraints, ix, terms

def extract_result(result, ix, terms, el, syn, wind, *other, **kwargs):
    row, dispatch = original_extract(result, ix, terms, el, syn, wind, *other, **kwargs)
    terminal = syn.get("targeted_terminal")
    if terminal:
        q = dispatch.NH3_production_t_per_h
        fee = float(q.sum())*terminal["variable_cost_CNY_per_t_gross"]
        row["annual_total_cost_CNY"] += fee
        row["annual_synthesis_process_CNY"] += fee
        row["annual_terminal_variable_CNY"] = fee
        row["LCOA_CNY_per_tNH3"] = row["annual_total_cost_CNY"]/row["annual_NH3_t"]
        row["annual_ammonia_conversion_cost_CNY"] += fee
        row["ammonia_conversion_cost_CNY_per_tNH3"] = row["annual_ammonia_conversion_cost_CNY"]/row["annual_NH3_t"]
        row["H2_subsystem_share_of_LCOA"] = row["annual_H2_subsystem_cost_CNY"]/row["annual_total_cost_CNY"]
        dispatch["terminal_power_kW"] = q*terminal["terminal_electricity_kWh_per_t_gross"]
        dispatch["upstream_synthesis_power_kW"] = dispatch.synthesis_power_kW-dispatch.terminal_power_kW
        dispatch["liquid_NH3_component_t_per_h"] = q*terminal["liquid_recovery_fraction"]
        dispatch["terminal_makeup_water_m3_per_h"] = q*terminal["water_m3_per_t_gross"]
        dispatch["terminal_variable_cost_CNY_per_h"] = q*terminal["variable_cost_CNY_per_t_gross"]
    return row, dispatch

surrogate_affine_parameters = terminal_affine

def project_fe(model, syn, row, dispatch, curve):
    if syn["syn_id"] != "Fe":
        return dict(applied=False, power_excess_GWh=0.)
    pwl = model.fe_surrogate_pwl_spec()
    scale = row["synthesis_nameplate_kt_per_y"]*1000/8760/curve["nh3_t_per_h"][-1]
    power = np.interp(dispatch.H2_consumption_Nm3_per_h.to_numpy()/scale,
                      pwl["h2_Nm3_per_h"], pwl["power_kW"])*scale
    power += syn["product_conditioning_electric_kWh_per_kgNH3"]*dispatch.NH3_production_t_per_h.to_numpy()*1000
    excess = dispatch.synthesis_power_kW.to_numpy()-power
    if excess.min() < -.001:
        raise ValueError("Fe projection would require extra electric power")
    dispatch["synthesis_power_kW"] = power
    dispatch["curtailment_kW"] += excess
    for target, source in (("synthesis_NH3_t_per_kWh", "NH3_production_t_per_h"),
                           ("synthesis_H2_Nm3_per_kWh", "H2_consumption_Nm3_per_h")):
        dispatch[target] = dispatch[source]/power
    saving = float(excess.sum())*model.SURROGATE_POWER_TIEBREAKER_CNY_PER_KWH
    for key in ("annual_total_cost_CNY", "annual_surrogate_power_tiebreaker_CNY", "annual_numerical_tiebreaker_total_CNY"):
        row[key] -= saving
    row["LCOA_CNY_per_tNH3"] = row["annual_total_cost_CNY"]/row["annual_NH3_t"]
    row["design_global_relative_gap"] = 1-row["design_global_lower_bound_CNY_per_tNH3"]/row["LCOA_CNY_per_tNH3"]
    row["design_global_upper_bound_CNY_per_tNH3"] = row["LCOA_CNY_per_tNH3"]
    row["curtailment_rate"] = dispatch.curtailment_kW.sum()/dispatch.wind_generation_kW.sum()
    return dict(applied=True, power_excess_GWh=float(excess.sum()/1e6), cost_tiebreaker_saving_CNY=saving)

def matrix_check(model, el, syn, wind, row, d):
    scenario = row["scenario"] if row["scenario"] != "fixed_nameplate_475kt" else "variable_scale"
    c, integrality, bounds, constraints, ix, terms = model.build_problem(
        el, syn, wind, scenario,
        lcoa_parameter=row["LCOA_CNY_per_tNH3"] if scenario != "fixed_300kt" else 0.,
        global_design_integrality=True, enforce_electrolyzer_commitment=True,
        enforce_battery_exclusivity=True,
        plant_options_kt_per_y=np.array([row["synthesis_nameplate_kt_per_y"]]),
        production_target_t=row.get("annual_NH3_target_t") if scenario == "fixed_300kt" else None,
        wind_capacity_min_kw=model.FIXED_WIND_KW if scenario == "fixed_wind" else 0.,
        wind_capacity_max_kw=model.FIXED_WIND_KW if scenario == "fixed_wind" else model.MAX_WIND_KW,
    )
    vector = np.zeros(ix.size)
    mapping = {
        "bat_ch": "battery_charge_kW", "bat_dis": "battery_discharge_kW",
        "bat_soc": "battery_SOC_kWh", "p_el": "electrolyzer_power_kW",
        "p_syn": "synthesis_power_kW", "h2_soc": "H2_storage_Nm3",
        "el_on": "electrolyzer_online_modules", "el_start": "electrolyzer_modules_started",
        "el_stop": "electrolyzer_modules_stopped", "h2_prod": "H2_production_Nm3_per_h",
        "water_use": "water_consumption_m3_per_h", "nh3_prod": "NH3_production_t_per_h",
        "h2_use": "H2_consumption_Nm3_per_h",
    }
    for key, column in mapping.items():
        start = getattr(ix, key)
        vector[start:start+ix.n] = d[column].to_numpy()
    vector[ix.bat_charge_mode:ix.bat_charge_mode+ix.n] = (d.battery_charge_kW > 1e-6).astype(int)
    for key, value in {
        "el_cap": row["electrolyzer_capacity_MW"]*1000,
        "bat_cap": row["battery_capacity_MWh"]*1000,
        "h2_cap": row["h2_tank_capacity_Nm3"], "wind_cap": row["wind_capacity_MW"]*1000,
        "el_modules": row["electrolyzer_capacity_MW"]*1000/el["unit_power_kW"],
        "wind_units": row["wind_capacity_MW"]*1000/model.WIND_TURBINE_KW, "plant_y": 1.,
    }.items():
        vector[getattr(ix, key)] = value
    constraint_checks = []
    for i, constraint in enumerate(constraints):
        ax = constraint.A @ vector
        violation = np.maximum.reduce([np.zeros_like(ax), constraint.lb-ax, ax-constraint.ub])
        active = np.ones(len(ax), dtype=bool)
        if i == 1:
            for rid, variable, trigger in terms["indicator_constraints"]:
                active[rid] = abs(vector[variable]-trigger) < 1e-6
        violation[~active] = 0.
        finite_lb = np.where(np.isfinite(constraint.lb), np.abs(constraint.lb), 0.)
        finite_ub = np.where(np.isfinite(constraint.ub), np.abs(constraint.ub), 0.)
        tolerance = .001+1e-8*np.maximum(finite_lb, finite_ub)
        bad = np.where(violation > tolerance)[0]
        constraint_checks.append(dict(block=i, rows=len(ax),
            active_rows=int(active.sum()), max_violation=float(violation.max()),
            tolerance="1e-3 + 1e-8 * max(abs(finite row bounds)); indicators only when active",
            count_above_tolerance=len(bad),
            worst_row=int(violation.argmax())))
    bound_error = float(np.maximum.reduce([np.zeros_like(vector), bounds.lb-vector, vector-bounds.ub]).max())
    mask = np.asarray(integrality) == 1
    integer_error = float(np.max(np.abs(vector[mask]-np.rint(vector[mask]))))
    annual_from_objective = float(c @ vector)
    if scenario in ("variable_scale", "fixed_wind"):
        annual_from_objective += row["LCOA_CNY_per_tNH3"] * float(d.NH3_production_t_per_h.sum())
    cost_error = annual_from_objective - row["annual_total_cost_CNY"]
    cost_tolerance = max(.1, 1e-8 * abs(row["annual_total_cost_CNY"]))
    result = dict(variables=len(vector), constraints=constraint_checks,
                  bound_max_violation=bound_error, integer_max_violation=integer_error,
                  annual_cost_from_current_objective_CNY=annual_from_objective,
                  annual_cost_from_stored_components_CNY=row["annual_total_cost_CNY"],
                  cost_residual_CNY=cost_error, cost_tolerance_CNY=cost_tolerance,
                  passed=bool(bound_error <= .001 and integer_error <= 1e-6 and abs(cost_error) <= cost_tolerance and all(a["count_above_tolerance"] == 0 for a in constraint_checks)),
                  scope="All current single-selected-nameplate MILP rows, bounds and integer variables evaluated; no fresh optimization or global certificate.")
    del c, integrality, bounds, constraints, ix, terms, vector
    gc.collect()
    return result

def refresh_allocations(row, dispatch):
    supply = row["annual_wind_CNY"]+row["annual_battery_CNY"]+row["annual_battery_tiebreaker_CNY"]
    energy = float(dispatch.electrolyzer_power_kW.sum()+dispatch.synthesis_power_kW.sum())
    share = float(dispatch.electrolyzer_power_kW.sum()/energy)
    h2 = (supply*share+row["annual_electrolyzer_CNY"]+row["annual_h2_tank_CNY"]
          +row["annual_stack_replacement_CNY"]+row["annual_water_CNY"]
          +row["annual_electrolyzer_transition_tiebreaker_CNY"])
    ammonia = (supply*(1-share)+row["annual_synthesis_process_CNY"]
               +row["annual_catalyst_CNY"]+row["annual_surrogate_power_tiebreaker_CNY"])
    row.update(annual_H2_subsystem_cost_CNY=h2, annual_ammonia_conversion_cost_CNY=ammonia,
        H2_subsystem_cost_CNY_per_tNH3=h2/row["annual_NH3_t"],
        ammonia_conversion_cost_CNY_per_tNH3=ammonia/row["annual_NH3_t"],
        H2_subsystem_share_of_LCOA=h2/row["annual_total_cost_CNY"],
        economic_allocation_reconciliation_residual_CNY_per_y=h2+ammonia-row["annual_total_cost_CNY"],
        annual_direct_process_load_GWh=energy/1e6,
        park_electricity_supply_cost_CNY_per_kWh_direct_process_load=supply/energy)
    assert abs(row["economic_allocation_reconciliation_residual_CNY_per_y"]) < .1

def terminal_native_audit(model, syn, summary, dispatch):
    curve = model._targeted_native_curves[syn["syn_id"]]
    q = dispatch.NH3_production_t_per_h.to_numpy()
    p = dispatch.synthesis_power_kW.to_numpy()
    h = dispatch.H2_consumption_Nm3_per_h.to_numpy()
    scale = (summary["synthesis_nameplate_kt_per_y"]*1000/8760)/curve["nh3_t_per_h"][-1]
    e = syn["product_conditioning_electric_kWh_per_kgNH3"]*1000
    if syn["syn_id"] == "Ru":
        a,b = np.polyfit(curve["total_power_kW"], curve["nh3_t_per_h"], 1)
        c,d = np.polyfit(curve["total_power_kW"], curve["h2_Nm3_per_h"], 1)
        native = (q-b*scale)/a
        hres = float(np.max(abs(h-(c*native+d*scale))))
    else:
        pwl = model.fe_surrogate_pwl_spec()
        native = np.interp(h/scale, pwl["h2_Nm3_per_h"], pwl["power_kW"])*scale
        hres = 0.
    power_res = float(np.max(abs(p-native-e*q)))
    rated = abs(syn["rated_power_kW"]-float(curve["total_power_kW"][-1])-e*syn["rated_NH3_kg_per_h"]/1000)
    return dict(native_plus_terminal_power_max_residual_kW=power_res,
                native_Ru_hydrogen_max_residual_Nm3_h=hres,
                rated_power_residual_kW=rated,
                passed=power_res < .01 and hres < .001 and rated < .001)


def plot_cost_row(summary, el, syn):
    q=summary['annual_liquid_NH3_component_t']
    row=dict(summary)
    cost_fields={'wind':['annual_wind_CNY'],
      'electrolysis':['annual_electrolyzer_CNY','annual_stack_replacement_CNY','annual_water_CNY'],
      'battery':['annual_battery_CNY'],'storage':['annual_h2_tank_CNY'],
      'synthesis':['annual_synthesis_process_CNY','annual_catalyst_CNY']}
    investments={'wind':['wind_capex_CNY'],'electrolysis':['electrolyzer_capex_CNY'],
      'battery':['battery_capex_CNY'],'storage':['h2_tank_capex_CNY'],
      'synthesis':['synthesis_process_capex_CNY','catalyst_capex_CNY']}
    for name,fields in cost_fields.items():
        row[name+'_cost']=sum(float(summary.get(k,0)) for k in fields)
        row[name+'_LCOA_CNY_per_t']=row[name+'_cost']/q
        row[name+'_investment']=sum(float(summary.get(k,0)) for k in investments[name])
    row['numerical_CNY_per_t']=summary['annual_numerical_tiebreaker_total_CNY']/q
    row['curtailment_percent']=100*summary['curtailment_rate']
    row['hydrogen_storage_Nm3']=summary['h2_tank_capacity_Nm3']
    return row



@contextmanager
def route_scope(el):
    global ELECTROLYZER_LIFE_Y
    previous=ELECTROLYZER_LIFE_Y
    ELECTROLYZER_LIFE_Y=float(el['system_life_y'])
    try:
        yield
    finally:
        ELECTROLYZER_LIFE_Y=previous

def matrix_digest(problem):
    c, integer, bounds, constraints, ix, terms = problem
    digest=hashlib.sha256()
    def add(value):
        value=np.ascontiguousarray(value,dtype=np.float64)
        digest.update(str(value.shape).encode())
        digest.update(value.tobytes())
    for value in [c,integer,bounds.lb,bounds.ub]:
        add(value)
    for con in constraints:
        matrix=con.A.tocsr()
        matrix.sort_indices()
        for value in [matrix.indptr,matrix.indices,matrix.data,con.lb,con.ub]:
            add(value)
    return {'sha256':digest.hexdigest(),'variables':len(c),'constraints':sum(x.A.shape[0] for x in constraints)}




def electrical_rows(matrix, ix):
    electrolyzer = np.asarray(matrix[:, ix.p_el:ix.p_el + ix.n].sum(axis=1)).ravel()
    synthesis = np.asarray(matrix[:, ix.p_syn:ix.p_syn + ix.n].sum(axis=1)).ravel()
    return np.flatnonzero((electrolyzer > 0) & (synthesis > 0))

@dataclass(frozen=True)
class PVMatrix:
    c: np.ndarray
    integrality: np.ndarray
    bounds: Bounds
    constraints: list
    capacity_index: int
    used_start: int
    curtailed_start: int
    hours: int
    base_variable_count: int
    indicator_constraints: np.ndarray
    fixed_share: float | None = None
    capacity_unit_index: int | None = None
    capacity_quantum_kw: float | None = None

def add_pv_block(problem, profile, *, annual_cost_per_kw, capacity_max_kw):
    """Append capacity and hourly PV flows without changing the original columns."""
    c, integers, bounds, constraints, ix, terms = problem
    profile = np.asarray(profile, dtype=float)
    if profile.shape != (ix.n,) or not np.isfinite(profile).all() or (profile < 0).any() or (profile > 1).any():
        raise ValueError('PV profile must match every model hour and be finite in [0, 1]')
    if not np.isfinite(annual_cost_per_kw) or annual_cost_per_kw < 0:
        raise ValueError('Explicit nonnegative annual PV cost is required')
    if not np.isfinite(capacity_max_kw) or capacity_max_kw < 0:
        raise ValueError('Explicit finite nonnegative PV capacity bound is required')
    n, old = (ix.n, len(c))
    extra = 1 + 2 * n
    power_rows = [(index, electrical_rows(item.A.tocsr(), ix)) for index, item in enumerate(constraints)]
    active = [(index, rows) for index, rows in power_rows if len(rows)]
    if len(active) != 1 or len(active[0][1]) != n:
        raise ValueError('Exactly one joint electricity row per model hour is required')
    power_index, rows = active[0]
    base_a = constraints[power_index].A.tocsr()
    for start in (ix.p_el, ix.p_syn):
        if (base_a[rows, start:start + n] - eye(n, format='csr')).nnz:
            raise ValueError('Electricity rows must use chronological unit load coefficients')
    adapted = []
    for index, constraint in enumerate(constraints):
        a = constraint.A.tocsr()
        if index == power_index:
            added = coo_matrix((-np.ones(n), (rows, 1 + np.arange(n))), shape=(a.shape[0], extra)).tocsr()
        else:
            added = csr_matrix((a.shape[0], extra))
        adapted.append(LinearConstraint(hstack([a, added], format='csr'), constraint.lb.copy(), constraint.ub.copy(), keep_feasible=constraint.keep_feasible.copy()))
    hours = np.arange(n)
    balance = coo_matrix((np.r_[-profile, np.ones(2 * n)], (np.tile(hours, 3), np.r_[np.full(n, old), old + 1 + hours, old + 1 + n + hours])), shape=(n, old + extra)).tocsr()
    adapted.append(LinearConstraint(balance, np.zeros(n), np.zeros(n)))
    objective = np.r_[c, annual_cost_per_kw, np.zeros(2 * n)]
    pv_upper = profile * capacity_max_kw
    return PVMatrix(c=objective, integrality=np.r_[integers, np.zeros(extra, dtype=integers.dtype)], bounds=Bounds(np.r_[bounds.lb, np.zeros(extra)], np.r_[bounds.ub, capacity_max_kw, pv_upper, pv_upper], keep_feasible=np.r_[bounds.keep_feasible, np.zeros(extra, dtype=bool)]), constraints=adapted, capacity_index=old, used_start=old + 1, curtailed_start=old + 1 + n, hours=n, base_variable_count=old, indicator_constraints=np.asarray(terms.get('indicator_constraints', ()), dtype=np.int64).reshape(-1, 3).copy())

def fix_pv_share(matrix, ix, share, *, capacity_quantum_kw=None):
    if matrix.fixed_share is not None:
        raise ValueError('A declared PV share must not be applied twice')
    if not math.isfinite(share) or not 0 <= share <= 1:
        raise ValueError('PV nameplate share must be finite in [0, 1]')
    if capacity_quantum_kw is not None and (not math.isfinite(capacity_quantum_kw) or capacity_quantum_kw <= 0):
        raise ValueError('An explicit positive finite PV capacity quantum is required')
    if ix.n != matrix.hours or not 0 <= ix.wind_cap < matrix.base_variable_count:
        raise ValueError('Declared share requires matching original wind and hourly indices')
    constraints = list(matrix.constraints)
    c, integers, bounds = (matrix.c, matrix.integrality, matrix.bounds)
    units = None
    if capacity_quantum_kw is not None:
        units = len(c)
        c = np.r_[c, 0.0]
        integers = np.r_[integers, 1]
        upper = matrix.bounds.ub[matrix.capacity_index]
        if not math.isfinite(upper):
            raise ValueError('PV design quantum requires a finite capacity bound')
        bounds = Bounds(np.r_[bounds.lb, 0.0], np.r_[bounds.ub, math.floor(upper / capacity_quantum_kw)], keep_feasible=np.r_[bounds.keep_feasible, False])
        constraints = [LinearConstraint(hstack([a.A, csr_matrix((a.A.shape[0], 1))], format='csr'), a.lb.copy(), a.ub.copy(), keep_feasible=a.keep_feasible.copy()) for a in constraints]
    rows = [0, 0]
    columns = [matrix.capacity_index, ix.wind_cap]
    values = [1.0 - share, -share]
    count = 1
    if units is not None:
        rows.extend([1, 1])
        columns.extend([matrix.capacity_index, units])
        values.extend([1.0, -capacity_quantum_kw])
        count += 1
    a = coo_matrix((values, (rows, columns)), shape=(count, len(c))).tocsr()
    constraints.append(LinearConstraint(a, np.zeros(count), np.zeros(count)))
    return replace(matrix, c=c, integrality=integers, bounds=bounds, constraints=constraints, fixed_share=float(share), capacity_unit_index=units, capacity_quantum_kw=capacity_quantum_kw)

def _land_row(matrix, ix, *, wind_ha_per_kw, process_areas_ha, site_envelope_ha, pv_ha_per_kw, adapted):
    areas = np.asarray(process_areas_ha, dtype=float)
    scalars = np.array([wind_ha_per_kw, site_envelope_ha, pv_ha_per_kw], dtype=float)
    if areas.ndim != 1 or not len(areas) or (not np.isfinite(areas).all()) or (areas <= 0).any() or (not np.isfinite(scalars).all()) or (scalars <= 0).any():
        raise ValueError('Explicit positive finite land coefficients and envelope are required')
    columns = np.r_[ix.wind_cap, ix.plant_y + np.arange(len(areas))].astype(int)
    if (columns < 0).any() or (columns >= matrix.base_variable_count).any() or len(np.unique(columns)) != len(columns):
        raise ValueError('Land columns must identify distinct original design variables')
    coefficients = np.r_[wind_ha_per_kw, areas]
    if adapted:
        columns = np.r_[columns, matrix.capacity_index]
        coefficients = np.r_[coefficients, pv_ha_per_kw]
    order = np.argsort(columns)
    columns, coefficients = (columns[order], coefficients[order])
    matches = []
    for block, constraint in enumerate(matrix.constraints):
        a = constraint.A.tocsr()
        candidates = a[:, ix.wind_cap].nonzero()[0]
        for row in candidates:
            start, end = a.indptr[row:row + 2]
            if np.array_equal(a.indices[start:end], columns) and np.array_equal(a.data[start:end], coefficients) and np.isneginf(constraint.lb[row]) and (constraint.ub[row] == site_envelope_ha):
                matches.append((block, int(row)))
    if len(matches) != 1:
        raise ValueError('Exactly one unchanged land-envelope row must match the explicit contract')
    block, row = matches[0]
    if block == 1 and any((int(item[0]) == row for item in matrix.indicator_constraints)):
        raise ValueError('Land envelope must not be a conditional constraint')
    return (block, row, areas)

def for_current_solver(matrix):
    """Append new equality rows to block zero; keep conditional row indices intact."""
    if len(matrix.constraints) < 2:
        raise ValueError('The current solver requires equality and upper-bound blocks')
    equality_blocks = []
    for index, constraint in enumerate(matrix.constraints):
        if constraint.A.shape[1] != len(matrix.c) or not np.isfinite(constraint.A.data).all():
            raise ValueError('Every constraint must contain finite full-width coefficients')
        if index == 1:
            if not np.isneginf(constraint.lb).all() or not np.isfinite(constraint.ub).all():
                raise ValueError('The original inequality block must contain only finite upper bounds')
        else:
            if not np.isfinite(constraint.ub).all() or not np.array_equal(constraint.lb, constraint.ub):
                raise ValueError('Every additional block must be equality-only')
            equality_blocks.append(constraint)
    equal = LinearConstraint(vstack([a.A for a in equality_blocks], format='csr'), np.concatenate([a.lb for a in equality_blocks]), np.concatenate([a.ub for a in equality_blocks]), keep_feasible=np.concatenate([a.keep_feasible for a in equality_blocks]))
    upper = matrix.constraints[1]
    for row, column, trigger in matrix.indicator_constraints:
        if not 0 <= row < upper.A.shape[0] or not 0 <= column < len(matrix.c) or matrix.integrality[column] != 1 or (trigger not in (0, 1)) or (matrix.bounds.lb[column] < 0) or (matrix.bounds.ub[column] > 1):
            raise ValueError('Invalid original upper-block indicator metadata')
    return replace(matrix, constraints=[equal, upper])

def as_model_problem(matrix, original_index, original_terms):
    """Expose the full PV tuple and index expected by the existing solve loop."""
    if original_index.size != matrix.base_variable_count or original_index.n != matrix.hours or 'regional_pv' in original_terms:
        raise ValueError('PV index adaptation requires the matching unextended problem')
    indicators = np.asarray(original_terms.get('indicator_constraints', ()), dtype=np.int64).reshape(-1, 3)
    if not np.array_equal(indicators, matrix.indicator_constraints):
        raise ValueError('Original and extended conditional constraints disagree')
    packed = for_current_solver(matrix)
    ix = copy.copy(original_index)
    ix.size = len(packed.c)
    ix.pv_cap = matrix.capacity_index
    ix.pv_injection = matrix.used_start
    ix.pv_curtailment = matrix.curtailed_start
    if matrix.capacity_unit_index is not None:
        ix.pv_units = matrix.capacity_unit_index
    terms = dict(original_terms)
    terms['indicator_constraints'] = packed.indicator_constraints.tolist()
    terms['regional_pv'] = dict(base_variable_count=matrix.base_variable_count, full_variable_count=ix.size, capacity_index=ix.pv_cap, injection_start=ix.pv_injection, curtailment_start=ix.pv_curtailment, fixed_share=matrix.fixed_share, capacity_unit_index=matrix.capacity_unit_index, capacity_quantum_kw=matrix.capacity_quantum_kw, regional_model_ready=False, scope='Solver tuple and warm-start indexing only; runtime and output integration remain required')
    return (packed.c, packed.integrality, packed.bounds, packed.constraints, ix, terms)

def audit_witness(matrix, vector):
    x = np.asarray(vector, dtype=float)
    if x.shape != matrix.c.shape or not np.isfinite(x).all():
        return {'passed': False, 'reason': 'Invalid or nonfinite full witness'}
    integers = np.asarray(matrix.integrality)
    if not np.isin(integers, [0, 1]).all():
        raise ValueError('Unsupported integrality type in the regional audit')
    indicators = np.asarray(matrix.indicator_constraints, dtype=np.int64).reshape(-1, 3)
    for row, col, trigger in indicators:
        if len(matrix.constraints) < 2 or not 0 <= row < matrix.constraints[1].A.shape[0] or (not 0 <= col < len(x)) or (integers[col] != 1) or (trigger not in (0, 1)) or (matrix.bounds.lb[col] < 0) or (matrix.bounds.ub[col] > 1):
            raise ValueError('Invalid current-model conditional constraint metadata')
    constraint_checks = []
    for block, constraint in enumerate(matrix.constraints):
        activity = constraint.A @ x
        if not np.isfinite(activity).all() or np.isnan(constraint.lb).any() or np.isnan(constraint.ub).any():
            return {'passed': False, 'reason': 'Nonfinite matrix activity or invalid row bound'}
        violation = np.maximum.reduce([np.zeros_like(activity), constraint.lb - activity, activity - constraint.ub])
        active = np.ones(len(activity), dtype=bool)
        if block == 1:
            for row, col, trigger in indicators:
                active[row] = abs(x[col] - trigger) < 1e-06
        violation[~active] = 0.0
        lower = np.where(np.isfinite(constraint.lb), np.abs(constraint.lb), 0.0)
        upper = np.where(np.isfinite(constraint.ub), np.abs(constraint.ub), 0.0)
        tolerance = 0.001 + 1e-08 * np.maximum(lower, upper)
        constraint_checks.append(dict(block=block, rows=len(activity), active_rows=int(active.sum()), max_violation=float(violation.max(initial=0.0)), count_above_tolerance=int(np.count_nonzero(violation > tolerance))))
    bound_error = float(np.maximum.reduce([np.zeros_like(x), matrix.bounds.lb - x, x - matrix.bounds.ub]).max(initial=0.0))
    integral = integers == 1
    integer_error = float(np.abs(x[integral] - np.rint(x[integral])).max(initial=0.0))
    passed = bound_error <= 0.001 and integer_error <= 1e-06 and all((item['count_above_tolerance'] == 0 for item in constraint_checks))
    return dict(passed=passed, variables=len(x), constraint_checks=constraint_checks, bound_max_violation=bound_error, integer_max_violation=integer_error, indicator_count=len(indicators), row_tolerance='1e-3 + 1e-8 * max(abs(finite row bounds)); active indicators only', bound_tolerance=0.001, integer_tolerance=1e-06, scope='Every original and PV matrix row, variable bound and integer; no solve or projection')

def extract_plant_output(vector, ix, *, synthesis, nameplate_kt_y):
    if hasattr(ix, 'storage_charge') or hasattr(ix, 'soec_heater_cap'):
        raise ValueError('Explicit compressor/heater scenarios require their own physical adapter')
    x = np.asarray(vector, dtype=float)
    if len(x) < ix.size or not np.isfinite(x).all():
        raise ValueError('Invalid plant witness')
    columns = {'bat_ch': 'battery_charge_kW', 'bat_dis': 'battery_discharge_kW', 'bat_soc': 'battery_SOC_kWh', 'p_el': 'electrolyzer_power_kW', 'p_syn': 'synthesis_power_kW', 'h2_soc': 'H2_storage_Nm3', 'el_on': 'electrolyzer_online_modules', 'el_start': 'electrolyzer_modules_started', 'el_stop': 'electrolyzer_modules_stopped', 'h2_prod': 'H2_production_Nm3_per_h', 'water_use': 'water_consumption_m3_per_h', 'nh3_prod': 'NH3_production_t_per_h', 'h2_use': 'H2_consumption_Nm3_per_h'}
    frame = pd.DataFrame({'hour': np.arange(1, ix.n + 1), **{column: x[getattr(ix, key):getattr(ix, key) + ix.n].copy() for key, column in columns.items()}})
    q = frame['NH3_production_t_per_h']
    terminal = synthesis['targeted_terminal']
    frame['terminal_power_kW'] = q * terminal['terminal_electricity_kWh_per_t_gross']
    frame['upstream_synthesis_power_kW'] = frame['synthesis_power_kW'] - frame['terminal_power_kW']
    frame['liquid_NH3_component_t_per_h'] = q * terminal['liquid_recovery_fraction']
    frame['terminal_makeup_water_m3_per_h'] = q * terminal['water_m3_per_t_gross']
    frame['terminal_variable_cost_CNY_per_h'] = q * terminal['variable_cost_CNY_per_t_gross']
    capacities = dict(battery_kWh=float(x[ix.bat_cap]), hydrogen_Nm3=float(x[ix.h2_cap]), electrolyzer_kW=float(x[ix.el_cap]), synthesis_kt_y=float(x[ix.plant_y]) * nameplate_kt_y)
    return (frame, capacities)

def add_shared_land(pv, ix, land_options, permanent):
    _land_row(pv, ix, **land_options, adapted=False)
    cols = [ix.wind_cap, pv.capacity_index, ix.plant_y]
    vals = [permanent, land_options['pv_ha_per_kw'], land_options['process_areas_ha'][0]]
    row = csr_matrix((vals, ([0] * 3, cols)), shape=(1, len(pv.c)))
    old = pv.constraints[1]
    constraints = list(pv.constraints)
    constraints[1] = LinearConstraint(vstack([old.A, row], format='csr'), np.r_[old.lb, -np.inf], np.r_[old.ub, land_options['site_envelope_ha']])
    return replace(pv, constraints=constraints)

def shared_land_audit(pv, x, ix, land_options, permanent):
    wind = float(x[ix.wind_cap] * land_options['wind_ha_per_kw'])
    footprint = float(x[ix.wind_cap] * permanent)
    solar = float(x[pv.capacity_index] * land_options['pv_ha_per_kw'])
    process = float(x[ix.plant_y] * land_options['process_areas_ha'][0])
    total = max(wind, footprint + solar) + process
    envelope = land_options['site_envelope_ha']
    return dict(passed=bool(total <= envelope + 1e-07), wind_spacing_ha=wind, wind_permanent_ha=footprint, pv_array_ha=solar, process_site_ha=process, total_accounted_land_ha=total, site_envelope_ha=envelope, pv_fits_wind_spare_area=bool(solar <= wind - footprint + 1e-07) if wind > 0 else None, formula='max(wind_spacing, wind_permanent + PV_array) + process', physical_layout_verified=False, incremental_PV_rent_removed=False)

def continuous_base_costs(model, x, ix, terms, el, syn):
    assert ix.n_plants == 1 and abs(x[ix.plant_y] - 1.0) < 1e-06
    assert terms['targeted_terminal_contract'] == syn['targeted_terminal']
    total = lambda start: float(x[start:start + ix.n].sum())
    annual = model.annualized_capital_factor
    gross = total(ix.nh3_prod)
    process = model.process_costs(syn, 475.0)
    c = dict(annual_wind_CNY=float(x[ix.wind_cap]) * (model.WIND_CAPEX_CNY_PER_KW * (annual(model.WIND_LIFE_Y) + model.WIND_FIXED_OM_RATIO) + model.land_only.annual_wind_land_cost_cny_per_kw()), annual_electrolyzer_CNY=float(x[ix.el_cap]) * el['installed_capex_CNY_per_kW'] * (annual(model.ELECTROLYZER_LIFE_Y) + el['fixed_om_ratio']), annual_battery_CNY=float(x[ix.bat_cap]) * model.BESS_CAPEX_CNY_PER_KWH * (annual(model.BESS_LIFE_Y) + model.BESS_FIXED_OM_RATIO), annual_h2_tank_CNY=float(x[ix.h2_cap]) * model.H2_TANK_CAPEX_CNY_PER_NM3 * (annual(model.H2_TANK_LIFE_Y) + model.H2_TANK_FIXED_OM_RATIO), annual_synthesis_process_CNY=float(process['annual_process_CNY']) + gross * syn['targeted_terminal']['variable_cost_CNY_per_t_gross'], annual_catalyst_CNY=float(process['annual_catalyst_CNY']), annual_stack_replacement_CNY=total(ix.p_el) * terms['stack_replacement_cny_per_kwh'], annual_water_CNY=total(ix.water_use) * model.WATER_PRICE_CNY_PER_M3, annual_battery_tiebreaker_CNY=(total(ix.bat_ch) + total(ix.bat_dis)) * model.BESS_FLOW_TIEBREAKER_CNY_PER_KWH, annual_electrolyzer_transition_tiebreaker_CNY=(total(ix.el_start) + total(ix.el_stop)) * model.ELECTROLYZER_TRANSITION_TIEBREAKER_CNY_PER_MODULE, annual_surrogate_power_tiebreaker_CNY=0.0)
    return dict(route='Ru+ALK', annual_NH3_t=gross, annual_total_cost_CNY=sum(c.values()), **c)

def remove_battery_circulation(x, ix, eta_c, eta_d):
    """Construct a feasible no-circulation witness, keeping every SOC unchanged.

    This model permits curtailment, with no utilization equality. Netting at the
    internal stored-energy boundary releases extra bus spill; it does not turn
    losses into delivered electricity. Preserve the solver vector separately.
    """
    y = x.copy()
    ch = x[ix.bat_ch:ix.bat_ch + ix.n]
    dis = x[ix.bat_dis:ix.bat_dis + ix.n]
    internal = eta_c * ch - dis / eta_d
    y[ix.bat_ch:ix.bat_ch + ix.n] = np.maximum(internal, 0) / eta_c
    y[ix.bat_dis:ix.bat_dis + ix.n] = np.maximum(-internal, 0) * eta_d
    new_ch = y[ix.bat_ch:ix.bat_ch + ix.n]
    new_dis = y[ix.bat_dis:ix.bat_dis + ix.n]
    np.testing.assert_allclose(eta_c * new_ch - new_dis / eta_d, internal, atol=1e-08)
    released = ch - dis - (new_ch - new_dis)
    assert released.min() >= -1e-07
    return (y, dict(method='SOC-preserving removal of simultaneous charging/discharging', raw_max_simultaneous_kW=float(np.minimum(ch, dis).max()), final_max_simultaneous_kW=float(np.minimum(new_ch, new_dis).max()), extra_curtailment_MWh=float(released.sum() / 1000), SOC_and_process_flows_unchanged=True, raw_solver_witness_retained=True, final_witness_is_postprocessed=True, requires_free_curtailment=True))

def evaluate(problem, vector, annual_cost, gross_output, lcoa_parameter):
    c, integrality, bounds, constraints, ix, terms = problem
    if len(vector) != len(c) or not np.isfinite(vector).all():
        raise ValueError('The witness has missing or non-finite variables')
    checks = []
    for block, constraint in enumerate(constraints):
        ax = constraint.A @ vector
        violation = np.maximum.reduce([np.zeros_like(ax), constraint.lb - ax, ax - constraint.ub])
        active = np.ones(len(ax), dtype=bool)
        if block == 1:
            for rid, col, trigger in terms.get('indicator_constraints', []):
                active[rid] = abs(vector[col] - trigger) < 1e-06
        violation[~active] = 0.0
        scale = np.maximum(np.where(np.isfinite(constraint.lb), abs(constraint.lb), 0.0), np.where(np.isfinite(constraint.ub), abs(constraint.ub), 0.0))
        checks.append(dict(block=block, rows=len(ax), active_rows=int(active.sum()), max_violation=float(violation.max(initial=0)), count_above_tolerance=int(np.count_nonzero(violation > 0.001 + 1e-08 * scale))))
    bound_error = float(np.maximum.reduce([np.zeros_like(vector), bounds.lb - vector, vector - bounds.ub]).max(initial=0))
    mask = np.asarray(integrality) == 1
    integer_error = float(np.abs(vector[mask] - np.rint(vector[mask])).max(initial=0))
    objective_cost = float(c @ vector + lcoa_parameter * gross_output)
    cost_error = objective_cost - annual_cost
    cost_tolerance = max(0.1, abs(annual_cost) * 1e-08)
    return dict(variables=len(vector), constraints=checks, bound_max_violation=bound_error, integer_max_violation=integer_error, annual_cost_from_current_objective_CNY=objective_cost, annual_cost_from_stored_components_CNY=annual_cost, cost_residual_CNY=cost_error, cost_tolerance_CNY=cost_tolerance, passed=bool(bound_error <= 0.001 and integer_error <= 1e-06 and (abs(cost_error) <= cost_tolerance) and all((x['count_above_tolerance'] == 0 for x in checks))), scope='All current rows, active indicators, variable bounds, integrality and annual objective')

def reprice(ledger, multipliers):
    slopes = ledger['annual_response_CNY']
    if set(multipliers) - set(slopes):
        raise ValueError('Unknown cost factor; the discount rate is fixed')
    output = float(ledger['annual_liquid_NH3_component_t'])
    if not np.isfinite(output) or output <= 0:
        raise ValueError('Recovered product must be finite and positive')
    values = np.asarray(ledger['baseline_LCOA_CNY_per_t'], dtype=float)
    for name, multiplier in multipliers.items():
        multiplier = np.asarray(multiplier, dtype=float)
        if not np.isfinite(multiplier).all() or np.any(multiplier <= 0):
            raise ValueError('Cost multipliers must be finite and positive')
        values = values + (multiplier - 1.0) * slopes[name] / output
    return float(values) if values.ndim == 0 else values

def monte_carlo(ledger, count, seed):
    if count < 1:
        raise ValueError('A positive sample count is required')
    rng = np.random.default_rng(seed)
    factors = list(ledger['annual_response_CNY'])
    samples = rng.triangular(0.8, 1.0, 1.2, size=(count, len(factors)))
    table = pd.DataFrame(samples, columns=factors)
    table['LCOA_CNY_per_t'] = reprice(ledger, {k: table[k].to_numpy() for k in factors})
    return table

@dataclass(frozen=True)
class Design:
    point: float
    lower_bound: float
    intercept: float
    slope: float
    witness: str
    feasible_set_sha256: str

    def upper(self, point):
        return self.intercept + self.slope * point

    def validate(self):
        values = (self.point, self.lower_bound, self.intercept, self.slope)
        if not all((math.isfinite(v) for v in values)):
            raise ValueError('A parametric certificate requires finite values')
        if not self.witness or not self.feasible_set_sha256:
            raise ValueError('A current-model witness and feasible-set hash are required')
        if self.upper(self.point) <= 0 or self.lower_bound > self.upper(self.point) + 1e-06:
            raise ValueError('Invalid cost bounds')

def certificates(grid, designs, tolerance):
    if not 0 < tolerance < 1:
        raise ValueError('Invalid certificate tolerance')
    grid = sorted(set((float(x) for x in grid)))
    if not grid or any((not math.isfinite(x) for x in grid)):
        raise ValueError('The grid must be finite and nonempty')
    for design in designs:
        design.validate()
    if len({d.feasible_set_sha256 for d in designs}) > 1:
        raise ValueError('Cannot combine designs with different physical feasible sets')
    ordered = sorted(designs, key=lambda d: d.point)
    rows = []
    for point in grid:
        if not ordered:
            rows.append(dict(point=point, certified=False, lower_bound=None, upper_bound=None, relative_gap=None, witness=None))
            continue
        best = min(ordered, key=lambda d: d.upper(point))
        upper = best.upper(point)
        lower_candidates = [d.lower_bound for d in ordered if abs(d.point - point) < 1e-12]
        for left in ordered:
            for right in ordered:
                if left.point <= point <= right.point and right.point > left.point:
                    weight = (point - left.point) / (right.point - left.point)
                    lower_candidates.append((1 - weight) * left.lower_bound + weight * right.lower_bound)
        lower = max(lower_candidates) if lower_candidates else None
        if lower is not None and lower > upper + 1e-05:
            raise ValueError('Cross-point bounds conflict; inspect the cost or physical fingerprint')
        gap = max(0.0, (upper - lower) / upper) if lower is not None and upper > 0 else None
        rows.append(dict(point=point, lower_bound=lower, upper_bound=upper, relative_gap=gap, certified=gap is not None and gap <= tolerance, witness=best.witness, method='current-model feasible design and concavity lower bound'))
    return rows

def configure_costs(model, el, syn, factor, multiplier):
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise ValueError('Cost multiplier must be finite and positive')
    if factor == 'wind_capex':
        model.WIND_CAPEX_CNY_PER_KW *= multiplier
    elif factor == 'electrolyzer_capex':
        el['installed_capex_CNY_per_kW'] *= multiplier
        if hasattr(model, 'REVIEW_SOEC_HEATER_CAPEX_CNY_PER_KW'):
            model.REVIEW_SOEC_HEATER_CAPEX_CNY_PER_KW *= multiplier
    elif factor == 'battery_capex':
        model.BESS_CAPEX_CNY_PER_KWH *= multiplier
    elif factor == 'hydrogen_capex':
        model.H2_TANK_CAPEX_CNY_PER_NM3 *= multiplier
        if hasattr(model, 'REVIEW_STORAGE_COMPRESSOR_CAPEX_CNY_PER_KW'):
            model.REVIEW_STORAGE_COMPRESSOR_CAPEX_CNY_PER_KW *= multiplier
    elif factor == 'fixed_om':
        el['fixed_om_ratio'] *= multiplier
        for name in ('WIND_FIXED_OM_RATIO', 'BESS_FIXED_OM_RATIO', 'H2_TANK_FIXED_OM_RATIO', 'SYNTHESIS_FIXED_OM_RATIO'):
            setattr(model, name, getattr(model, name) * multiplier)
        syn['targeted_terminal']['terminal_fixed_OM_fraction'] *= multiplier
        if hasattr(model, 'REVIEW_STORAGE_COMPRESSOR_FIXED_OM'):
            model.REVIEW_STORAGE_COMPRESSOR_FIXED_OM *= multiplier
        if hasattr(model, 'REVIEW_SOEC_HEATER_FIXED_OM'):
            model.REVIEW_SOEC_HEATER_FIXED_OM *= multiplier
    elif factor == 'synthesis_capex':
        original = model.process_costs

        def scaled(synthesis, capacity):
            values = original(synthesis, capacity)
            capital_delta = (multiplier - 1) * values['process_equipment_capex_CNY']
            annual_delta = (multiplier - 1) * values['annual_process_equipment_CNY']
            for key in ('process_capex_CNY', 'process_equipment_capex_CNY'):
                values[key] += capital_delta
            for key in ('annual_process_CNY', 'annual_process_equipment_CNY', 'annual_total_CNY'):
                values[key] += annual_delta
            for key in ('targeted_terminal_capital_CNY', 'targeted_terminal_fixed_annual_CNY'):
                if key in values:
                    values[key] *= multiplier
            return values
        model.process_costs = scaled
    else:
        raise ValueError('Unknown economic factor: ' + str(factor))

def cost_response_slopes(model, el, syn, summary, dispatch, factor=None, multiplier=1.0):
    """Annual derivatives with respect to a dimensionless baseline multiplier."""
    process = model.process_costs(syn, summary['synthesis_nameplate_kt_per_y'])
    wind_capital = summary['wind_capacity_MW'] * 1000 * model.WIND_CAPEX_CNY_PER_KW
    el_capital = summary['electrolyzer_capacity_MW'] * 1000 * el['installed_capex_CNY_per_kW']
    bat_capital = summary['battery_capacity_MWh'] * 1000 * model.BESS_CAPEX_CNY_PER_KWH
    tank_capital = summary['h2_tank_capacity_Nm3'] * model.H2_TANK_CAPEX_CNY_PER_NM3
    terminal = syn.get('targeted_terminal', {})
    terminal_capital = process.get('targeted_terminal_capital_CNY', 0.0)
    fom = wind_capital * model.WIND_FIXED_OM_RATIO + el_capital * el['fixed_om_ratio'] + bat_capital * model.BESS_FIXED_OM_RATIO + tank_capital * model.H2_TANK_FIXED_OM_RATIO + (process['process_equipment_capex_CNY'] - terminal_capital) * model.SYNTHESIS_FIXED_OM_RATIO + terminal_capital * terminal.get('terminal_fixed_OM_fraction', 0.0) + summary.get('annual_storage_compressor_fixed_om_CNY', 0.0) + summary.get('annual_soec_heater_fixed_om_CNY', 0.0)
    slopes = dict(wind_capex=wind_capital * (model.annualized_capital_factor(model.WIND_LIFE_Y) + model.WIND_FIXED_OM_RATIO), electrolyzer_capex=el_capital * (model.annualized_capital_factor(el['system_life_y']) + el['fixed_om_ratio']) + summary['annual_stack_replacement_CNY'] + summary.get('annual_soec_heater_CNY', 0.0), battery_capex=bat_capital * (model.annualized_capital_factor(model.BESS_LIFE_Y) + model.BESS_FIXED_OM_RATIO), hydrogen_capex=tank_capital * (model.annualized_capital_factor(model.H2_TANK_LIFE_Y) + model.H2_TANK_FIXED_OM_RATIO) + summary.get('annual_storage_compressor_CNY', 0.0), synthesis_capex=process['annual_process_equipment_CNY'], fixed_om=fom)
    if factor:
        slopes[factor] /= multiplier
    return slopes

TERMINAL_CAPACITY_TABLE = {'Ru_100': {'installed_terminal_capital_CNY': 19467485.865100116, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_100': {'installed_terminal_capital_CNY': 25738933.729228888, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_125': {'installed_terminal_capital_CNY': 20285557.0191488, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_125': {'installed_terminal_capital_CNY': 28167470.69549008, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_150': {'installed_terminal_capital_CNY': 21174960.62157114, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_150': {'installed_terminal_capital_CNY': 30633297.750402, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_175': {'installed_terminal_capital_CNY': 22092372.199236933, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_175': {'installed_terminal_capital_CNY': 33072841.32915067, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_200': {'installed_terminal_capital_CNY': 23018297.25635091, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_200': {'installed_terminal_capital_CNY': 35482009.458317645, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_225': {'installed_terminal_capital_CNY': 23943270.10394622, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_225': {'installed_terminal_capital_CNY': 37861134.21542951, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_250': {'installed_terminal_capital_CNY': 24862523.277972564, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_250': {'installed_terminal_capital_CNY': 40212113.87544239, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_275': {'installed_terminal_capital_CNY': 25773660.156737972, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_275': {'installed_terminal_capital_CNY': 42537317.675469674, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_300': {'installed_terminal_capital_CNY': 26675544.098529458, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_300': {'installed_terminal_capital_CNY': 44839148.838481866, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_325': {'installed_terminal_capital_CNY': 27567731.482391443, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_325': {'installed_terminal_capital_CNY': 47119873.38890671, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_350': {'installed_terminal_capital_CNY': 28450167.11866352, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_350': {'installed_terminal_capital_CNY': 49381561.44991446, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_375': {'installed_terminal_capital_CNY': 29393177.33551647, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_375': {'installed_terminal_capital_CNY': 51626076.91683247, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_400': {'installed_terminal_capital_CNY': 30343848.46098727, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_400': {'installed_terminal_capital_CNY': 53855087.41444122, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_425': {'installed_terminal_capital_CNY': 31285108.29022182, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_425': {'installed_terminal_capital_CNY': 56070081.88688945, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_450': {'installed_terminal_capital_CNY': 32217387.25527134, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_450': {'installed_terminal_capital_CNY': 58272390.09281219, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_475': {'installed_terminal_capital_CNY': 33141115.52700142, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_475': {'installed_terminal_capital_CNY': 60463201.480717756, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_500': {'installed_terminal_capital_CNY': 34056712.31849299, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_500': {'installed_terminal_capital_CNY': 62643582.42617419, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_525': {'installed_terminal_capital_CNY': 34964579.95893679, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_525': {'installed_terminal_capital_CNY': 64814491.520821154, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_550': {'installed_terminal_capital_CNY': 35865100.88339042, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_550': {'installed_terminal_capital_CNY': 66976792.92803489, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_575': {'installed_terminal_capital_CNY': 36758636.40654063, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_575': {'installed_terminal_capital_CNY': 69131267.95836662, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_600': {'installed_terminal_capital_CNY': 37645526.57996557, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_600': {'installed_terminal_capital_CNY': 71278625.06579784, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_625': {'installed_terminal_capital_CNY': 38526090.694838345, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_625': {'installed_terminal_capital_CNY': 73419508.47100979, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_650': {'installed_terminal_capital_CNY': 39405850.12661251, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_650': {'installed_terminal_capital_CNY': 75559800.90855871, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_675': {'installed_terminal_capital_CNY': 40281430.65204616, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_675': {'installed_terminal_capital_CNY': 77696241.16995838, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_700': {'installed_terminal_capital_CNY': 41151559.296636395, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_700': {'installed_terminal_capital_CNY': 79827855.7830485, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_725': {'installed_terminal_capital_CNY': 42016479.502885036, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_725': {'installed_terminal_capital_CNY': 81955094.10680477, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_750': {'installed_terminal_capital_CNY': 42876419.689782955, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_750': {'installed_terminal_capital_CNY': 84078370.08750097, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_775': {'installed_terminal_capital_CNY': 43731594.31229792, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_775': {'installed_terminal_capital_CNY': 86198065.89591858, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_800': {'installed_terminal_capital_CNY': 44582204.86641868, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_800': {'installed_terminal_capital_CNY': 88314535.11410214, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_825': {'installed_terminal_capital_CNY': 45428440.83227017, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_825': {'installed_terminal_capital_CNY': 90428105.53544068, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_850': {'installed_terminal_capital_CNY': 46270480.55256168, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_850': {'installed_terminal_capital_CNY': 92539081.63198288, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_875': {'installed_terminal_capital_CNY': 47108492.046619, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_875': {'installed_terminal_capital_CNY': 94647746.73460917, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_900': {'installed_terminal_capital_CNY': 47942633.762075804, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_900': {'installed_terminal_capital_CNY': 96754364.96474834, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_925': {'installed_terminal_capital_CNY': 48773055.26736177, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_925': {'installed_terminal_capital_CNY': 98859182.95051049, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_950': {'installed_terminal_capital_CNY': 49599897.88868106, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_950': {'installed_terminal_capital_CNY': 100962431.35524204, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_975': {'installed_terminal_capital_CNY': 50423295.29540724, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_975': {'installed_terminal_capital_CNY': 103064326.2424178, 'terminal_fixed_OM_fraction': 0.03}, 'Ru_1000': {'installed_terminal_capital_CNY': 51243374.03784316, 'terminal_fixed_OM_fraction': 0.03}, 'Fe_1000': {'installed_terminal_capital_CNY': 105165070.29735008, 'terminal_fixed_OM_fraction': 0.03}}

import argparse as exergy_argparse
import copy as exergy_copy
import hashlib as exergy_hashlib
import json as exergy_json
import math as exergy_math
from pathlib import Path as exergy_Path
from typing import Mapping as exergy_Mapping
import numpy as exergy_np
import pandas as exergy_pd
exergy_BOUNDARY_ID = 'Ru_ALK_Fig06d_combined_N2_supply_20260923_Ar12'
exergy_HOURS_PER_YEAR = 8760
exergy_CHEMICAL_EXERGY_KJ_PER_MOL = {'H2O_liquid': 0.9, 'H2': 236.1, 'N2': 0.72, 'O2': 3.97, 'Ar': 12.0, 'NH3': 337.9}
exergy_MOLAR_MASS_KG_PER_KMOL = {'H2O_liquid': 18.01528, 'H2': 2.01588, 'N2': 28.0134, 'O2': 31.9988, 'Ar': 39.948, 'NH3': 17.03052}
exergy_RU_RATED_NH3_KG_PER_H = 33.3056072
exergy_RU_N2_FEED_KG_PER_H = 28.0134716
exergy_NORMAL_MOLAR_VOLUME_NM3_PER_KMOL = 22.414
exergy_DRY_AIR_N2_MASS_FRACTION = 0.7552
exergy_DRY_AIR_O2_MASS_FRACTION = 0.2314
exergy_ASU_KWH_PER_KG_N2 = 0.11
exergy_N2_PRESSURE_MOLAR_MASS_KG_PER_KMOL = 28.01348
exergy_GAS_CONSTANT_KJ_PER_KMOL_K = 8.314462618
exergy_REFERENCE_TEMPERATURE_K = 298.15
exergy_N2_INLET_PRESSURE_BARA = 1.01325
exergy_N2_DELIVERY_PRESSURE_BARA = 30.0
exergy_ISOTHERMAL_EFFICIENCY = 0.75
exergy_FLOW_ATOL_GWH = 1e-08
exergy_REQUIRED_SUMMARY = ('route', 'annual_NH3_t', 'annual_H2_production_Nm3', 'annual_water_m3')
exergy_REQUIRED_DISPATCH = ('hour', 'wind_generation_kW', 'curtailment_kW', 'battery_charge_kW', 'battery_discharge_kW', 'electrolyzer_power_kW', 'synthesis_power_kW', 'H2_production_Nm3_per_h', 'H2_consumption_Nm3_per_h', 'NH3_production_t_per_h', 'water_consumption_m3_per_h', 'liquid_NH3_component_t_per_h')
exergy_NODE_LABELS = {'n01': 'Wind', 'n02': 'Electricity', 'n03': 'Battery', 'n04': 'Water', 'n05': 'Electrolysis', 'n06': 'Hydrogen', 'n07': 'Hydrogen storage', 'n08': 'ASU + N₂ comp.', 'n10': 'Nitrogen\n(30 bar)', 'n11': 'NH₃ synthesis', 'n12': 'Unrecovered NH₃', 'n13': 'Recovered NH₃', 'n14': 'O₂ coproduct', 'n15': 'Drainage', 'n16': 'Purge', 'n17': 'Balance residual', 'n18': 'Curtailment', 'n19': 'Other gases'}
exergy_NODE_POSITIONS = {'n01': (123.5, 596.6454971040573), 'n02': (578.5, 567.6035103049339), 'n03': (935.9451904296875, 1148.41862586086), 'n04': (124.03642272949219, 1163.2779957165394), 'n05': (1113.5, 554.027789872488), 'n06': (1623.5, 413.5427467285583), 'n07': (1963.7261962890625, 713.5229964606606), 'n08': (1630.5, 1327.375659781517), 'n10': (2055.168212890625, 1204.8724133130338), 'n11': (2303.5, 426.7535495734179), 'n12': (2903.5, 1435.0), 'n13': (2903.5, 399.01741259222786), 'n14': (2903.5, 1073.3577321253697), 'n15': (2903.7431640625, 1155.3949134179848), 'n16': (2903.5, 994.1277143328871), 'n17': (2903.5, 750.8462263385645), 'n18': (2903.5, 1327.375659781517), 'n19': (2903.5, 1206.8724133130338)}
exergy_FLOW_SPEC = (('f01', 'n01', 'n02', 'electricity', 'Direct wind electricity'), ('f06', 'n01', 'n03', 'electricity', 'Battery charge'), ('f02', 'n01', 'n18', 'electricity', 'Curtailed wind electricity'), ('f07', 'n03', 'n02', 'electricity', 'Battery discharge'), ('f10', 'n03', 'n17', 'balance_residual', 'Battery balance difference'), ('f03', 'n02', 'n05', 'electricity', 'Electrolysis electricity'), ('f04', 'n02', 'n08', 'electricity', 'ASU plus N2 precompression electricity; reassigned once'), ('f05', 'n02', 'n11', 'electricity', 'Other synthesis electricity; mixed-gas compression retained'), ('f11', 'n04', 'n05', 'water', 'Feed-water chemical exergy'), ('f12', 'n05', 'n06', 'hydrogen', 'Hydrogen chemical exergy'), ('f16', 'n05', 'n14', 'oxygen', 'Electrolytic oxygen chemical exergy'), ('f17', 'n05', 'n15', 'water', 'Residual water chemical exergy'), ('f27', 'n05', 'n17', 'balance_residual', 'Electrolysis balance difference'), ('f14', 'n06', 'n07', 'hydrogen', 'Hydrogen storage annual throughput'), ('f13', 'n06', 'n11', 'hydrogen', 'Direct hydrogen to synthesis'), ('f15', 'n07', 'n11', 'hydrogen', 'Hydrogen storage to synthesis'), ('f18', 'n08', 'n10', 'nitrogen', 'N2 chemical plus estimated pressure exergy at 30 bara'), ('f08', 'n08', 'n14', 'oxygen', 'ASU oxygen chemical exergy'), ('f21', 'n08', 'n19', 'argon_other', 'Other gases, inherited Ar=12 kJ/mol approximation'), ('f28', 'n08', 'n17', 'balance_residual', 'ASU plus N2 precompression balance difference'), ('f19', 'n10', 'n11', 'nitrogen', 'Delivered N2, same chemical-plus-pressure boundary'), ('f22', 'n11', 'n13', 'ammonia', 'Recovered NH3 chemical exergy'), ('f23', 'n11', 'n16', 'purge', 'Purge and unconverted gas chemical exergy'), ('f29', 'n11', 'n17', 'balance_residual', 'Other synthesis balance difference'), ('f24', 'n11', 'n12', 'ammonia', 'Unrecovered NH3 chemical exergy'))
exergy_CARRIER_COLORS = {'electricity': '#84a7cc', 'hydrogen': '#319D92', 'nitrogen': '#D1A52A', 'oxygen': '#55A6C8', 'water': '#4EA5D9', 'ammonia': '#C44E52', 'argon_other': '#9FA9B2', 'purge': '#9AA2A8', 'balance_residual': '#BEC2C5'}

def exergy__check(condition, message):
    if not condition:
        raise ValueError(message)

def exergy_chemical_exergy_gwh(species, mass_kt):
    """Inherited standard chemical exergy: kt/y -> GWh_ex/y."""
    mass_kg = float(mass_kt) * 1000000.0
    kg_per_mol = exergy_MOLAR_MASS_KG_PER_KMOL[species] / 1000.0
    return mass_kg / kg_per_mol * exergy_CHEMICAL_EXERGY_KJ_PER_MOL[species] / 3600000000.0

def exergy__validated_dispatch(summary, dispatch):
    missing = [key for key in exergy_REQUIRED_SUMMARY if key not in summary]
    exergy__check(not missing, f'Missing required summary inputs: {missing}')
    exergy__check(summary['route'] == 'Ru+ALK', 'This fixed-boundary API supports Ru+ALK only')
    missing = [key for key in exergy_REQUIRED_DISPATCH if key not in dispatch.columns]
    exergy__check(not missing, f'Missing required dispatch inputs: {missing}; retained flows are never used')
    exergy__check(len(dispatch) == exergy_HOURS_PER_YEAR, 'Expected all 8760 one-hour observations')
    d = dispatch.loc[:, list(exergy_REQUIRED_DISPATCH)].apply(exergy_pd.to_numeric, errors='raise')
    exergy__check(exergy_np.isfinite(d.to_numpy()).all(), 'Non-finite dispatch values')
    exergy__check(exergy_np.array_equal(d.hour.to_numpy(), exergy_np.arange(1, exergy_HOURS_PER_YEAR + 1)), 'Dispatch hours must be ordered and unique: 1..8760')
    exergy__check((d.drop(columns='hour').to_numpy() >= -1e-06).all(), 'Negative dispatch inputs')
    for column in ('grid_import_kW', 'grid_export_kW', 'pv_generation_kW'):
        if column in dispatch:
            extra = exergy_pd.to_numeric(dispatch[column], errors='raise').to_numpy()
            exergy__check(exergy_np.isfinite(extra).all() and exergy_np.max(exergy_np.abs(extra)) <= 1e-06, f'{column} is outside the authoritative wind-only boundary')
    totals = {}
    for key, column in (('annual_NH3_t', 'NH3_production_t_per_h'), ('annual_H2_production_Nm3', 'H2_production_Nm3_per_h'), ('annual_water_m3', 'water_consumption_m3_per_h')):
        expected = float(summary[key])
        actual = float(d[column].sum())
        exergy__check(exergy_math.isfinite(expected) and expected > 0, f'Invalid summary total: {key}')
        exergy__check(exergy_math.isclose(actual, expected, rel_tol=1e-10, abs_tol=0.0001), f'Summary/dispatch mismatch for {key}: {expected} != {actual}')
        totals[key] = dict(summary=expected, dispatch=actual, difference=actual - expected)
    power = d.wind_generation_kW + d.battery_discharge_kW - d.electrolyzer_power_kW - d.synthesis_power_kW - d.battery_charge_kW - d.curtailment_kW
    exergy__check(float(power.abs().max()) < 0.001, 'Hourly electricity balance failed')
    exergy__check((d.liquid_NH3_component_t_per_h <= d.NH3_production_t_per_h + 1e-06).all(), 'Recovered NH3 exceeds gross NH3')
    return (d, totals, float(power.abs().max()))

def exergy__node_balances(flows):
    exergy__check(list(flows.id) == [item[0] for item in exergy_FLOW_SPEC], 'Expected exact ordered 25-link topology')
    for row, (_, source, target, _, _) in zip(flows.itertuples(), exergy_FLOW_SPEC):
        exergy__check((row.idSource, row.idTarget) == (source, target), f'Unexpected endpoints: {row.id}')
    exergy__check(exergy_np.isfinite(flows.value_GWh_ex_per_y.to_numpy()).all(), 'Non-finite link values')
    exergy__check((flows.value_GWh_ex_per_y >= 0).all(), 'Negative link values; no silent clipping')
    balances = []
    for node in exergy_NODE_LABELS:
        incoming = flows.loc[flows.idTarget.eq(node), 'value_GWh_ex_per_y'].tolist()
        outgoing = flows.loc[flows.idSource.eq(node), 'value_GWh_ex_per_y'].tolist()
        a, b = (exergy_math.fsum(incoming), exergy_math.fsum(outgoing))
        if incoming and outgoing:
            exergy__check(abs(a - b) <= exergy_FLOW_ATOL_GWH, f'Internal node {node} does not balance: {a - b}')
        balances.append(dict(node=node, incoming_GWh_ex_y=a, outgoing_GWh_ex_y=b, residual_GWh_ex_y=a - b, internal=bool(incoming and outgoing)))
    inputs = exergy_math.fsum((row['outgoing_GWh_ex_y'] for row in balances if not any(flows.idTarget.eq(row['node']))))
    outputs = exergy_math.fsum((row['incoming_GWh_ex_y'] for row in balances if not any(flows.idSource.eq(row['node']))))
    exergy__check(abs(inputs - outputs) <= exergy_FLOW_ATOL_GWH, 'Global exergy balance failed')
    return (balances, inputs, outputs)

def exergy_calculate_fig06d(summary: exergy_Mapping, dispatch: exergy_pd.DataFrame):
    """Recalculate all 25 values; no annual value is read from a retained ledger."""
    d, totals, max_power_residual = exergy__validated_dispatch(summary, dispatch)
    energy = lambda column: float(d[column].sum()) / 1000000.0
    wind, curtailed = (energy('wind_generation_kW'), energy('curtailment_kW'))
    charge, discharge = (energy('battery_charge_kW'), energy('battery_discharge_kW'))
    electrolysis = energy('electrolyzer_power_kW')
    synthesis_total = energy('synthesis_power_kW')
    direct = wind - curtailed - charge
    process = direct + discharge
    exergy__check(exergy_math.isclose(process, electrolysis + synthesis_total, rel_tol=0, abs_tol=0.0001), 'Annual electricity balance failed')
    nh3 = float(summary['annual_NH3_t']) / 1000.0
    h2 = float(summary['annual_H2_production_Nm3']) * exergy_MOLAR_MASS_KG_PER_KMOL['H2'] / exergy_NORMAL_MOLAR_VOLUME_NM3_PER_KMOL / 1000000.0
    water = float(summary['annual_water_m3']) / 1000.0
    n2 = nh3 * exergy_RU_N2_FEED_KG_PER_H / exergy_RU_RATED_NH3_KG_PER_H
    h2_stoich = nh3 * (3 * exergy_MOLAR_MASS_KG_PER_KMOL['H2'] / (2 * exergy_MOLAR_MASS_KG_PER_KMOL['NH3']))
    n2_stoich = nh3 * (exergy_MOLAR_MASS_KG_PER_KMOL['N2'] / (2 * exergy_MOLAR_MASS_KG_PER_KMOL['NH3']))
    exergy__check(h2 >= h2_stoich - 1e-06 and n2 >= n2_stoich - 1e-06, 'Fresh feeds below stoichiometric demand')
    purge_h2, purge_n2 = (max(h2 - h2_stoich, 0), max(n2 - n2_stoich, 0))
    oxygen = h2 * (exergy_MOLAR_MASS_KG_PER_KMOL['O2'] / (2 * exergy_MOLAR_MASS_KG_PER_KMOL['H2']))
    residual_water_raw = water - h2 - oxygen
    exergy__check(residual_water_raw >= -1e-06, 'Negative residual water in inherited electrolysis material boundary')
    residual_water = max(residual_water_raw, 0.0)
    air = n2 / exergy_DRY_AIR_N2_MASS_FRACTION
    asu_o2 = air * exergy_DRY_AIR_O2_MASS_FRACTION
    argon_other = air - n2 - asu_o2
    asu_electricity = n2 * 1000000.0 * exergy_ASU_KWH_PER_KG_N2 / 1000000.0
    delta = d.H2_production_Nm3_per_h - d.H2_consumption_Nm3_per_h
    h2_charge = float(delta.clip(lower=0).sum()) * exergy_MOLAR_MASS_KG_PER_KMOL['H2'] / exergy_NORMAL_MOLAR_VOLUME_NM3_PER_KMOL / 1000000.0
    h2_discharge = float((-delta).clip(lower=0).sum()) * exergy_MOLAR_MASS_KG_PER_KMOL['H2'] / exergy_NORMAL_MOLAR_VOLUME_NM3_PER_KMOL / 1000000.0
    exergy__check(exergy_math.isclose(h2_charge, h2_discharge, rel_tol=0, abs_tol=0.0002), 'Annual cyclic hydrogen charge/discharge does not reconcile')
    stored_h2 = 0.5 * (h2_charge + h2_discharge)
    direct_h2 = h2 - stored_h2
    exergy__check(direct_h2 >= 0, 'Negative direct-H2 throughput')
    recovered_mass = float(d.liquid_NH3_component_t_per_h.sum()) / 1000.0
    exergy__check(0 <= recovered_mass <= nh3, 'Invalid recovered-NH3 annual mass')
    chem = exergy_chemical_exergy_gwh
    water_ex, drainage_ex = (chem('H2O_liquid', water), chem('H2O_liquid', residual_water))
    h2_ex, direct_h2_ex, stored_h2_ex = (chem('H2', h2), chem('H2', direct_h2), chem('H2', stored_h2))
    n2_chemical = chem('N2', n2)
    oxygen_ex, asu_oxygen_ex, argon_ex = (chem('O2', oxygen), chem('O2', asu_o2), chem('Ar', argon_other))
    nh3_gross_ex, nh3_recovered_ex = (chem('NH3', nh3), chem('NH3', recovered_mass))
    purge_ex = chem('H2', purge_h2) + chem('N2', purge_n2)
    electrolysis_residual = electrolysis + water_ex - h2_ex - oxygen_ex - drainage_ex
    raw_asu_residual = asu_electricity - n2_chemical - asu_oxygen_ex - argon_ex
    raw_synthesis_electricity = synthesis_total - asu_electricity
    raw_synthesis_residual = raw_synthesis_electricity + h2_ex + n2_chemical - nh3_gross_ex - purge_ex
    n2_kmol = n2 * 1000000.0 / exergy_N2_PRESSURE_MOLAR_MASS_KG_PER_KMOL
    n2_pressure_ex = n2_kmol * exergy_GAS_CONSTANT_KJ_PER_KMOL_K * exergy_REFERENCE_TEMPERATURE_K * exergy_math.log(exergy_N2_DELIVERY_PRESSURE_BARA / exergy_N2_INLET_PRESSURE_BARA) / 3600000000.0
    compression_electricity = n2_pressure_ex / exergy_ISOTHERMAL_EFFICIENCY
    compression_residual = compression_electricity - n2_pressure_ex
    supply_electricity = asu_electricity + compression_electricity
    other_synthesis_electricity = raw_synthesis_electricity - compression_electricity
    delivered_n2_ex = n2_chemical + n2_pressure_ex
    exergy__check(other_synthesis_electricity >= 0, 'ASU/precompression exceeds synthesis_power_kW scope')
    values = {'f01': direct, 'f06': charge, 'f02': curtailed, 'f07': discharge, 'f10': charge - discharge, 'f03': electrolysis, 'f04': supply_electricity, 'f05': other_synthesis_electricity, 'f11': water_ex, 'f12': h2_ex, 'f16': oxygen_ex, 'f17': drainage_ex, 'f27': electrolysis_residual, 'f14': stored_h2_ex, 'f13': direct_h2_ex, 'f15': stored_h2_ex, 'f18': delivered_n2_ex, 'f08': asu_oxygen_ex, 'f21': argon_ex, 'f28': raw_asu_residual + compression_residual, 'f19': delivered_n2_ex, 'f22': nh3_recovered_ex, 'f23': purge_ex, 'f29': raw_synthesis_residual - compression_residual, 'f24': nh3_gross_ex - nh3_recovered_ex}
    masses = {'f11': water, 'f12': h2, 'f16': oxygen, 'f17': residual_water, 'f14': stored_h2, 'f13': direct_h2, 'f15': stored_h2, 'f18': n2, 'f08': asu_o2, 'f21': argon_other, 'f19': n2, 'f22': recovered_mass, 'f23': purge_h2 + purge_n2, 'f24': nh3 - recovered_mass}
    flows = exergy_pd.DataFrame([dict(id=edge, idSource=a, idTarget=b, source=exergy_NODE_LABELS[a], target=exergy_NODE_LABELS[b], value_GWh_ex_per_y=float(values[edge]), mass_kt_per_y=masses.get(edge), carrier=carrier, data_status='recomputed_from_supplied_summary_and_dispatch', note=note) for edge, a, b, carrier, note in exergy_FLOW_SPEC])
    balances, inputs, outputs = exergy__node_balances(flows)
    audit = dict(passed=True, boundary_id=exergy_BOUNDARY_ID, route='Ru+ALK', hourly_rows=len(d), link_count=25, node_count=18, unit='GWh_ex_per_y', all_25_values_recomputed=True, retained_annual_values_used=False, scope='Electricity and standard chemical exergy plus N2 pressure-exergy estimate only', residual_interpretation='Control-volume balance differences, not independently validated device irreversibility', synthesis_power_boundary='ASU and N2 precompression already included; caller must confirm model convention', new_Aspen_or_optimisation_result=False, full_physical_exergy_audit=False, heat_recovery_credit=False, gross_NH3_kt_per_y=nh3, recovered_NH3_kt_per_y=recovered_mass, unrecovered_NH3_kt_per_y=nh3 - recovered_mass, gross_NH3_chemical_exergy_GWh=nh3_gross_ex, recovered_NH3_chemical_exergy_GWh=nh3_recovered_ex, nitrogen_kt_per_y=n2, air_feed_mass_kt_per_y=air, air_feed_exergy_GWh_per_y=0.0, asu_electricity_GWh_y=asu_electricity, nitrogen_precompression_electricity_GWh_y=compression_electricity, combined_nitrogen_supply_electricity_GWh_y=supply_electricity, nitrogen_chemical_exergy_GWh_y=n2_chemical, nitrogen_pressure_exergy_GWh_y=n2_pressure_ex, nitrogen_delivery_exergy_GWh_y=delivered_n2_ex, other_synthesis_electricity_GWh_y=other_synthesis_electricity, h2_charge_kt_per_y=h2_charge, h2_discharge_kt_per_y=h2_discharge, h2_storage_throughput_rule='Mean of annual positive production-minus-consumption and negative magnitudes', inherited_material_clamps=dict(purge_h2_unclamped_kt=h2 - h2_stoich, purge_n2_unclamped_kt=n2 - n2_stoich, residual_water_unclamped_kt=residual_water_raw), summary_reconciliation=totals, hourly_power_max_residual_kW=max_power_residual, node_balances=balances, external_input_GWh_ex_y=inputs, external_output_GWh_ex_y=outputs, global_balance_residual_GWh_ex_y=inputs - outputs, constants=dict(chemical_exergy_kJ_per_mol=dict(exergy_CHEMICAL_EXERGY_KJ_PER_MOL), chemical_molar_masses_kg_per_kmol=dict(exergy_MOLAR_MASS_KG_PER_KMOL), nitrogen_pressure_molar_mass=exergy_N2_PRESSURE_MOLAR_MASS_KG_PER_KMOL, nitrogen_inlet_bara=exergy_N2_INLET_PRESSURE_BARA, nitrogen_delivery_bara=exergy_N2_DELIVERY_PRESSURE_BARA, temperature_K=exergy_REFERENCE_TEMPERATURE_K, isothermal_efficiency=exergy_ISOTHERMAL_EFFICIENCY, gas_constant_kJ_per_kmol_K=exergy_GAS_CONSTANT_KJ_PER_KMOL_K, Ru_rated_NH3_kg_h=exergy_RU_RATED_NH3_KG_PER_H, Ru_fresh_N2_kg_h=exergy_RU_N2_FEED_KG_PER_H, normal_molar_volume_Nm3_per_kmol=exergy_NORMAL_MOLAR_VOLUME_NM3_PER_KMOL, dry_air_N2_mass_fraction=exergy_DRY_AIR_N2_MASS_FRACTION, dry_air_O2_mass_fraction=exergy_DRY_AIR_O2_MASS_FRACTION, ASU_kWh_per_kg_N2=exergy_ASU_KWH_PER_KG_N2))
    return (flows, audit)

def exergy__visual_local(properties):
    return {key: exergy_copy.deepcopy(value) for key, value in properties.items() if key.startswith(('shape_', 'name_label_', 'value_label_'))}

def exergy_make_opensankey(flows, audit, layout_template=None):
    """Reuse only optional presentation geometry, never retained numerical fields."""
    balances, _, _ = exergy__node_balances(flows)
    exergy__check(audit.get('boundary_id') == exergy_BOUNDARY_ID, 'Wrong exergy boundary')
    template = {} if layout_template is None else layout_template
    if template:
        exergy__check(set(template.get('nodes', {})) == set(exergy_NODE_LABELS), 'Layout template has a different node set')
        exergy__check(set(template.get('links', {})) == set(flows.id), 'Layout template has a different link set')
        for row in flows.itertuples():
            link = template['links'][row.id]
            exergy__check((link['idSource'], link['idTarget']) == (row.idSource, row.idTarget), f'Layout topology mismatch: {row.id}')
    doc = dict(version='1.3.2', format_version=3, id='sankey_maitre', node_pos_is_center=True, height=1520, width=3443.40478515625, user_scale=500, grid_visible=False, couleur_fond_sankey='#ffffff', show_zero_links=False, auto_fit_mode='full', main_zone=dict(show_diagram=True, show_spreadsheet=False, show_doc=False, doc_layout='sheet-right', doc_bottom_px=280, split_ratio=2 / 3, doc_sheet_ratio=0.5, show_unitary=False, unitary_ratio=0.6), panels=dict(sidebar_id='', sidebar_width_px=270, sidebar_open=False, popups={}), sheets=dict(current='fig06d', order=['fig06d'], entries={'fig06d': {'name': 'Fig06d'}}), style={'default': {'name': 'Default'}, 'NodeStyle': {'name_label_is_visible': True}, 'LinkStyle': {'value_label_is_visible': True}}, nodes={}, links={}, spreadsheet_state={'col_overrides': {}, 'sheet_overrides': {}, 'active_sheet': 'sheet-flux'})
    for key in ('height', 'width', 'user_scale', 'font_size_locked', 'auto_fit_mode', 'smart_guides', 'connection_arrows_off', 'legend', 'couleur_fond_sankey'):
        if key in template:
            doc[key] = exergy_copy.deepcopy(template[key])
    if 'style' in template:
        doc['style'] = {key: dict(exergy__visual_local(value), name=value.get('name', key)) for key, value in template['style'].items()}
    for record in balances:
        node_id = record['node']
        old = template.get('nodes', {}).get(node_id, {})
        x, y = exergy_NODE_POSITIONS[node_id]
        incoming = flows.loc[flows.idTarget.eq(node_id), 'id'].tolist()
        outgoing = flows.loc[flows.idSource.eq(node_id), 'id'].tolist()
        incident = incoming + outgoing
        order = [edge for edge in old.get('links_order', []) if edge in incident]
        order += [edge for edge in incident if edge not in order]
        local = {'shape_min_width': 7, 'shape_min_height': 0, 'shape_color': '#506574', 'shape_io_reorg_mode': 'none', 'name_label_is_visible': True, 'name_label_font_family': 'Arial,sans-serif', 'name_label_font_size': 48, 'name_label_bold': False, 'name_label_vert': 'top'}
        local.update(exergy__visual_local(old.get('local', {})))
        value = max(record['incoming_GWh_ex_y'], record['outgoing_GWh_ex_y'])
        label = f'Nitrogen\n(30 bar) {value:,.2f}' if node_id == 'n10' else f'{exergy_NODE_LABELS[node_id]}\n{value:,.2f}'
        doc['nodes'][node_id] = dict(x=old.get('x', x), y=old.get('y', y), style=exergy_copy.deepcopy(old.get('style', ['default'])), local=local, name=label, inputLinksId=incoming, outputLinksId=outgoing, links_order=order)
    for row in flows.itertuples():
        old = template.get('links', {}).get(row.id, {})
        local = {'shape_type': 'bezier_path', 'shape_color': exergy_CARRIER_COLORS[row.carrier], 'shape_opacity': 0.72, 'shape_curvature': 0.15, 'shape_waypoints': [], 'shape_is_arrow': False, 'shape_color_rule': 'own', 'name_label_is_visible': False, 'value_label_is_visible': False}
        local.update(exergy__visual_local(old.get('local', {})))
        link = dict(x=old.get('x', exergy_NODE_POSITIONS[row.idSource][0]), y=old.get('y', exergy_NODE_POSITIONS[row.idSource][1]), style=exergy_copy.deepcopy(old.get('style', ['default'])), local=local, idSource=row.idSource, idTarget=row.idTarget, value={'data_value': row.value_GWh_ex_per_y}, tooltip_text=f'{row.value_GWh_ex_per_y:.12g} GWh_ex/y; {row.note}; recalculated from supplied Ru+ALK inputs')
        for key in ('source_side_locked', 'source_side_frozen', 'target_side_locked', 'target_side_frozen'):
            if key in old:
                link[key] = exergy_copy.deepcopy(old[key])
        doc['links'][row.id] = link
    ids = list(doc['links']) + list(doc['nodes'])
    order = [key for key in template.get('order_g_elements', []) if key in ids]
    doc['order_g_elements'] = order + [key for key in ids if key not in order]
    return doc

def exergy_validate_reference(flows, project, atol=exergy_FLOW_ATOL_GWH):
    """Compare every ID, endpoint and unrounded value. Never alter generated flows."""
    exergy__node_balances(flows)
    exergy__check(set(project.get('nodes', {})) == set(exergy_NODE_LABELS), 'Reference does not contain the expected 18 nodes')
    exergy__check(set(project.get('links', {})) == set(flows.id), 'Reference does not contain the expected 25 links')
    rows = []
    for row in flows.itertuples():
        ref = project['links'][row.id]
        expected = float(ref['value']['data_value'])
        same_pair = (ref['idSource'], ref['idTarget']) == (row.idSource, row.idTarget)
        delta = row.value_GWh_ex_per_y - expected
        rows.append(dict(id=row.id, idSource=row.idSource, idTarget=row.idTarget, calculated_GWh_ex_y=row.value_GWh_ex_per_y, reference_GWh_ex_y=expected, difference_GWh_ex_y=delta, passed=bool(same_pair and exergy_math.isfinite(expected) and (abs(delta) <= atol))))
    failed = [item for item in rows if not item['passed']]
    exergy__check(not failed, f'Reference differs; no retained values substituted: {failed}')
    return dict(passed=True, checked_links=len(rows), absolute_tolerance_GWh=atol, max_absolute_difference_GWh=max((abs(item['difference_GWh_ex_y']) for item in rows)), links=rows)

def exergy_export_fig06d(summary, dispatch, output_dir, layout_template=None):
    """Explicit opt-in file export; computation and validation finish before writing."""
    flows, audit = exergy_calculate_fig06d(summary, dispatch)
    document = exergy_make_opensankey(flows, audit, layout_template)
    payload = exergy_json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False)
    audit_payload = exergy_json.dumps(audit, indent=2, ensure_ascii=False, allow_nan=False)
    output = exergy_Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = {'flows': output / 'Fig06d_Exergy_Flows.csv', 'opensankey': output / 'Fig06d_Exergy_OpenSankey.json', 'audit': output / 'Fig06d_Exergy_Audit.json'}
    flows.to_csv(paths['flows'], index=False, encoding='utf-8-sig')
    paths['opensankey'].write_text(payload + '\n', encoding='utf-8')
    paths['audit'].write_text(audit_payload + '\n', encoding='utf-8')
    return (paths, audit)

# Case interfaces. Every calculation uses the common physical model above.
CASE_ROUTES = ['Ru+ALK', 'Fe+ALK', 'Ru+AEM', 'Fe+AEM', 'Ru+PEM', 'Fe+PEM', 'Ru+SOEC', 'Fe+SOEC']
PACKAGE = THIS_FILE.parent.parent
PUBLISHED = PACKAGE / 'Plotting/Data'
CASE_INPUTS = THIS_FILE.parent / 'Case_Inputs'


def case_parser(case):
    parser = argparse.ArgumentParser(description='Compute the ' + case + ' case and export plotting tables.')
    parser.add_argument('--output', type=Path, default=PACKAGE / 'Results' / ('New_' + case))
    parser.add_argument('--input', type=Path, default=THIS_FILE.with_name('Resource_Inputs.xlsx'))
    parser.add_argument('--route', choices=['all'] + CASE_ROUTES, default='all')
    parser.add_argument('--check', action='store_true', help='Validate inputs and construct matrices without optimisation')
    parser.add_argument('--time-limit', type=float, default=3600.)
    parser.add_argument('--threads', type=int, default=8)
    parser.add_argument('--gurobi', type=Path)
    parser.add_argument('--years', nargs='+', type=int, default=list(range(2021, 2026)))
    parser.add_argument('--targets', nargs='+', type=int, help='Explicit LAEC utilisation percentages')
    parser.add_argument('--sites', nargs='+', choices=['IM_KESHIKETENG', 'IM_SIZIWANG', 'XZ_NGARI', 'FJ_PINGTAN_OFFSHORE'])
    parser.add_argument('--shares', nargs='+', type=float, help='PV nameplate fractions, not percentages')
    parser.add_argument('--iterations', type=int, default=20)
    parser.add_argument('--baseline', type=Path, default=CASE_INPUTS / 'Baseline_Ledgers.json', help='Cost ledgers from a baseline run')
    parser.add_argument('--reoptimize', action='store_true', help='Sensitivity only: recompute all 25 candidate designs before evaluating the cost envelope')
    return parser


def read_case_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_case_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, default=lambda x: x.item() if isinstance(x, np.generic) else str(x)), encoding='utf-8')


def write_case_table(output, name, rows):
    dest = Path(output) / 'Plot_Data'
    dest.mkdir(parents=True, exist_ok=True)
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    frame.to_csv(dest / name, index=False)


def resource_profile(options, sheet):
    data = pd.read_excel(options.input, sheet_name=sheet)
    column = 'wind_generation_kW_per_kW' if 'wind_generation_kW_per_kW' in data else 'wind_per_kw'
    wind = data[column].to_numpy(float)
    if len(wind) not in (8760, 8784) or not np.isfinite(wind).all() or (wind < 0).any():
        raise ValueError('Invalid complete-year resource profile: ' + sheet)
    return wind, data


@contextmanager
def capacity_terminal_scope():
    global process_costs
    saved = process_costs
    def scaled(syn, capacity):
        row = original_process(syn, capacity)
        terminal = TERMINAL_CAPACITY_TABLE[f'{syn["syn_id"]}_{int(capacity)}']
        capital = terminal['installed_terminal_capital_CNY']
        annual = capital * (annualized_capital_factor(30.) + terminal['terminal_fixed_OM_fraction'])
        for field in ('process_capex_CNY', 'process_equipment_capex_CNY'):
            row[field] += capital
        for field in ('annual_process_CNY', 'annual_process_equipment_CNY', 'annual_total_CNY'):
            row[field] += annual
        row.update(targeted_terminal_capital_CNY=capital, targeted_terminal_fixed_annual_CNY=annual)
        return row
    try:
        process_costs = scaled
        yield
    finally:
        process_costs = saved


def case_ledger(summary):
    el = ELECTROLYZERS[summary['electrolyzer']]
    annual = annualized_capital_factor
    slopes = {
        'wind_capex': summary['wind_capex_CNY'] * (annual(WIND_LIFE_Y) + WIND_FIXED_OM_RATIO),
        'electrolyzer_capex': summary['electrolyzer_capex_CNY'] * (annual(el['system_life_y']) + el['fixed_om_ratio']) + summary['annual_stack_replacement_CNY'],
        'synthesis_capex': process_costs(SYNTHESES[summary['synthesis']], summary['synthesis_nameplate_kt_per_y'])['annual_process_equipment_CNY'],
        'battery_capex': summary['battery_capex_CNY'] * (annual(BESS_LIFE_Y) + BESS_FIXED_OM_RATIO),
        'hydrogen_capex': summary['h2_tank_capex_CNY'] * (annual(H2_TANK_LIFE_Y) + H2_TANK_FIXED_OM_RATIO),
        'catalyst_capex': summary['annual_catalyst_CNY'],
    }
    q = summary['annual_liquid_NH3_component_t']
    return dict(route=summary['route'], annual_response_CNY=slopes, annual_liquid_NH3_component_t=q,
                baseline_LCOA_CNY_per_t=summary['annual_total_cost_CNY']/q,
                residual_annual_CNY=summary['annual_total_cost_CNY']-sum(slopes.values()),
                baseline_gross_NH3_t=summary['annual_NH3_t'], baseline_solver_gap=summary.get('design_global_relative_gap'))


def formal_case(case, options):
    from contextlib import nullcontext
    routes = CASE_ROUTES if options.route == 'all' else [options.route]
    if case == 'weather':
        routes = ['Ru+ALK']
        if 2025 not in options.years:
            raise ValueError('Weather comparisons require 2025 in --years for the storage-capacity reference.')
    years = [2025] + sorted(set(options.years) - {2025}) if case == 'weather' else [2025]
    nameplates = np.arange(100., 1001., 25.) if case in ('fixed_production', 'fixed_wind') else np.array([475.])
    scenario = {'fixed_production': 'fixed_300kt', 'fixed_wind': 'fixed_wind'}.get(case, 'variable_scale')
    all_rows, plot_rows, ledgers, checks = [], [], {}, []
    for year in years:
        wind, _ = resource_profile(options, f'Wind_{year}')
        for route in routes:
            sid, tech = route.split('+')
            el, syn = copy.deepcopy(ELECTROLYZERS[tech]), copy.deepcopy(SYNTHESES[sid])
            target = 300000. / syn['targeted_terminal']['liquid_recovery_fraction'] if case == 'fixed_production' else None
            limits = dict(wind_capacity_min_kw=FIXED_WIND_KW if case == 'fixed_wind' else 0.,
                          wind_capacity_max_kw=FIXED_WIND_KW if case == 'fixed_wind' else MAX_WIND_KW,
                          plant_options_kt_per_y=nameplates)
            with route_scope(el), capacity_terminal_scope() if len(nameplates) > 1 else nullcontext():
                if options.check:
                    problem = build_problem(el, syn, wind, scenario, lcoa_parameter=0., production_target_t=target,
                        global_design_integrality=True, enforce_electrolyzer_commitment=True,
                        enforce_battery_exclusivity=True, **limits)
                    checks.append(dict(route=route, year=year, hours=len(wind), nameplates=nameplates.tolist(),
                                       production_target_gross_t=target, **matrix_digest(problem)))
                    print(json.dumps(checks[-1]), flush=True)
                    del problem
                    continue
                if not GUROBI_CL.is_file():
                    raise RuntimeError('Set --gurobi to a licensed Gurobi CLI executable.')
                if target is not None:
                    row, dispatch, history = run_certified_fixed_production_design(el, syn, wind,
                        production_target_t=target, scenario=scenario, final_stage=case, **limits)
                else:
                    row, dispatch, history = run_certified_fractional_design(el, syn, wind,
                        scenario=scenario, final_stage=case, initial_lcoa=5000., **limits)
                row.update(route=route, synthesis=sid, electrolyzer=tech, year=year)
                project_fe(model, syn, row, dispatch, PARAMETERS['curves']['Fe'])
                row['annual_NH3_target_t'] = target
                audit = matrix_check(model, el, syn, wind, row, dispatch)
                physical = terminal_native_audit(model, syn, row, dispatch)
                if not audit['passed'] or not physical['passed']:
                    raise RuntimeError('Computed design failed feasibility verification.')
                row['annual_liquid_NH3_component_t'] = float(dispatch.NH3_production_t_per_h.sum()) * syn['targeted_terminal']['liquid_recovery_fraction']
                row['LCOA_CNY_per_t_liquid_NH3'] = row['annual_total_cost_CNY'] / row['annual_liquid_NH3_component_t']
                ledgers[route] = case_ledger(row)
                plot = plot_cost_row(row, el, syn)
                folder = options.output / (f'{year}_' + route.replace('+', '_'))
                folder.mkdir(parents=True, exist_ok=False)
                dispatch.to_csv(folder / 'Hourly_Dispatch.csv', index=False)
                pd.DataFrame(history).to_csv(folder / 'Solver_Iterations.csv', index=False)
                write_case_json(folder / 'Solution_Check.json', dict(matrix=audit, process=physical))
                write_case_json(folder / 'Economics_and_Capacities.json', row)
                all_rows.append(row)
                plot_rows.append(plot)
                if case == 'baseline':
                    write_case_table(options.output, 'Fig05_Economics_and_Capacities.csv', plot_rows)
                    if route == 'Ru+ALK':
                        write_case_table(options.output, 'Fig06_Hourly_Dispatch.csv', dispatch)
                        exergy_export_fig06d(row, dispatch, options.output / 'Plot_Data')
                elif case == 'weather':
                    write_case_table(options.output, 'Fig11_Weather_Year_Results.csv', weather_plot_table(all_rows))
                else:
                    write_case_table(options.output, 'SI_Boundary_' + scenario + '.csv', plot_rows)
                write_case_json(options.output / 'Baseline_Ledgers.json', ledgers)
                print(route, year, row['LCOA_CNY_per_t_liquid_NH3'], flush=True)
    if options.check:
        write_case_json(options.output / 'Matrix_Check.json', checks)


def weather_plot_table(rows):
    output = []
    for r in rows:
        p = plot_cost_row(r, ELECTROLYZERS['ALK'], SYNTHESES['Ru'])
        recovery = SYNTHESES['Ru']['targeted_terminal']['liquid_recovery_fraction']
        output.append(dict(year=r['year'], upper_bound=r['LCOA_CNY_per_t_liquid_NH3'],
            curtailment_percent=100*r['curtailment_rate'],
            lower_bound=r['design_global_lower_bound_CNY_per_tNH3']/recovery,
            gap=r['design_global_relative_gap'], wind_MW=r['wind_capacity_MW'], EL_MW=r['electrolyzer_capacity_MW'],
            battery_MWh=r['battery_capacity_MWh'], H2_Nm3=r['h2_tank_capacity_Nm3'],
            hours=8784 if r['year'] == 2024 else 8760, liquid_NH3_t=r['annual_liquid_NH3_component_t'],
            synthesis=p['synthesis_LCOA_CNY_per_t'], electrolysis=p['electrolysis_LCOA_CNY_per_t'],
            battery=p['battery_LCOA_CNY_per_t'], hydrogen=p['storage_LCOA_CNY_per_t'], wind=p['wind_LCOA_CNY_per_t'],
            numerical_CNY_per_t=p['numerical_CNY_per_t'], physical_LCOA_CNY_per_t=r['LCOA_CNY_per_t_liquid_NH3']-p['numerical_CNY_per_t']))
    frame = pd.DataFrame(output)
    base = frame[frame.year == 2025]
    if base.empty:
        raise ValueError('The computed 2025 reference is required; no published reference is substituted.')
    frame['battery_index_2025'] = frame.battery_MWh/base.iloc[0].battery_MWh
    frame['hydrogen_index_2025'] = frame.H2_Nm3/base.iloc[0].H2_Nm3
    return frame.sort_values('year').reset_index(drop=True)


def economic_case(case, options):
    ledgers = read_case_json(options.baseline)
    for ledger in ledgers.values():
        total = ledger['residual_annual_CNY'] + sum(ledger['annual_response_CNY'].values())
        np.testing.assert_allclose(total/ledger['annual_liquid_NH3_component_t'], ledger['baseline_LCOA_CNY_per_t'], rtol=1e-10)
    if options.check:
        write_case_json(options.output/'Input_Check.json', dict(passed=True, routes=list(ledgers), case=case))
        return
    if case == 'uncertainty':
        rows = []
        for route in CASE_ROUTES if options.route == 'all' else [options.route]:
            ledger = ledgers[route]
            draws = monte_carlo(ledger, 100000, 20260920)
            values = np.quantile(draws.LCOA_CNY_per_t, [.05, .25, .5, .75, .95])
            rows.append(dict(route=route, baseline=ledger['baseline_LCOA_CNY_per_t'],
                **dict(zip(['p05', 'p25', 'median', 'p75', 'p95'], values)), samples=100000, seed=20260920,
                baseline_solver_gap=ledger['baseline_solver_gap']))
        write_case_table(options.output, 'Fig07_Cost_Uncertainty.csv', pd.DataFrame(rows).sort_values('baseline'))
    elif case == 'pem_cost':
        rows, parity = [], []
        prices = np.linspace(1000, 6350, 201)
        for sid in ('Ru', 'Fe'):
            l, alk = ledgers[sid+'+PEM'], ledgers[sid+'+ALK']['baseline_LCOA_CNY_per_t']
            values = reprice(l, {'electrolyzer_capex': prices/6350})
            slope = l['annual_response_CNY']['electrolyzer_capex']/l['annual_liquid_NH3_component_t']
            price = 6350*(1+(alk-l['baseline_LCOA_CNY_per_t'])/slope)
            rows.append(pd.DataFrame(dict(catalyst=sid, PEM_CAPEX_CNY_per_kW=prices, PEM_LCOA_CNY_per_t=values, ALK_LCOA_CNY_per_t=alk)))
            parity.append(dict(catalyst=sid, fixed_design_parity_CAPEX_CNY_per_kW=price, ALK_LCOA_CNY_per_t=alk, capacity_and_dispatch_reoptimized=False))
        write_case_table(options.output, 'Fig09_PEM_Cost_Response.csv', pd.concat(rows))
        write_case_table(options.output, 'Fig09_PEM_Parity_Investment.csv', parity)
        years = np.linspace(2025., 2050., 501)
        # Conditional equipment-price trajectories used in the manuscript.
        write_case_table(options.output, 'Fig09_Equipment_Price_Paths.csv', pd.DataFrame(dict(year=years,
            ALK=np.maximum(1027.3443515596175,2200*.97**(years-2025)), PEM=np.maximum(1700.,6350*.952**(years-2025)))))
    elif case == 'coal':
        l = ledgers['Ru+ALK']
        xx, yy = np.meshgrid(np.linspace(900, 5400, 151), np.linspace(440, 2640, 121))
        z = reprice(l, {'wind_capex': xx/3600, 'electrolyzer_capex': yy/2200})
        write_case_table(options.output, 'Fig10_Joint_Cost_Surface.csv', pd.DataFrame(dict(
            wind_CAPEX_CNY_per_kW=xx.ravel(), electrolyzer_CAPEX_CNY_per_kW=yy.ravel(), LCOA_CNY_per_t=z.ravel())))
        write_case_table(options.output, 'Fig10_Carbon_Parity.csv', [dict(route='Ru+ALK', baseline_LCOA_CNY_per_t=l['baseline_LCOA_CNY_per_t'],
            coal_reference_CNY_per_t=2350., coal_process_emissions_tCO2_per_tNH3=e,
            parity_carbon_price_CNY_per_tCO2=(l['baseline_LCOA_CNY_per_t']-2350)/e) for e in (4.4, 4.6, 4.8)])
        anchors = pd.read_csv(CASE_INPUTS/'Cost_Path_Anchors.csv')
        coal = pd.read_csv(CASE_INPUTS/'Carbon_Price_Paths.csv')
        coal['coal_cost_CNY_per_tNH3'] = 2350. + 4.6*coal.carbon_CNY_reference_per_tCO2
        quantiles = coal.groupby('year').coal_cost_CNY_per_tNH3.quantile([.25,.5,.75]).unstack()
        years, paths = quantiles.index.to_numpy(), []
        for route in CASE_ROUTES:
            for name in ('low', 'high'):
                base = np.interp(2025, anchors.year, anchors[name+'_usd_per_t'])
                factors = np.interp(years, anchors.year, anchors[name+'_usd_per_t'])/base
                paths.append(pd.DataFrame(dict(route=route, path=name, year=years, normalized_scenario_multiplier=factors,
                                               LCOA_CNY_per_t=ledgers[route]['baseline_LCOA_CNY_per_t']*factors)))
        green = pd.concat(paths, ignore_index=True)
        envelope = green.groupby('year').LCOA_CNY_per_t.agg(['min','max'])
        ranges = pd.DataFrame(dict(year=years, green_low_CNY_per_t=envelope['min'].to_numpy(), green_high_CNY_per_t=envelope['max'].to_numpy(),
            coal_p25_CNY_per_t=quantiles[.25].to_numpy(), coal_median_CNY_per_t=quantiles[.5].to_numpy(), coal_p75_CNY_per_t=quantiles[.75].to_numpy()))
        for name, frame in [('Fig10_Green_Cost_Paths.csv',green), ('Fig10_Annual_Cost_Ranges.csv',ranges), ('Fig10_Coal_Scenario_Ensemble.csv',coal)]:
            write_case_table(options.output, name, frame)
        write_case_table(options.output, 'Fig10_Baseline_Route_Costs.csv', [dict(route=k,
            LCOA_CNY_per_t=v['baseline_LCOA_CNY_per_t'], wind_CAPEX_CNY_per_kW=WIND_CAPEX_CNY_PER_KW,
            electrolyzer_CAPEX_CNY_per_kW=ELECTROLYZERS[k.split('+')[1]]['installed_capex_CNY_per_kW'],
            baseline_solver_gap=v['baseline_solver_gap']) for k,v in ledgers.items()])
    elif case == 'sensitivity':
        sensitivity_case(options)


def sensitivity_case(options):
    witnesses = sensitivity_reoptimise(options) if options.reoptimize else read_case_json(CASE_INPUTS/'Sensitivity_Designs.json')
    factors = ['wind_capex', 'electrolyzer_capex', 'synthesis_capex', 'catalyst_capex', 'hydrogen_capex', 'battery_capex']
    def normalized(r):
        return r['annual_total_CNY'] - ((r['multiplier']-1)*r['annual_factor_slopes_CNY'][r['factor']] if r['factor'] else 0.)
    def cost(r, f, value):
        return (normalized(r)+(value-1)*r['annual_factor_slopes_CNY'][f])/r['annual_liquid_NH3_component_t']
    def anchor(r, f):
        slope = r['annual_factor_slopes_CNY'][f]/r['annual_liquid_NH3_component_t']
        return Design(r['multiplier'], r['lower_bound_CNY_per_t_liquid_NH3'], r['LCOA_CNY_per_t_liquid_NH3']-slope*r['multiplier'],
                      slope, r['witness'], r['feasible_set_sha256'])
    baseline = witnesses[0]
    anchors = {f:[anchor(baseline,f)]+[anchor(r,f) for r in witnesses[1:] if r['factor']==f] for f in factors}
    best = min(witnesses, key=lambda r:cost(r,factors[0],1.))
    upper0 = cost(best,factors[0],1.)
    lower0 = max(certificates([1.], a, .02)[0]['lower_bound'] for a in anchors.values())
    grid, rows, endpoints = np.linspace(.8,1.2,801), [], []
    for factor in factors:
        priced = np.array([[cost(r,factor,p) for p in grid] for r in witnesses])
        active = priced.argmin(axis=0)
        upper = priced[active,np.arange(len(grid))]
        slope = best['annual_factor_slopes_CNY'][factor]/best['annual_liquid_NH3_component_t']
        shared = Design(1.,lower0,upper0-slope,slope,best['witness'],best['feasible_set_sha256'])
        bounds = certificates(grid,anchors[factor]+[shared],.02)
        lower = np.array([b['lower_bound'] for b in bounds])
        x = (grid-1)*100
        fit = np.polyval(np.polyfit(x,upper,1),x)
        for i,p in enumerate(grid):
            rows.append(dict(factor=factor, change_percent=x[i], multiplier=p, LCOA_CNY_per_t=upper[i], lower_bound=lower[i],
                relative_gap=(upper[i]-lower[i])/upper[i], linear_fit_LCOA=fit[i], active_witness=witnesses[active[i]]['witness'],
                method='Lower envelope of verified feasible design cost functions; not a new solve at each evaluation'))
        endpoints.append(dict(factor=factor, minimum=upper[0], maximum=upper[-1], range_CNY_per_t=upper[-1]-upper[0]))
    write_case_table(options.output,'Fig07_Investment_Sensitivity.csv',rows)
    write_case_table(options.output,'Fig07_Sensitivity_Endpoints.csv',endpoints)


def sensitivity_reoptimise(options):
    global process_costs,AUDIT_DIR
    factors = ['wind_capex','electrolyzer_capex','synthesis_capex','catalyst_capex','hydrogen_capex','battery_capex']
    wind,_ = resource_profile(options,'Wind_2025')
    jobs = [(None,1.)]+[(f,m) for f in factors for m in (.8,.9,1.1,1.2)]
    witnesses=[]
    for index,(factor,multiplier) in enumerate(jobs):
        el,syn=copy.deepcopy(ELECTROLYZERS['ALK']),copy.deepcopy(SYNTHESES['Ru'])
        old_process=process_costs
        old_values={k:globals()[k] for k in ('WIND_CAPEX_CNY_PER_KW','BESS_CAPEX_CNY_PER_KWH','H2_TANK_CAPEX_CNY_PER_NM3')}
        folder=options.output/f'Design_{index:02d}'
        folder.mkdir(parents=True,exist_ok=False)
        AUDIT_DIR=folder/'Solver_Logs'
        AUDIT_DIR.mkdir()
        try:
            if factor=='catalyst_capex':
                syn['catalyst_CNY_per_kg']*=multiplier
            elif factor:
                configure_costs(model,el,syn,factor,multiplier)
            with route_scope(el):
                row,d,history=run_certified_fractional_design(el,syn,wind,scenario='variable_scale',
                    final_stage='investment_sensitivity',initial_lcoa=5000.,plant_option_index=0,
                    plant_options_kt_per_y=[475.],wind_capacity_min_kw=0.,wind_capacity_max_kw=MAX_WIND_KW)
                check=matrix_check(model,el,syn,wind,row,d)
                if not check['passed']:raise RuntimeError('Sensitivity witness is infeasible.')
                slopes=cost_response_slopes(model,el,syn,row,d,factor=None)
                slopes.pop('fixed_om')
                slopes['catalyst_capex']=row['annual_catalyst_CNY']
                if factor:slopes[factor]/=multiplier
                physical=build_problem(el,syn,wind,'variable_scale',lcoa_parameter=0.,
                    plant_option_index=0,plant_options_kt_per_y=[475.],
                    global_design_integrality=True,enforce_electrolyzer_commitment=True,
                    enforce_battery_exclusivity=True,wind_capacity_min_kw=0.,wind_capacity_max_kw=MAX_WIND_KW)
                feasible_hash=matrix_digest((np.zeros_like(physical[0]),*physical[1:]))['sha256']
                del physical
            recovery=syn['targeted_terminal']['liquid_recovery_fraction']
            q=float(d.NH3_production_t_per_h.sum())*recovery
            witnesses.append(dict(witness=f'Design_{index:02d}',factor=factor,multiplier=multiplier,
                annual_total_CNY=row['annual_total_cost_CNY'],annual_factor_slopes_CNY=slopes,
                annual_liquid_NH3_component_t=q,LCOA_CNY_per_t_liquid_NH3=row['annual_total_cost_CNY']/q,
                lower_bound_CNY_per_t_liquid_NH3=row['design_global_lower_bound_CNY_per_tNH3']/recovery,
                feasible_set_sha256=feasible_hash))
            d.to_csv(folder/'Hourly_Dispatch.csv',index=False)
            pd.DataFrame(history).to_csv(folder/'Solver_Iterations.csv',index=False)
            write_case_json(folder/'Solution_Check.json',check)
            write_case_json(options.output/'Sensitivity_Designs.json',witnesses)
        finally:
            process_costs=old_process
            globals().update(old_values)
    return witnesses


def continuous_solve(problem, folder, options, fractional=False):
    import highspy
    c, integers, bounds, constraints, ix, terms = problem
    if np.any(integers) or terms.get('indicator_constraints'):
        raise ValueError('Continuous solver cannot silently relax an integer or indicator constraint.')
    a = vstack([v.A for v in constraints],format='csr')
    lower, upper = np.concatenate([v.lb for v in constraints]), np.concatenate([v.ub for v in constraints])
    lp = highspy.HighsLp()
    lp.num_col_, lp.num_row_ = len(c), a.shape[0]
    lp.col_cost_, lp.col_lower_, lp.col_upper_ = c, bounds.lb, bounds.ub
    lp.row_lower_, lp.row_upper_ = lower, upper
    lp.a_matrix_.format_ = highspy.MatrixFormat.kRowwise
    lp.a_matrix_.start_, lp.a_matrix_.index_, lp.a_matrix_.value_ = a.indptr.astype(np.int32), a.indices.astype(np.int32), a.data
    solver = highspy.Highs()
    folder.mkdir(parents=True,exist_ok=True)
    for k,v in dict(solver='ipm', threads=options.threads, parallel='off',
                    run_crossover='off' if fractional else 'on',
                    primal_feasibility_tolerance=1e-7,dual_feasibility_tolerance=1e-7,output_flag=False).items():
        if solver.setOptionValue(k,v) == highspy.HighsStatus.kError:
            raise RuntimeError('HiGHS option failed: '+k)
    solver.setOptionValue('log_file',str(folder/'HiGHS.log'))
    solver.passModel(lp)
    columns = np.arange(len(c),dtype=np.int32)
    parameter, best, history, converged = 5000., None, [], False
    for iteration in range(options.iterations if fractional else 1):
        objective = c.copy()
        if fractional:
            objective[ix.nh3_prod:ix.nh3_prod+ix.n] -= parameter
        # Equivalent objective scaling keeps compatibility with older HiGHS versions.
        solver.changeColsCost(len(c), columns, objective/256. if fractional else objective)
        solver.setOptionValue('time_limit',solver.getRunTime()+options.time_limit)
        solver.run()
        status, solution = solver.getModelStatus(), solver.getSolution()
        vector = np.asarray(solution.col_value)
        gross = float(vector[ix.nh3_prod:ix.nh3_prod+ix.n].sum()) if solution.value_valid else 0.
        check = evaluate(problem,vector,float(c@vector),gross,0.) if solution.value_valid and gross>0 else {'passed':False}
        if not check['passed']:
            if fractional and best is not None:
                history.append(dict(iteration=iteration+1,status=str(status),feasible=False,
                    retained_previous_feasible_solution=True,converged=False))
                converged=False
                break
            write_case_json(folder/'Failed_Solve.json',dict(status=str(status),check=check))
            raise RuntimeError('No verified feasible continuous solution; status='+str(status))
        ratio = float(c@vector)/gross
        if best is None or ratio <= float(c@best)/float(best[ix.nh3_prod:ix.nh3_prod+ix.n].sum()):
            best = vector.copy()
            np.savez_compressed(folder/'Best_Feasible_Solution.npz',x=best,c=c)
        residual = float(objective@vector)
        optimal = status == highspy.HighsModelStatus.kOptimal
        converged = bool(optimal and (not fractional or abs(parameter-ratio)/max(abs(parameter),1)<=.0005))
        history.append(dict(iteration=iteration+1,status=str(status),gross_LCOA=ratio,fractional_residual_CNY=residual,converged=converged))
        print(json.dumps(history[-1]),flush=True)
        if converged:
            break
        parameter = ratio
    if not fractional and not converged:
        raise RuntimeError('LAEC point did not reach an optimal continuous solution.')
    check=evaluate(problem,best,float(c@best),float(best[ix.nh3_prod:ix.nh3_prod+ix.n].sum()),0.)
    np.savez_compressed(folder/'Solution.npz',x=best,c=c)
    write_case_json(folder/'Solver_Status.json',dict(history=history,converged=converged,solver='HiGHS '+solver.version(),matrix_check=check))
    return best,converged,history


def raw_cost_components(problem, x):
    c, _, _, _, ix, _ = problem
    block = lambda start: float(np.dot(c[start:start+ix.n],x[start:start+ix.n]))
    values = dict(annual_synthesis_CNY=float(c[ix.plant_y]*x[ix.plant_y])+block(ix.nh3_prod),
        annual_electrolysis_CNY=float(c[ix.el_cap]*x[ix.el_cap])+block(ix.p_el)+block(ix.water_use),
        annual_battery_CNY=float(c[ix.bat_cap]*x[ix.bat_cap]), annual_hydrogen_storage_CNY=float(c[ix.h2_cap]*x[ix.h2_cap]),
        annual_wind_CNY=float(c[ix.wind_cap]*x[ix.wind_cap]))
    values['annual_numerical_tie_CNY'] = sum(block(getattr(ix,k)) for k in ('bat_ch','bat_dis','el_start','el_stop','p_syn'))
    np.testing.assert_allclose(sum(values.values()),float(c@x),atol=.05,rtol=1e-9)
    return values


def laec_case(case,options):
    wind,_ = resource_profile(options,'Wind_2025')
    ledgers = read_case_json(options.baseline)
    routes = (['Ru+PEM','Fe+PEM'] if case=='pem_laec' else CASE_ROUTES) if options.route=='all' else [options.route]
    if case=='pem_laec' and any(not r.endswith('+PEM') for r in routes):
        raise ValueError('The reduced-cost case is defined only for PEM.')
    rows,checks = [],[]
    for route in routes:
        sid,tech = route.split('+')
        el,syn = copy.deepcopy(ELECTROLYZERS[tech]),copy.deepcopy(SYNTHESES[sid])
        if case=='pem_laec':
            el['installed_capex_CNY_per_kW'] = 2200.
        target = ledgers[route]['baseline_gross_NH3_t']
        if options.targets:
            targets = list(options.targets)
        elif case=='pem_laec':
            targets = [30,50,70,80,85,90]
        else:
            current = pd.read_csv(PUBLISHED/'Fig08_LAEC_Sampled_Results.csv')
            targets = sorted(current.loc[current.route==route,'utilization_target_percent'].astype(int).unique())
        for u in targets:
            if not 0<u<=100:
                raise ValueError('Utilisation must be in (0,100].')
            with route_scope(el):
                problem = build_problem(el,syn,wind,'fixed_300kt',lcoa_parameter=0.,production_target_t=target,
                    wind_utilization_target=u/100.,plant_options_kt_per_y=[475.],plant_option_index=0,
                    wind_capacity_min_kw=0.,wind_capacity_max_kw=MAX_WIND_KW,
                    enforce_electrolyzer_commitment=False,enforce_battery_exclusivity=False,
                    global_design_integrality=False,continuous_design_relaxation=True)
                if options.check:
                    checks.append(dict(route=route,utilization_target_percent=u,production_target_gross_t=target,**matrix_digest(problem)))
                    del problem
                    continue
                folder = options.output/(route.replace('+','_')+f'_{u:03d}')
                x,converged,history = continuous_solve(problem,folder,options)
                ix=problem[-2]
                dispatch,_=extract_plant_output(x,ix,synthesis=syn,nameplate_kt_y=475.)
                dispatch['wind_generation_kW']=wind*x[ix.wind_cap]
                used=dispatch.electrolyzer_power_kW+dispatch.synthesis_power_kW+dispatch.battery_charge_kW-dispatch.battery_discharge_kW
                dispatch['curtailment_kW']=dispatch.wind_generation_kW-used
                used_mwh=float(used.sum()/1000)
                actual=float(used.sum()/dispatch.wind_generation_kW.sum()*100)
                gross=float(dispatch.NH3_production_t_per_h.sum())
                np.testing.assert_allclose(actual,u,atol=1e-6,rtol=0)
                np.testing.assert_allclose(gross,target,atol=max(.001,target*1e-8),rtol=0)
                costs=raw_cost_components(problem,x)
                row=dict(route=route,utilization_target_percent=u,actual_wind_utilization_percent=actual,
                    LAEC_CNY_per_MWh=float(problem[0]@x)/used_mwh,annual_total_cost_CNY=float(problem[0]@x),
                    annual_gross_NH3_t=gross,annual_liquid_NH3_component_t=gross*syn['targeted_terminal']['liquid_recovery_fraction'],
                    utilized_wind_energy_MWh=used_mwh,wind_capacity_MW=x[ix.wind_cap]/1000,
                    electrolyzer_capacity_MW=x[ix.el_cap]/1000,battery_capacity_MWh=x[ix.bat_cap]/1000,
                    hydrogen_storage_Nm3=float(x[ix.h2_cap]),solver_relative_gap=0.,continuous_reoptimization_executed=True,
                    integer_global_optimum=False,PEM_CAPEX_CNY_per_kW=el['installed_capex_CNY_per_kW'],**costs)
                dispatch.to_csv(folder/'Hourly_Dispatch.csv',index=False)
                rows.append(row)
                write_case_table(options.output,'Fig09_Reduced_Cost_Components.csv' if case=='pem_laec' else 'Fig08_LAEC_Sampled_Results.csv',rows)
                if case=='pem_laec':
                    write_case_table(options.output,'Fig09_Reduced_Cost_LAEC.csv',rows)
                print(route,u,row['LAEC_CNY_per_MWh'],flush=True)
    if options.check:
        write_case_json(options.output/'Matrix_Check.json',checks)


@contextmanager
def regional_scope(site):
    offshore = site == 'FJ_PINGTAN_OFFSHORE'
    capex = 6.592e9/464000. if offshore else 3600.
    model_values = dict(WIND_CAPEX_CNY_PER_KW=capex, WIND_FIXED_OM_RATIO=733.125/capex if offshore else .03,
                        WIND_TURBINE_KW=15000. if offshore else 10000.)
    land_values = dict(SITE_ENVELOPE_HA=11400. if offshore else 45300.,ROTOR_DIAMETER_M=240. if offshore else 221.,
        WIND_TURBINE_MW=15. if offshore else 10.,WIND_PERMANENT_HA_PER_MW=0. if offshore else .0692,
        RENEWABLE_LAND_LEASE_CNY_PER_MU_YEAR=0. if offshore else 400.)
    previous = {k:globals()[k] for k in model_values}
    previous_land = {k:globals()['land_'+k] for k in land_values}
    area = (25000./15./710.)/1000.
    rent = area*15.*land_values['RENEWABLE_LAND_LEASE_CNY_PER_MU_YEAR']
    pv_annual = 2500.*annualized_capital_factor(25.)+25.+rent
    try:
        globals().update(model_values)
        for k,v in land_values.items():
            globals()['land_'+k] = v
            setattr(land_only,k,v)
        yield dict(pv_annual_cost_per_kw=pv_annual,pv_ha_per_kw=area,
                   permanent_ha_per_kw=land_values['WIND_PERMANENT_HA_PER_MW']/1000)
    finally:
        globals().update(previous)
        for k,v in previous_land.items():
            globals()['land_'+k] = v
            setattr(land_only,k,v)


def regional_case(case,options):
    sites = options.sites or (['IM_KESHIKETENG','IM_SIZIWANG'] if case=='wind_pv' else ['IM_KESHIKETENG','IM_SIZIWANG','XZ_NGARI','FJ_PINGTAN_OFFSHORE'])
    mapping = dict(IM_KESHIKETENG='Keshiketeng',IM_SIZIWANG='Siziwang',XZ_NGARI='Ngari',FJ_PINGTAN_OFFSHORE='Pingtan')
    rows,checks = [],[]
    for site in sites:
        shares = options.shares or ([0.,.025,.05,.075,.10,.15,.20] if case=='wind_pv' else [1. if site=='XZ_NGARI' else 0.])
        wind,data = resource_profile(options,mapping[site]+'_2025')
        solar = data.pv_per_kw.to_numpy(float)
        el,syn = ELECTROLYZERS['ALK'],SYNTHESES['Ru']
        with route_scope(el),regional_scope(site) as setting:
            lo = dict(wind_ha_per_kw=land_only.wind_spacing_control_ha_per_turbine()/WIND_TURBINE_KW,
                process_areas_ha=[land_only.process_site_ha(475.)],site_envelope_ha=land_only.SITE_ENVELOPE_HA,
                pv_ha_per_kw=setting['pv_ha_per_kw'])
            for share in shares:
                original = build_problem(el,syn,wind,'variable_scale',lcoa_parameter=0.,plant_option_index=0,
                    plant_options_kt_per_y=[475.],wind_capacity_min_kw=0.,wind_capacity_max_kw=MAX_WIND_KW,
                    enforce_electrolyzer_commitment=True,enforce_battery_exclusivity=False,
                    global_design_integrality=False,continuous_design_relaxation=True)
                pv = add_pv_block(original,solar,annual_cost_per_kw=setting['pv_annual_cost_per_kw'],capacity_max_kw=MAX_WIND_KW)
                pv = add_shared_land(pv,original[-2],lo,setting['permanent_ha_per_kw'])
                pv = fix_pv_share(pv,original[-2],share)
                if len(pv.indicator_constraints):
                    raise ValueError('Regional approximation must not retain unexpanded indicator rows.')
                pv = replace(pv,integrality=np.zeros_like(pv.integrality))
                problem = as_model_problem(pv,original[-2],original[-1])
                ix,terms = problem[-2:]
                if options.check:
                    checks.append(dict(site=site,pv_share=share,land=lo,**matrix_digest(problem)))
                    print(site,share,checks[-1]['sha256'],flush=True)
                    continue
                folder = options.output/(mapping[site]+f'_PV_{share:.3f}')
                x,converged,history = continuous_solve(problem,folder,options,fractional=True)
                x,cleanup = remove_battery_circulation(x,ix,BESS_CHARGE_EFFICIENCY,BESS_DISCHARGE_EFFICIENCY)
                if not audit_witness(pv,x)['passed']:
                    raise RuntimeError('Regional solution failed full matrix validation after battery cleanup.')
                land = shared_land_audit(pv,x,ix,lo,setting['permanent_ha_per_kw'])
                if not land['passed']:
                    raise RuntimeError('Shared land boundary violated.')
                dispatch,_ = extract_plant_output(x,ix,synthesis=syn,nameplate_kt_y=475.)
                dispatch['wind_generation_kW'] = wind*x[ix.wind_cap]
                dispatch['PV_generation_kW'] = solar*x[ix.pv_cap]
                dispatch['PV_used_kW'] = x[ix.pv_injection:ix.pv_injection+ix.n]
                dispatch['PV_curtailed_kW'] = x[ix.pv_curtailment:ix.pv_curtailment+ix.n]
                used = dispatch.electrolyzer_power_kW+dispatch.synthesis_power_kW+dispatch.battery_charge_kW-dispatch.battery_discharge_kW
                generation = dispatch.wind_generation_kW+dispatch.PV_generation_kW
                dispatch['total_curtailment_kW'] = generation-used
                costs = continuous_base_costs(model,x,ix,terms,el,syn)
                pv_cost = float(x[ix.pv_cap])*setting['pv_annual_cost_per_kw']
                annual = costs['annual_total_cost_CNY']+pv_cost
                np.testing.assert_allclose(annual,float(problem[0]@x),atol=.1,rtol=1e-8)
                q = float(dispatch.liquid_NH3_component_t_per_h.sum())
                row = dict(site=mapping[site],PV_share_percent=100*share,LCOA=annual/q,
                    curtailment=float((generation-used).sum()/generation.sum()*100),wind_MW=x[ix.wind_cap]/1000,pv_MW=x[ix.pv_cap]/1000,
                    electrolyzer_MW=x[ix.el_cap]/1000,battery_MWh=x[ix.bat_cap]/1000,hydrogen_storage_Nm3=x[ix.h2_cap],
                    liquid_NH3_t=q,fractional_iteration_converged=converged,
                    renewable=(costs['annual_wind_CNY']+pv_cost)/q,
                    electrolysis=sum(costs[k] for k in ['annual_electrolyzer_CNY','annual_stack_replacement_CNY','annual_water_CNY',
                        'annual_electrolyzer_transition_tiebreaker_CNY','annual_surrogate_power_tiebreaker_CNY'])/q,
                    battery=(costs['annual_battery_CNY']+costs['annual_battery_tiebreaker_CNY'])/q,hydrogen=costs['annual_h2_tank_CNY']/q,
                    synthesis=(costs['annual_synthesis_process_CNY']+costs['annual_catalyst_CNY'])/q)
                dispatch.to_csv(folder/'Hourly_Dispatch.csv',index=False)
                write_case_json(folder/'Checks.json',dict(land=land,battery_cleanup=cleanup,matrix=audit_witness(pv,x)))
                rows.append(row)
                write_case_table(options.output,'Fig11_Wind_PV_Results.csv' if case=='wind_pv' else 'Fig11_Regional_Results.csv',rows)
                print(site,share,row['LCOA'],converged,flush=True)
    if options.check:
        write_case_json(options.output/'Matrix_Check.json',checks)


def run_case(case):
    global AUDIT_DIR, RUN_DIR, MIP_TIME_LIMIT_S, GUROBI_CL
    options = case_parser(case).parse_args()
    options.output = options.output.resolve()
    if options.output == PUBLISHED.resolve() or PUBLISHED.resolve() in options.output.parents:
        raise ValueError('Published figure inputs must not be overwritten.')
    options.output.mkdir(parents=True, exist_ok=True)
    RUN_DIR = options.output
    AUDIT_DIR = options.output / 'Solver_Logs'
    AUDIT_DIR.mkdir(exist_ok=True)
    MIP_TIME_LIMIT_S = options.time_limit
    os.environ['WIND_NH3_GUROBI_THREADS'] = str(options.threads)
    if options.gurobi:
        GUROBI_CL = options.gurobi
    if case in ('baseline', 'fixed_production', 'fixed_wind', 'weather'):
        formal_case(case, options)
    elif case in ('uncertainty', 'pem_cost', 'coal', 'sensitivity'):
        economic_case(case, options)
    elif case in ('laec', 'pem_laec'):
        laec_case(case, options)
    elif case in ('regional', 'wind_pv'):
        regional_case(case, options)
    else:
        raise ValueError(case)


if __name__ == '__main__':
    run_case('baseline')
