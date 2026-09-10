from datetime import date

from tools.sync_ozon_profit import ads_day_matrix, finance_matrix, month_chunks, parse_ads_daily_csv


def test_month_chunks_never_cross_calendar_month():
    chunks = list(month_chunks(date(2026, 1, 28), date(2026, 3, 3)))
    assert chunks == [
        (date(2026, 1, 28), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 3)),
    ]


def test_finance_matrix_does_not_duplicate_multi_sku_operation_amount():
    operation = {
        "operation_id": 1,
        "operation_date": "2026-09-01 12:00:00",
        "type": "orders",
        "operation_type": "OperationAgentDeliveredToCustomer",
        "operation_type_name": "Delivered",
        "amount": 500,
        "posting": {"posting_number": "ABC-1"},
        "items": [{"sku": 111}, {"sku": 222}],
    }
    matrix = finance_matrix([operation])
    assert len(matrix) == 2
    assert matrix[1][5] == ""
    assert matrix[1][6] == 500


def test_parse_ads_daily_csv_and_skip_total_row():
    text = (
        "ID кампании;Название кампании;Дата;Показы;Клики;Расход, ₽;Средняя ставка;Заказы, шт;Сумма заказов, ₽\n"
        "37197616;YG-002 CPC;2026-09-09;2458;165;419,35;2,54;4;2600,00\n"
        ";Итого;;2458;165;419,35;;4;2600,00\n"
    )
    rows = parse_ads_daily_csv(text)
    assert len(rows) == 1
    row = rows[0]
    assert row["campaign_id"] == "37197616"
    assert row["date"] == "2026-09-09"
    assert row["spend"] == 419.35
    assert row["impressions"] == 2458
    assert row["clicks"] == 165
    assert row["orders"] == 4
    assert row["revenue"] == 2600.0


def test_ads_day_matrix_aggregates_campaigns_without_double_counting():
    rows = [
        {
            "date": "2026-09-09",
            "campaign_id": "1",
            "campaign_name": "A",
            "spend": 10.5,
            "impressions": 100,
            "clicks": 10,
            "orders": 1,
            "revenue": 100,
            "raw": {},
        },
        {
            "date": "2026-09-09",
            "campaign_id": "2",
            "campaign_name": "B",
            "spend": 5.5,
            "impressions": 50,
            "clicks": 5,
            "orders": 2,
            "revenue": 200,
            "raw": {},
        },
    ]
    matrix = ads_day_matrix(rows)
    assert matrix[1][0] == "2026-09-09"
    assert matrix[1][1] == 16.0
    assert matrix[1][2] == 150
    assert matrix[1][3] == 15
    assert matrix[1][4] == 3
    assert matrix[1][5] == 300.0
