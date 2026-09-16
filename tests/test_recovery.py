"""Анализ обязан находить заложенное в данных, а не читать его из конфига.

Данные синтетические, и эффекты в них заложены самим проектом — это ровно тот
случай, когда «находка» ничего не стоит: генератор посадил, анализ нашёл.
Единственное, что здесь можно доказать, — что цепочка между данными и выводом
работает: измеренное по витрине сходится с параметром, который его породил,
и сходится с допуском, а не по формулировке.

Каждый тест меряет по складу или по материализованным таблицам и сравнивает с
config/assumptions.yml.
"""

from __future__ import annotations

import pandas as pd
import pytest


def test_the_segments_churn_at_the_rates_they_were_given(con, config) -> None:
    """Логотипная отвальность по сегментам — основной драйвер всей экономики."""
    measured = pd.read_sql(
        """
        SELECT segment,
               SUM(CASE WHEN movement_type = 'churn' THEN 1 ELSE 0 END) * 1.0
               / SUM(CASE WHEN prev_mrr > 0 THEN 1 ELSE 0 END) AS monthly_churn
        FROM v_customer_month
        WHERE month BETWEEN '2024-01' AND '2024-12'   -- до посаженных событий
        GROUP BY segment
        """,
        con,
    ).set_index("segment")["monthly_churn"]

    # Базовая ставка модулируется множителем по стажу, поэтому точного совпадения
    # тут быть не может: требуем тот же порядок величины и верный порядок сегментов.
    for segment, params in config["segments"].items():
        planted = params["base_monthly_logo_churn"]
        assert 0.5 * planted <= measured[segment] <= 2.0 * planted, (
            f"{segment}: заложено {planted:.4f}, измерено {measured[segment]:.4f}"
        )
    # Порядок сегментов по отвальности должен восстанавливаться точно.
    assert measured["SMB"] > measured["MidMarket"] > measured["Enterprise"]


def test_the_average_deal_sizes_come_back(con, config) -> None:
    measured = pd.read_sql(
        "SELECT segment, AVG(new_acv) AS acv FROM v_new_business GROUP BY segment", con
    ).set_index("segment")["acv"]
    for segment, params in config["segments"].items():
        assert measured[segment] == pytest.approx(params["acv_mean"], rel=0.25), (
            f"{segment}: заложено {params['acv_mean']}, измерено {measured[segment]:.0f}"
        )


def test_the_paid_social_collapse_is_visible_in_the_cohorts(con, config) -> None:
    """E1: с ноября 2024 когорты Paid Social должны и чаще уходить, и быть мельче."""
    event = next(e for e in config["events_log"] if e["id"] == "E1")
    start = event["start_month"]

    acv = pd.read_sql(
        """
        SELECT CASE WHEN signup_month < ? THEN 'before' ELSE 'after' END AS era,
               AVG(new_acv) AS acv, COUNT(*) AS wins
        FROM v_new_business WHERE channel = 'paid_social' GROUP BY era
        """,
        con, params=(start,),
    ).set_index("era")

    ratio = acv.loc["after", "acv"] / acv.loc["before", "acv"]
    planted = event["params"]["acv_multiplier"]
    assert ratio == pytest.approx(planted, rel=0.25), (
        f"размер сделки после события: заложено x{planted}, измерено x{ratio:.2f}"
    )
    assert acv.loc["after", "wins"] > acv.loc["before", "wins"], "объём должен был вырасти"


def test_the_paid_social_cohorts_churn_faster_after_the_collapse(con, config) -> None:
    event = next(e for e in config["events_log"] if e["id"] == "E1")
    churn = pd.read_sql(
        """
        SELECT CASE WHEN signup_month < ? THEN 'before' ELSE 'after' END AS era,
               SUM(CASE WHEN movement_type = 'churn' THEN 1 ELSE 0 END) * 1.0
               / SUM(CASE WHEN prev_mrr > 0 THEN 1 ELSE 0 END) AS monthly_churn
        FROM v_customer_month
        WHERE channel = 'paid_social' AND is_pre_window = 0
        GROUP BY era
        """,
        con, params=(event["start_month"],),
    ).set_index("era")["monthly_churn"]

    observed = churn["after"] / churn["before"]
    planted = event["params"]["churn_multiplier"]
    assert observed == pytest.approx(planted, rel=0.3), (
        f"заложено x{planted}, измерено x{observed:.2f}"
    )


def test_the_enterprise_expansion_wave_shows_up_as_nrr_not_as_logos(con, config) -> None:
    """E2: продукт вышел в июне 2025 — расширение, а не новые логотипы."""
    event = next(e for e in config["events_log"] if e["id"] == "E2")
    rates = pd.read_sql(
        """
        SELECT CASE WHEN month < ? THEN 'before' ELSE 'after' END AS era,
               SUM(CASE WHEN movement_type = 'expansion' THEN 1 ELSE 0 END) * 1.0
               / COUNT(*) AS expansion_rate
        FROM v_customer_month WHERE segment = 'Enterprise' GROUP BY era
        """,
        con, params=(event["start_month"],),
    ).set_index("era")["expansion_rate"]

    observed = rates["after"] / rates["before"]
    planted = event["params"]["expansion_rate_multiplier"]
    assert observed == pytest.approx(planted, rel=0.5), (
        f"расширение Enterprise: заложено x{planted}, измерено x{observed:.2f}"
    )


def test_the_smb_price_rise_lifts_arpa_and_churn_together(con, config) -> None:
    """E3: подняли цену на 18% — выросла и выручка на клиента, и отвал."""
    event = next(e for e in config["events_log"] if e["id"] == "E3")
    start = event["start_month"]
    era = "CASE WHEN month < ? THEN 'before' ELSE 'after' END"
    arpa = pd.read_sql(
        f"SELECT {era} AS era, AVG(mrr) AS arpa FROM v_customer_month "
        "WHERE segment = 'SMB' AND mrr > 0 GROUP BY era",
        con, params=(start,),
    ).set_index("era")["arpa"]
    # Отвал считается по строкам, где клиент был жив В НАЧАЛЕ месяца: у ушедшего
    # mrr уже ноль, поэтому фильтр mrr > 0 обнулил бы числитель.
    churn = pd.read_sql(
        f"SELECT {era} AS era, "
        "SUM(CASE WHEN movement_type = 'churn' THEN 1 ELSE 0 END) * 1.0 "
        "/ SUM(CASE WHEN prev_mrr > 0 THEN 1 ELSE 0 END) AS churn "
        "FROM v_customer_month WHERE segment = 'SMB' GROUP BY era",
        con, params=(start,),
    ).set_index("era")["churn"]

    uplift = arpa["after"] / arpa["before"] - 1
    assert uplift > config["events_log"][2]["params"]["price_uplift"] / 2, (
        f"ARPA после подорожания выросла всего на {uplift:.1%}"
    )
    assert churn["after"] > churn["before"], (
        "повышение цены обязано было поднять отвал — иначе событие не восстановлено"
    )


def test_the_sdr_saturation_curve_is_measured_not_assumed(con, config, table) -> None:
    """E4: команда выросла с 4 до 9 — встреч на человека стало меньше.

    Это единственное прямое свидетельство того, что дадут SDR №10-18, и вся
    рекомендация против найма девяти человек стоит на нём.
    """
    sdr = table("Q11_sdr_productivity")
    small = sdr[sdr["sdr_heads"] <= config["sdr"]["saturation_team_size"]]["meetings_per_rep"]
    large = sdr[sdr["sdr_heads"] >= 8]["meetings_per_rep"]

    assert small.mean() == pytest.approx(config["sdr"]["meetings_per_rep_small_team"], rel=0.2)
    assert large.mean() < small.mean(), "насыщение должно быть видно в данных"
    decay = 1 - large.mean() / small.mean()
    assert decay > 0.15, f"падение продуктивности всего {decay:.1%} — событие не восстановлено"


def test_the_billing_migration_double_post_is_quarantined(con, config) -> None:
    """E6: движок биллинга продублировал месяц счетов — ETL обязан это поймать."""
    month = config["data_quality"]["duplicate_month"]
    quarantined = pd.read_sql(
        "SELECT reason, COUNT(*) AS n FROM dq_quarantine GROUP BY reason", con
    )
    assert not quarantined.empty, "карантин пуст — дефекты не ловятся"
    assert quarantined["n"].sum() > 100

    bridge = pd.read_sql(
        "SELECT month, SUM(mrr) AS mrr FROM v_customer_month GROUP BY month ORDER BY month", con
    ).set_index("month")["mrr"]
    # После дедупликации февраль 2026 не должен торчать вверх относительно соседей.
    neighbours = (bridge.loc["2026-01"] + bridge.loc["2026-03"]) / 2
    assert bridge.loc[month] == pytest.approx(neighbours, rel=0.1), (
        "февральский дубль не вычищен: месяц выбивается из ряда"
    )


def test_the_pre_window_base_is_excluded_from_cac(con, config) -> None:
    """Самый частый способ приукрасить CAC — оставить в знаменателе старую базу."""
    counts = pd.read_sql(
        "SELECT is_pre_window, COUNT(*) AS n FROM dim_customer GROUP BY is_pre_window", con
    ).set_index("is_pre_window")["n"]
    assert counts[1] == config["legacy_base"]["n_customers"]

    in_cac = pd.read_sql("SELECT COUNT(*) AS n FROM v_new_business", con)["n"].iloc[0]
    assert in_cac == counts[0], "в CAC попали клиенты, у которых нет затрат на привлечение"
