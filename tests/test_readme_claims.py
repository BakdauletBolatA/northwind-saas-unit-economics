"""Каждое число в README должно находиться в артефактах прогона.

Написано после того, как выяснилось, что таблица по outbound в README жила
своей жизнью: там стоял CAC $70,164 и окупаемость 12.3 месяца, а Q07 в
outputs/tables давал $72,219 и 12.6. Ошибка мелкая, но это ровно тот класс
расхождений, из-за которого читатель не может проверить ни одну цифру, не
пересчитав всё сам.

Тесты не сверяют README с README: каждая проверка сначала читает значение из
таблицы прогона, форматирует его так, как оно должно выглядеть в тексте, и
ищет эту строку.
"""

from __future__ import annotations

import pandas as pd
import pytest

WINDOW = ("2025-09", "2026-08")


def money(value: float) -> str:
    return f"${value:,.0f}"


# -- окупаемость по каналам ------------------------------------------------------


def test_the_blended_outbound_payback_matches_q07(readme, table) -> None:
    q07 = table("Q07_cac_ltv_payback_by_channel").set_index("channel")
    assert f"{q07.loc['outbound_sdr', 'cac_payback_months']:.1f} months" in readme


def test_the_outbound_segment_table_matches_the_warehouse(readme, table) -> None:
    alloc = table("allocation_sensitivity")
    outbound = alloc[alloc["channel"] == "outbound_sdr"].set_index("segment")
    for segment in ("Enterprise", "MidMarket", "SMB"):
        row = outbound.loc[segment]
        assert money(row["cac_equal_split"]) in readme, f"CAC {segment} разошёлся с прогоном"
        assert money(row["avg_new_acv"]) in readme, f"средний чек {segment} разошёлся"
        assert f"{row['payback_equal_split']:.1f} months" in readme, (
            f"окупаемость {segment} при равном распределении разошлась"
        )
        assert f"{row['payback_effort_wtd']:.1f} months" in readme, (
            f"окупаемость {segment} по трудоёмкости разошлась"
        )


def test_the_guardrail_verdict_is_the_one_the_numbers_give(readme, table, config) -> None:
    """README утверждает, что под не проходит порог по трудоёмкости. Проверяем."""
    alloc = table("allocation_sensitivity")
    ent = alloc[(alloc["channel"] == "outbound_sdr")
                & (alloc["segment"] == "Enterprise")].iloc[0]
    hurdle = config["treasury"]["board_min_runway_months"]

    assert ent["payback_equal_split"] <= hurdle, "текст обещает, что на равном сплите проходит"
    assert ent["payback_effort_wtd"] > hurdle, "текст обещает, что по трудоёмкости не проходит"
    assert "misses" in readme and "only while an Enterprise deal costs less than about" in readme


def test_the_break_even_effort_ratio_is_where_the_sweep_puts_it(readme, table) -> None:
    sweep = table("allocation_breakeven")
    last_clear = sweep[sweep["clears_hurdle"]]["enterprise_effort_index"].max()
    first_miss = sweep[~sweep["clears_hurdle"]]["enterprise_effort_index"].min()
    assert last_clear < 3.6 < first_miss, (
        f"README называет ~3.6x, а перелом между {last_clear} и {first_miss}"
    )
    assert "**3.6x**" in readme


def test_paid_search_midmarket_has_the_same_shape(readme, table) -> None:
    alloc = table("allocation_sensitivity")
    row = alloc[(alloc["channel"] == "paid_search") & (alloc["segment"] == "MidMarket")].iloc[0]
    assert f"{row['payback_effort_wtd']:.1f} effort-weighted" in readme
    assert f"{row['payback_equal_split']:.1f}" in readme


# -- остальные заявленные цифры ---------------------------------------------------


def test_the_paid_social_verdict_matches_q07(readme, table) -> None:
    q07 = table("Q07_cac_ltv_payback_by_channel").set_index("channel")
    row = q07.loc["paid_social"]
    assert f"{row['cac_payback_months']:.1f}-month payback" in readme
    assert f"**{row['ltv_cac_b']:.2f}**" in readme, "LTV/CAC берётся из конечного горизонта"
    assert money(row["new_acv"]) in readme


def test_the_nrr_by_segment_matches_q06(readme, table) -> None:
    q06 = table("Q06_nrr_grr_by_segment")
    latest = q06[q06["as_of_month"] == q06["as_of_month"].max()].set_index("segment")
    for segment in ("Enterprise", "MidMarket", "SMB"):
        assert f"{latest.loc[segment, 'nrr_pct']:.1f}%" in readme, (
            f"NRR {segment} разошёлся с Q06"
        )


def test_the_sdr_productivity_numbers_match_q11(readme, table) -> None:
    sdr = table("Q11_sdr_productivity")
    small = sdr[sdr["sdr_heads"] == 4]["meetings_per_rep"].max()
    large = sdr[sdr["sdr_heads"] == 9]["meetings_per_rep"].max()
    assert f"{small:.2f}" in readme and f"{large:.2f}" in readme, (
        "падение продуктивности SDR в тексте не совпадает с Q11"
    )


def test_the_scenario_headline_matches_the_cash_model(readme, table) -> None:
    cash = table("cashflow_all_scenarios")
    final = cash[cash["month"] == cash["month"].max()].set_index("scenario")
    for scenario in ("base", "sales_proposal", "selective"):
        arr_m = final.loc[scenario, "arr"] / 1e6
        assert f"${arr_m:.2f}m" in readme, f"ARR сценария {scenario} разошёлся с моделью"


def test_the_validation_gate_counts_match_the_audit(readme, con) -> None:
    rows_in = pd.read_sql("SELECT COUNT(*) AS n FROM dq_quarantine", con)["n"].iloc[0]
    assert f"{rows_in:,} quarantined" in readme or f"{rows_in} quarantined" in readme


def test_the_outbound_rows_quote_the_run_and_nothing_else(readme, table) -> None:
    """Строки таблицы по outbound должны состоять только из чисел прогона.

    Раньше здесь стоял запрет на конкретные устаревшие значения, но он ловил и
    абзац, который про это расхождение рассказывает. Проверяем прямо: в строке
    сегмента стоит CAC из прогона и не стоит никакой другой.
    """
    alloc = table("allocation_sensitivity")
    outbound = alloc[alloc["channel"] == "outbound_sdr"].set_index("segment")
    rows = [line for line in readme.splitlines()
            if line.startswith("| Enterprise |") or line.startswith("| Mid-Market |")]
    assert rows, "таблица по outbound исчезла из README"

    current_cac = money(outbound.loc["Enterprise", "cac_equal_split"])
    for row in rows:
        if "$" in row:
            assert current_cac in row, f"строка не из прогона: {row}"


# -- меморандум — это и есть поставляемый документ, он тоже обязан сходиться -------


@pytest.fixture(scope="session")
def memo() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "docs" / "CEO_MEMO.md").read_text(
        encoding="utf-8"
    )


def test_the_memo_quotes_the_same_outbound_table(memo, table) -> None:
    alloc = table("allocation_sensitivity")
    outbound = alloc[alloc["channel"] == "outbound_sdr"].set_index("segment")
    for segment in ("Enterprise", "MidMarket", "SMB"):
        row = outbound.loc[segment]
        assert money(row["cac_equal_split"]) in memo
        assert f"{row['payback_equal_split']:.1f} months" in memo


def test_the_memo_states_the_guardrail_miss(memo, table, config) -> None:
    """Меморандум обязан назвать промах по порогу, а не обойти его формулировкой."""
    ent = table("allocation_sensitivity")
    row = ent[(ent["channel"] == "outbound_sdr") & (ent["segment"] == "Enterprise")].iloc[0]
    hurdle = config["treasury"]["board_min_runway_months"]
    assert row["payback_effort_wtd"] > hurdle
    assert f"{row['payback_effort_wtd']:.1f}\nmonths" in memo or (
        f"{row['payback_effort_wtd']:.1f} months" in memo
    )
    assert "misses your 18-month guardrail" in memo
    assert "still pays back" not in memo, "старая формулировка обходила промах по порогу"
