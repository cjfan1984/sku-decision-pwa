from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import gspread
import requests
from google.oauth2.service_account import Credentials

SELLER_BASE = "https://api-seller.ozon.ru"
STOCKS_PATH = "/v4/product/info/stocks"
FBP_POSTINGS_PATH = "/v1/posting/fbp/list"
HTTP_TIMEOUT = (10, 45)
HTTP_ATTEMPTS = 4

STOCK_HEADER = [
    "offer_id", "product_id", "stock_type", "sku", "present", "reserved",
    "shipment_type", "raw_json",
]
INVENTORY_HEADER = [
    "offer_id", "product_id",
    "fbp_present", "fbp_reserved",
    "fbs_present", "fbs_reserved",
    "fbo_present", "fbo_reserved",
    "other_present", "other_reserved",
    "total_present", "total_reserved", "ozon_skus", "stock_types",
]
FBP_POSTING_HEADER = [
    "posting_number", "order_date", "status", "order_id", "order_number",
    "offer_id", "sku", "name", "quantity", "seller_price", "currency",
    "customer_price", "provider_id", "raw_json",
]
FBP_PRODUCT_HEADER = [
    "offer_id", "sku", "name", "current_present", "current_reserved",
    "recent_order_qty", "recent_postings", "last_order_date", "last_status", "source",
]
STATUS_HEADER = [
    "run_at_utc", "seller_client_id", "stocks_status", "stock_items",
    "fbp_status", "fbp_postings", "fbp_products", "fbp_from", "fbp_to", "message",
]


@dataclass(frozen=True)
class Config:
    seller_client_id: str
    seller_api_key: str


class SyncError(RuntimeError):
    pass


def google_credentials() -> Credentials:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise SyncError("missing GOOGLE_SERVICE_ACCOUNT_JSON")
    return Credentials.from_service_account_info(
        json.loads(raw),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )


def google_book(spreadsheet_id: str):
    return gspread.authorize(google_credentials()).open_by_key(spreadsheet_id)


def get_or_create_worksheet(book, title: str, rows: int, cols: int):
    try:
        return book.worksheet(title)
    except gspread.WorksheetNotFound:
        return book.add_worksheet(title=title, rows=rows, cols=cols)


def read_config(book) -> Config:
    ws = book.worksheet("CONFIG")
    values = ws.get("A2:B2")
    row = values[0] if values else []
    row = list(row) + [""] * (2 - len(row))
    cfg = Config(*(str(value or "").strip() for value in row[:2]))
    if not cfg.seller_client_id or not cfg.seller_api_key:
        raise SyncError("CONFIG A2/B2 missing Seller API credentials")
    expected = os.getenv("EXPECTED_SELLER_CLIENT_ID", "").strip()
    if expected and cfg.seller_client_id != expected:
        raise SyncError("seller client id mismatch; refusing to sync a different store")
    return cfg


def seller_headers(cfg: Config) -> dict[str, str]:
    return {
        "Client-Id": cfg.seller_client_id,
        "Api-Key": cfg.seller_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def request_with_retry(session: requests.Session, method: str, url: str, **kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            response = session.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < HTTP_ATTEMPTS:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
            return response
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == HTTP_ATTEMPTS:
                break
            time.sleep(min(2 ** (attempt - 1), 8))
    raise SyncError(
        f"network request failed after {HTTP_ATTEMPTS} attempts: {type(last_exc).__name__}"
    )


def overwrite_worksheet(ws, matrix: list[list[Any]]) -> None:
    needed_rows = max(len(matrix) + 10, 100)
    needed_cols = max(max((len(row) for row in matrix), default=1), 1)
    if ws.row_count < needed_rows or ws.col_count < needed_cols:
        ws.resize(rows=max(ws.row_count, needed_rows), cols=max(ws.col_count, needed_cols))
    ws.clear()
    ws.update(values=matrix, range_name="A1", value_input_option="RAW")


def _safe_int(value: Any) -> int:
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return 0


def _money_amount(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("amount") or "")
    return str(value or "")


def _money_currency(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("currency") or value.get("currency_code") or "")
    return ""


def _stock_entries(item: dict[str, Any]) -> list[dict[str, Any]]:
    stocks = item.get("stocks") or []
    if isinstance(stocks, dict):
        return [value for value in stocks.values() if isinstance(value, dict)]
    if isinstance(stocks, list):
        return [value for value in stocks if isinstance(value, dict)]
    return []


def _stock_bucket(stock_type: Any) -> str:
    value = str(stock_type or "").strip().lower()
    if "fbp" in value:
        return "fbp"
    if "fbs" in value or "rfbs" in value:
        return "fbs"
    if "fbo" in value:
        return "fbo"
    return "other"


def fetch_product_stocks(cfg: Config) -> list[dict[str, Any]]:
    session = requests.Session()
    cursor = ""
    seen_cursors: set[str] = set()
    result: list[dict[str, Any]] = []
    for _ in range(10000):
        body = {"cursor": cursor, "filter": {"visibility": "ALL"}, "limit": 1000}
        response = request_with_retry(
            session,
            "POST",
            f"{SELLER_BASE}{STOCKS_PATH}",
            headers=seller_headers(cfg),
            json=body,
        )
        if response.status_code != 200:
            raise SyncError(f"Product stocks API HTTP {response.status_code}")
        payload = response.json()
        rows = payload.get("items") or []
        if not isinstance(rows, list):
            raise SyncError("Product stocks API returned an invalid items payload")
        result.extend(row for row in rows if isinstance(row, dict))
        next_cursor = str(payload.get("cursor") or "")
        if not rows or not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise SyncError("Product stocks pagination exceeded safety limit")
    return result


def stock_matrix(items: list[dict[str, Any]]) -> list[list[Any]]:
    matrix: list[list[Any]] = [STOCK_HEADER]
    for item in items:
        raw = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        for stock in _stock_entries(item):
            matrix.append([
                item.get("offer_id", ""),
                item.get("product_id", ""),
                str(stock.get("type") or "").lower(),
                stock.get("sku", ""),
                _safe_int(stock.get("present")),
                _safe_int(stock.get("reserved")),
                stock.get("shipment_type", ""),
                raw,
            ])
    return matrix


def inventory_matrix(items: list[dict[str, Any]]) -> list[list[Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        offer_id = str(item.get("offer_id") or "")
        product_id = str(item.get("product_id") or "")
        key = (offer_id, product_id)
        bucket = grouped.setdefault(
            key,
            {
                "offer_id": offer_id,
                "product_id": product_id,
                "present": defaultdict(int),
                "reserved": defaultdict(int),
                "skus": set(),
                "types": set(),
            },
        )
        for stock in _stock_entries(item):
            kind = _stock_bucket(stock.get("type"))
            bucket["present"][kind] += _safe_int(stock.get("present"))
            bucket["reserved"][kind] += _safe_int(stock.get("reserved"))
            sku = str(stock.get("sku") or "")
            if sku:
                bucket["skus"].add(sku)
            stock_type = str(stock.get("type") or "").strip().lower()
            if stock_type:
                bucket["types"].add(stock_type)
    matrix: list[list[Any]] = [INVENTORY_HEADER]
    for key in sorted(grouped):
        b = grouped[key]
        p, r = b["present"], b["reserved"]
        matrix.append([
            b["offer_id"],
            b["product_id"],
            p["fbp"],
            r["fbp"],
            p["fbs"],
            r["fbs"],
            p["fbo"],
            r["fbo"],
            p["other"],
            r["other"],
            sum(p.values()),
            sum(r.values()),
            ",".join(sorted(b["skus"])),
            ",".join(sorted(b["types"])),
        ])
    return matrix


def fetch_fbp_postings(cfg: Config, start: date, end: date) -> list[dict[str, Any]]:
    session = requests.Session()
    cursor = ""
    seen_cursors: set[str] = set()
    result: list[dict[str, Any]] = []
    for _ in range(10000):
        body = {
            "cursor": cursor,
            "filter": {
                "since": f"{start.isoformat()}T00:00:00Z",
                "to": f"{end.isoformat()}T23:59:59Z",
            },
            "limit": 1000,
        }
        response = request_with_retry(
            session,
            "POST",
            f"{SELLER_BASE}{FBP_POSTINGS_PATH}",
            headers=seller_headers(cfg),
            json=body,
        )
        if response.status_code != 200:
            raise SyncError(f"FBP postings API HTTP {response.status_code}")
        payload = response.json()
        rows = payload.get("postings") or []
        if not isinstance(rows, list):
            raise SyncError("FBP postings API returned an invalid postings payload")
        result.extend(row for row in rows if isinstance(row, dict))
        next_cursor = str(payload.get("cursor") or "")
        if not rows or not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise SyncError("FBP postings pagination exceeded safety limit")
    return result


def fbp_postings_matrix(postings: list[dict[str, Any]]) -> list[list[Any]]:
    matrix: list[list[Any]] = [FBP_POSTING_HEADER]
    for posting in sorted(
        postings,
        key=lambda row: (str(row.get("order_date") or ""), str(row.get("posting_number") or "")),
    ):
        raw = json.dumps(posting, ensure_ascii=False, separators=(",", ":"))
        products = posting.get("products") or []
        if not isinstance(products, list) or not products:
            products = [{}]
        for product in products:
            product = product if isinstance(product, dict) else {}
            seller_price = product.get("seller_price") or product.get("price") or {}
            customer_price = product.get("customer_price") or {}
            matrix.append([
                posting.get("posting_number", ""),
                posting.get("order_date", ""),
                posting.get("status", ""),
                posting.get("order_id", ""),
                posting.get("order_number", ""),
                product.get("offer_id", ""),
                product.get("sku", ""),
                product.get("name", ""),
                _safe_int(product.get("quantity")),
                _money_amount(seller_price),
                _money_currency(seller_price),
                _money_amount(customer_price),
                posting.get("provider_id", ""),
                raw,
            ])
    return matrix


def fbp_products_matrix(
    items: list[dict[str, Any]], postings: list[dict[str, Any]]
) -> list[list[Any]]:
    products: dict[str, dict[str, Any]] = {}

    def ensure(key: str, offer_id: str = "", sku: str = "") -> dict[str, Any]:
        return products.setdefault(
            key,
            {
                "offer_id": offer_id,
                "sku": sku,
                "name": "",
                "present": 0,
                "reserved": 0,
                "order_qty": 0,
                "postings": set(),
                "last_order_date": "",
                "last_status": "",
                "sources": set(),
            },
        )

    for item in items:
        offer_id = str(item.get("offer_id") or "")
        for stock in _stock_entries(item):
            if _stock_bucket(stock.get("type")) != "fbp":
                continue
            sku = str(stock.get("sku") or "")
            key = offer_id or sku
            if not key:
                continue
            row = ensure(key, offer_id, sku)
            if not row["sku"] and sku:
                row["sku"] = sku
            row["present"] += _safe_int(stock.get("present"))
            row["reserved"] += _safe_int(stock.get("reserved"))
            row["sources"].add("stock")

    for posting in postings:
        posting_number = str(posting.get("posting_number") or "")
        order_date = str(posting.get("order_date") or "")
        status = str(posting.get("status") or "")
        for product in posting.get("products") or []:
            if not isinstance(product, dict):
                continue
            offer_id = str(product.get("offer_id") or "")
            sku = str(product.get("sku") or "")
            key = offer_id or sku
            if not key:
                continue
            row = ensure(key, offer_id, sku)
            if not row["offer_id"] and offer_id:
                row["offer_id"] = offer_id
            if not row["sku"] and sku:
                row["sku"] = sku
            if not row["name"] and product.get("name"):
                row["name"] = str(product.get("name"))
            row["order_qty"] += _safe_int(product.get("quantity"))
            if posting_number:
                row["postings"].add(posting_number)
            if order_date >= row["last_order_date"]:
                row["last_order_date"] = order_date
                row["last_status"] = status
            row["sources"].add("posting")

    matrix: list[list[Any]] = [FBP_PRODUCT_HEADER]
    for key in sorted(products):
        row = products[key]
        matrix.append([
            row["offer_id"],
            row["sku"],
            row["name"],
            row["present"],
            row["reserved"],
            row["order_qty"],
            len(row["postings"]),
            row["last_order_date"],
            row["last_status"],
            "+".join(sorted(row["sources"])),
        ])
    return matrix


def write_status(book, values: list[Any]) -> None:
    ws = get_or_create_worksheet(book, "FBP_SYNC_STATUS", rows=200, cols=len(STATUS_HEADER))
    existing = ws.get_all_values()
    if not existing or existing[0] != STATUS_HEADER:
        ws.clear()
        ws.update(values=[STATUS_HEADER], range_name="A1", value_input_option="RAW")
    ws.append_row(values, value_input_option="RAW")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Ozon stock and FBP data into the inventory spreadsheet."
    )
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--fbp-days", type=int, default=186)
    args = parser.parse_args()

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=max(args.fbp_days - 1, 0))
    run_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    book = google_book(args.spreadsheet_id)
    cfg = read_config(book)
    stocks_status = "not_run"
    fbp_status = "not_run"
    messages: list[str] = []
    stock_items: list[dict[str, Any]] = []
    fbp_postings: list[dict[str, Any]] = []

    try:
        stock_items = fetch_product_stocks(cfg)
        if not stock_items:
            raise SyncError("Product stocks API returned zero items; refusing to erase stock tables")
        overwrite_worksheet(
            get_or_create_worksheet(
                book,
                "OZON_STOCKS",
                rows=max(len(stock_items) * 3 + 20, 1000),
                cols=len(STOCK_HEADER),
            ),
            stock_matrix(stock_items),
        )
        inventory = inventory_matrix(stock_items)
        overwrite_worksheet(
            get_or_create_worksheet(
                book,
                "OZON_INVENTORY",
                rows=max(len(inventory) + 20, 1000),
                cols=len(INVENTORY_HEADER),
            ),
            inventory,
        )
        stocks_status = "ok"
    except Exception as exc:
        stocks_status = "error"
        messages.append(f"stocks:{type(exc).__name__}:{exc}")

    try:
        fbp_postings = fetch_fbp_postings(cfg, start, today)
        overwrite_worksheet(
            get_or_create_worksheet(
                book,
                "FBP_POSTINGS",
                rows=max(len(fbp_postings) * 3 + 20, 1000),
                cols=len(FBP_POSTING_HEADER),
            ),
            fbp_postings_matrix(fbp_postings),
        )
        fbp_status = "ok"
    except Exception as exc:
        fbp_status = "error"
        messages.append(f"fbp:{type(exc).__name__}:{exc}")

    if stock_items or fbp_postings:
        fbp_products = fbp_products_matrix(stock_items, fbp_postings)
        if len(fbp_products) > 1:
            overwrite_worksheet(
                get_or_create_worksheet(
                    book,
                    "FBP_PRODUCTS",
                    rows=max(len(fbp_products) + 20, 1000),
                    cols=len(FBP_PRODUCT_HEADER),
                ),
                fbp_products,
            )
    else:
        fbp_products = [FBP_PRODUCT_HEADER]

    write_status(
        book,
        [
            run_at,
            cfg.seller_client_id,
            stocks_status,
            len(stock_items),
            fbp_status,
            len(fbp_postings),
            max(len(fbp_products) - 1, 0),
            start.isoformat(),
            today.isoformat(),
            " | ".join(messages)[:1000],
        ],
    )
    print(
        json.dumps(
            {
                "seller_client_id": cfg.seller_client_id,
                "stocks_status": stocks_status,
                "stock_items": len(stock_items),
                "fbp_status": fbp_status,
                "fbp_postings": len(fbp_postings),
                "fbp_products": max(len(fbp_products) - 1, 0),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    if stocks_status != "ok":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
