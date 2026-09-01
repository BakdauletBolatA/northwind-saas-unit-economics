#!/usr/bin/env bash
# Northwind Cloud — full pipeline. Deterministic: same seed, same outputs.
set -euo pipefail
cd "$(dirname "$0")"

step () { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

step "1/8  Generate synthetic data (seeded, deterministic)"
python3 src/01_generate_data.py

step "2/8  Build the SQLite star schema and ETL audit"
python3 src/02_build_warehouse.py

step "3/8  Run the SQL library"
python3 src/03_run_sql_library.py

step "4/8  Forecast bake-off and 18-month cash model"
python3 src/04_cashflow_model.py

step "5/8  Build the Excel model"
python3 src/05_build_excel_model.py

step "6/8  Validate (needs LibreOffice Calc: apt-get install -y libreoffice-calc)"
python3 src/06_validate.py

step "7/8  Export the Power BI star"
python3 src/07_export_powerbi.py

step "8/8  Regenerate the data dictionary and cleaning rules"
python3 src/08_generate_docs.py

printf '\n\033[1mDone.\033[0m Deliverables:\n'
printf '  outputs/excel/northwind_unit_economics.xlsx\n'
printf '  outputs/tables/*.csv        (SQL results, backtest, scenarios, checks)\n'
printf '  outputs/powerbi/*.csv       (star-schema extracts)\n'
printf '  docs/CEO_MEMO.md            (the answer)\n'
