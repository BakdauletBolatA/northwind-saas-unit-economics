"""Общие фикстуры: витрина, конфиг и материализованные таблицы."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "warehouse" / "northwind.db"
TABLES = ROOT / "outputs" / "tables"

pytestmark = pytest.mark.skipif(not DB.exists(), reason="нет витрины — сначала ./run_all.sh")


@pytest.fixture(scope="session")
def config() -> dict:
    return yaml.safe_load((ROOT / "config" / "assumptions.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def con():
    if not DB.exists():
        pytest.skip("нет витрины — сначала ./run_all.sh")
    connection = sqlite3.connect(DB)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def table():
    def load(name: str) -> pd.DataFrame:
        path = TABLES / f"{name}.csv"
        if not path.exists():
            pytest.skip(f"нет {path.name} — сначала ./run_all.sh")
        return pd.read_csv(path)
    return load


@pytest.fixture(scope="session")
def readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")
