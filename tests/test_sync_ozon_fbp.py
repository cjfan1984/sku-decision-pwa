from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sync_ozon_fbp import (
    fbp_postings_matrix,
    fbp_products_matrix,
    inventory_matrix,
    stock_matrix,
)


def test_stock_and_inventory_matrix_keep_fbp_separate_without_duplicate_offer():
    items = [{
        "offer_id": "YG-002",
        "product_id": 77,
        "stocks": [
            {"type": "fbs", "sku": 1001, "present": 3, "reserved": 1},
            {"type": "fbp", "sku": 1002, "present": 5, "reserved": 2},
        ],
    }]
    assert len(stock_matrix(items)) == 3
    row = inventory_matrix(items)[1]
    assert row[0] == "YG-002"
    assert row[2:8] == [5, 2, 3, 1, 0, 0]
    assert row[10:12] == [8, 3]


def test_stock_dict_shape_is_supported():
    items = [{
        "offer_id": "A",
        "product_id": 1,
        "stocks": {"fbp": {"type": "fbp", "sku": 9, "present": 2, "reserved": 0}},
    }]
    assert inventory_matrix(items)[1][2] == 2


def test_fbp_product_union_preserves_zero_stock_recent_product():
    items = [{
        "offer_id": "YG-004",
        "product_id": 44,
        "stocks": [{"type": "fbp", "sku": 444, "present": 2, "reserved": 1}],
    }]
    postings = [
        {
            "posting_number": "P1",
            "order_date": "2026-10-01T10:00:00Z",
            "status": "delivered",
            "products": [{
                "offer_id": "YG-004",
                "sku": 444,
                "name": "4-head stirrer",
                "quantity": 2,
            }],
        },
        {
            "posting_number": "P2",
            "order_date": "2026-10-02T11:00:00Z",
            "status": "awaiting_delivery",
            "products": [{
                "offer_id": "OLD-FBP",
                "sku": 555,
                "name": "old",
                "quantity": 1,
            }],
        },
    ]
    assert len(fbp_postings_matrix(postings)) == 3
    rows = {r[0]: r for r in fbp_products_matrix(items, postings)[1:]}
    assert rows["YG-004"][3:7] == [2, 1, 2, 1]
    assert rows["YG-004"][9] == "posting+stock"
    assert rows["OLD-FBP"][3] == 0
    assert rows["OLD-FBP"][5] == 1
