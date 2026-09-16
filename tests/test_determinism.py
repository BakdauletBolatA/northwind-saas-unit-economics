"""Прогон должен воспроизводить закоммиченные артефакты.

Не побайтово: последний бит суммы в CSV зависит от версии BLAS и от машины, и
требовать точного совпадения — значит получить красный CI на ровном месте.
Числа сравниваются с относительным допуском 1e-6, текстовые колонки — точно.

Это и есть проверка заявления в README о том, что прогон детерминирован: seed
зафиксирован, и результат прогона совпадает с тем, что лежит в репозитории.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
TABLES = ROOT / "outputs" / "tables"
RTOL = 1e-6

# Эти два файла считает не пайплайн, а LibreOffice: он пересчитывает книгу и
# отдаёт расхождение с питоновской моделью. Разные сборки LibreOffice (macOS и
# ubuntu-latest) дают разные последние центы, и требовать от них совпадения до
# 1e-6 значит проверять версию стороннего табличного движка, а не свой код.
# Поэтому здесь проверяется то, ради чего эти файлы существуют: вердикт тот же,
# а остаток по-прежнему пренебрежимо мал против сумм в миллионы.
SPREADSHEET_OUTPUTS = {"excel_vs_sql_reconciliation.csv", "scenario_switch_test.csv"}
SPREADSHEET_ATOL = 1.0  # доллар: настоящая поломка формулы промахнётся на порядки


def committed(path: Path) -> pd.DataFrame | None:
    """Версия файла из HEAD, или None, если файл не под контролем версий."""
    rel = path.relative_to(ROOT).as_posix()
    done = subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{rel}"],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return None
    return pd.read_csv(io.StringIO(done.stdout))


TABLE_FILES = sorted(TABLES.glob("*.csv")) if TABLES.exists() else []


@pytest.mark.parametrize("path", TABLE_FILES, ids=[p.name for p in TABLE_FILES])
def test_the_run_reproduces_the_committed_table(path: Path) -> None:
    reference = committed(path)
    if reference is None:
        pytest.skip(f"{path.name} ещё не в репозитории")

    current = pd.read_csv(path)
    assert list(current.columns) == list(reference.columns), f"{path.name}: колонки разошлись"
    assert len(current) == len(reference), f"{path.name}: число строк разошлось"

    spreadsheet = path.name in SPREADSHEET_OUTPUTS
    for column in current.columns:
        left, right = current[column], reference[column]
        if pd.api.types.is_numeric_dtype(left) and pd.api.types.is_numeric_dtype(right):
            if spreadsheet:
                assert np.allclose(
                    left.to_numpy(), right.to_numpy(), rtol=1e-6, atol=SPREADSHEET_ATOL,
                    equal_nan=True,
                ), f"{path.name}: колонка {column} ушла больше чем на ${SPREADSHEET_ATOL:g}"
            else:
                assert np.allclose(left.to_numpy(), right.to_numpy(), rtol=RTOL, equal_nan=True), (
                    f"{path.name}: колонка {column} разошлась больше чем на {RTOL:g}"
                )
        else:
            assert left.astype(str).equals(right.astype(str)), (
                f"{path.name}: колонка {column} разошлась"
            )


def test_the_spreadsheet_reconciliation_still_passes_every_check() -> None:
    """Смягчённый допуск выше не должен прятать сломанную формулу.

    Остатки в этих файлах — центы против миллионов. Если книга разъедется с
    моделью по-настоящему, вердикт перестанет быть PASS, и это ловится точно.
    """
    path = TABLES / "excel_vs_sql_reconciliation.csv"
    if not path.exists():
        pytest.skip("нет отчёта сверки — сначала ./run_all.sh с LibreOffice")
    table = pd.read_csv(path)
    assert set(table["status"]) == {"PASS"}, table[table["status"] != "PASS"]
    assert (table["difference"].abs() <= table["tolerance"]).all()


def test_there_are_tables_to_check() -> None:
    """Страховка: пустой список параметров превратил бы этот файл в ноль проверок."""
    assert len(TABLE_FILES) >= 15
