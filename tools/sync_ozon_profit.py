from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import gspread
import requests
from google.oauth2.service_account import Credentials


SELLER_BASE = "https://api-seller.ozon.ru"
PERF_BASE = "https://api-performance.ozon.ru"
FINANCE_PATH = "/v3/finance/transaction/list"
PERF_TOKEN_PATH = "/api/client/token"
DEFAULT_PERF_STATS_PATH = "/api/client/statistics/daily"
FIN_TX_HEADER = [
    "operation_id",
    "operation_date",
    "type",
    "posting_number",
    "order_id",
    "sku",
    "amount",
    "currency",
    "service",
    "comment_raw",
    "raw_json",
]
ADS_DAY_HEADER = ["date", "spend", "impressions", "clicks", "orders", "revenue", "raw_json"]
ADS_CAMPAIGN_DAY_HEADER = [
    "date",
    "campaign_id",
    "campaign_name",
    "spend",
    "impressions",
    "clicks",
    "orders",
    "revenue",
    "raw_json",
]
STATUS_HEADER = [
    "run_at_utc",
    "seller_client_id",
    "finance_from",
    "finance_to",
    "finance_rows",
    "finance_latest_operation_date",
    "ads_from",
    "ads_to",
    "ads_campaign_rows",
    "ads_auth_mode",
    "finance_status",
    "ads_status",
    "message",
]
HTTP_TIMEOUT = (10, 45)
HTTP_ATTEMPTS = 4


@dataclass(frozen=True)
class Config:
    seller_client_id: str
    seller_api_key: str
    perf_client_id: str
    perf_api_key: str


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
    client = gspread.authorize(google_credentials())
    return client.open_by_key(spreadsheet_id)


def get_or_create_worksheet(book, title: str, rows: int, cols: int):
    try:
        return book.worksheet(title)
    except gspread.WorksheetNotFound:
        return book.add_worksheet(title=title, rows=rows, cols=cols)


def read_config(book) -> Config:
    ws = book.worksheet("CONFIG")
    values = ws.get("A2:D2")
    row = values[0] if values else []
    row = list(row) + [""] * (4 - len(row))
    cfg = Config(*(str(value or "").strip() for value in row[:4]))
    if not cfg.seller_client_id or not cfg.seller_api_key:
        raise SyncError("CONFIG A2/B2 missing Seller API credentials")
    expected = os.getenv("EXPECTED_SELLER_CLIENT_ID", "").strip()
    if expected and cfg.seller_client_id != expected:
        raise SyncError("seller client id mismatch; refusing to sync a different store")
    return cfg


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
    raise SyncError(f"network request failed after {HTTP_ATTEMPTS} attempts: {type(last_exc).__name__}")


def month_chunks(start: date, end: date) -> Iterable[tuple[date, date]]:
    """Yield inclusive chunks that never cross a calendar month."""
    cursor = start
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        chunk_end = min(end, next_month - timedelta(days=1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def seller_headers(cfg: Config) -> dict[str, str]:
    return {
        "Client-Id": cfg.seller_client_id,
        "Api-Key": cfg.seller_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def fetch_finance_transactions(cfg: Config, start: date, end: date) -> list[dict[str, Any]]:
    session = requests.Session()
    headers = seller_headers(cfg)
    operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk_start, chunk_end in month_chunks(start, end):
        page = 1
        while True:
            body = {
                "filter": {
                    "date": {
                        "from": f"{chunk_start.isoformat()}T00:00:00.000Z",
                        "to": f"{chunk_end.isoformat()}T23:59:59.999Z",
                    },
                    "operation_type": [],
                    "posting_number": "",
                    "transaction_type": "all",
                },
                "page": page,
                "page_size": 1000,
            }
            response = request_with_retry(
                session,
                "POST",
                f"{SELLER_BASE}{FINANCE_PATH}",
                headers=headers,
                json=body,
            )
            if response.status_code != 200:
                raise SyncError(f"Seller Finance API HTTP {response.status_code}")
            payload = response.json()
            result = payload.get("result") or {}
            rows = result.get("operations") or []
            if not isinstance(rows, list):
                raise SyncError("Seller Finance API returned an invalid operations payload")
            for operation in rows:
                key = str(operation.get("operation_id") or "")
                if not key:
                    key = json.dumps(operation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if key not in seen:
                    seen.add(key)
                    operations.append(operation)
            page_count = int(result.get("page_count") or 0)
            if not rows or page_count == 0 or page >= page_count:
                break
            page += 1
    operations.sort(key=lambda row: (str(row.get("operation_date") or ""), str(row.get("operation_id") or "")))
    return operations


def _single_sku(operation: dict[str, Any]) -> str:
    skus = {
        str(item.get("sku"))
        for item in (operation.get("items") or [])
        if isinstance(item, dict) and item.get("sku") not in (None, "")
    }
    return next(iter(skus)) if len(skus) == 1 else ""


def finance_matrix(operations: list[dict[str, Any]]) -> list[list[Any]]:
    rows: list[list[Any]] = [FIN_TX_HEADER]
    for operation in operations:
        posting = operation.get("posting") or {}
        raw = json.dumps(operation, ensure_ascii=False, separators=(",", ":"))
        rows.append(
            [
                operation.get("operation_id", ""),
                operation.get("operation_date", ""),
                operation.get("type", ""),
                posting.get("posting_number", "") if isinstance(posting, dict) else "",
                posting.get("order_id", "") if isinstance(posting, dict) else "",
                _single_sku(operation),
                operation.get("amount", 0),
                operation.get("currency", "RUB") or "RUB",
                operation.get("operation_type", ""),
                operation.get("operation_type_name", ""),
                raw,
            ]
        )
    return rows


def overwrite_worksheet(ws, matrix: list[list[Any]]) -> None:
    needed_rows = max(len(matrix) + 10, 100)
    needed_cols = max(max((len(row) for row in matrix), default=1), 1)
    if ws.row_count < needed_rows or ws.col_count < needed_cols:
        ws.resize(rows=max(ws.row_count, needed_rows), cols=max(ws.col_count, needed_cols))
    ws.clear()
    ws.update(values=matrix, range_name="A1", value_input_option="RAW")


def performance_token(cfg: Config) -> str:
    response = request_with_retry(
        requests.Session(),
        "POST",
        f"{PERF_BASE}{PERF_TOKEN_PATH}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        json={
            "client_id": cfg.perf_client_id,
            "client_secret": cfg.perf_api_key,
            "grant_type": "client_credentials",
        },
    )
    if response.status_code != 200:
        raise SyncError(f"Performance token HTTP {response.status_code}")
    payload = response.json()
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise SyncError("Performance token response missing access_token")
    return token


def perf_headers(cfg: Config, mode: str) -> tuple[dict[str, str], str]:
    normalized = mode.strip().lower()
    if normalized in ("auto", "oauth", "bearer"):
        try:
            token = performance_token(cfg)
            return {"Authorization": f"Bearer {token}", "Accept": "text/csv,application/json"}, "oauth"
        except SyncError:
            if normalized != "auto":
                raise
    if normalized in ("auto", "api_key"):
        return {
            "Client-Id": cfg.perf_client_id,
            "Api-Key": cfg.perf_api_key,
            "Accept": "text/csv,application/json",
        }, "api_key"
    if normalized == "direct_bearer":
        return {"Authorization": f"Bearer {cfg.perf_api_key}", "Accept": "text/csv,application/json"}, "direct_bearer"
    raise SyncError(f"unsupported PERF_AUTH_MODE={mode}")


def _number(value: Any) -> float:
    text = str(value or "").strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return 0.0
    text = text.replace("₽", "").replace("%", "")
    if text.count(",") == 1 and text.count(".") == 0:
        text = text.replace(",", ".")
    elif text.count(",") > 0 and text.count(".") > 0:
        text = text.replace(",", "")
    text = re.sub(r"[^0-9+\-.]", "", text)
    if text in ("", "+", "-", "."):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\ufeff", "").strip().lower())


def _find_header(headers: list[str], includes: tuple[str, ...], excludes: tuple[str, ...] = ()) -> int | None:
    for index, header in enumerate(headers):
        normalized = _norm_header(header)
        if all(token in normalized for token in includes) and not any(token in normalized for token in excludes):
            return index
    return None


def parse_ads_daily_csv(text: str) -> list[dict[str, Any]]:
    clean = text.lstrip("\ufeff").strip()
    if not clean:
        return []
    reader = csv.reader(io.StringIO(clean), delimiter=";")
    rows = list(reader)
    if len(rows) < 2:
        return []
    headers = rows[0]
    normalized = [_norm_header(header) for header in headers]

    date_idx = _find_header(headers, ("дата",))
    campaign_idx = _find_header(headers, ("id", "кампан"))
    if campaign_idx is None:
        campaign_idx = _find_header(headers, ("кампан", "id"))
    name_idx = _find_header(headers, ("назван", "кампан"))
    spend_idx = _find_header(headers, ("расход",))
    impressions_idx = _find_header(headers, ("показ",))
    clicks_idx = _find_header(headers, ("клик",))
    orders_idx = _find_header(headers, ("заказ", "шт"))
    if orders_idx is None:
        orders_idx = _find_header(headers, ("заказ", "кол"))
    if orders_idx is None:
        orders_idx = _find_header(headers, ("заказ",), ("сум", "руб", "₽", "доля", "дрр"))
    revenue_idx = _find_header(headers, ("сум", "заказ"))
    if revenue_idx is None:
        revenue_idx = _find_header(headers, ("заказ", "₽"))
    if revenue_idx is None:
        revenue_idx = _find_header(headers, ("заказ", "руб"))

    # Current Ozon daily report documented order is campaign id, name, date,
    # impressions, clicks, spend, average bid, orders, revenue.
    def fallback(current: int | None, index: int) -> int | None:
        return current if current is not None else (index if index < len(headers) else None)

    campaign_idx = fallback(campaign_idx, 0)
    name_idx = fallback(name_idx, 1)
    date_idx = fallback(date_idx, 2)
    impressions_idx = fallback(impressions_idx, 3)
    clicks_idx = fallback(clicks_idx, 4)
    spend_idx = fallback(spend_idx, 5)
    orders_idx = fallback(orders_idx, 7)
    revenue_idx = fallback(revenue_idx, 8)

    def cell(row: list[str], index: int | None) -> str:
        return row[index].strip() if index is not None and index < len(row) else ""

    result: list[dict[str, Any]] = []
    for values in rows[1:]:
        campaign_id = cell(values, campaign_idx)
        row_date = cell(values, date_idx)
        # Ozon appends a total row; it has no campaign id and must not be counted.
        if not campaign_id or not row_date:
            continue
        result.append(
            {
                "date": row_date,
                "campaign_id": campaign_id,
                "campaign_name": cell(values, name_idx),
                "spend": _number(cell(values, spend_idx)),
                "impressions": int(round(_number(cell(values, impressions_idx)))),
                "clicks": int(round(_number(cell(values, clicks_idx)))),
                "orders": int(round(_number(cell(values, orders_idx)))),
                "revenue": _number(cell(values, revenue_idx)),
                "raw": {headers[i]: values[i] for i in range(min(len(headers), len(values)))},
            }
        )
    return result


def fetch_ads_daily(cfg: Config, start: date, end: date, auth_mode: str, stats_path: str) -> tuple[list[dict[str, Any]], str]:
    if not cfg.perf_client_id or not cfg.perf_api_key:
        raise SyncError("CONFIG C2/D2 missing Performance API credentials")
    headers, resolved_mode = perf_headers(cfg, auth_mode)
    session = requests.Session()
    response = request_with_retry(
        session,
        "GET",
        f"{PERF_BASE}{stats_path}",
        headers=headers,
        params={"dateFrom": start.isoformat(), "dateTo": end.isoformat()},
    )
    if response.status_code in (401, 403) and auth_mode.strip().lower() == "auto" and resolved_mode == "api_key":
        headers = {"Authorization": f"Bearer {cfg.perf_api_key}", "Accept": "text/csv,application/json"}
        response = request_with_retry(
            session,
            "GET",
            f"{PERF_BASE}{stats_path}",
            headers=headers,
            params={"dateFrom": start.isoformat(), "dateTo": end.isoformat()},
        )
        resolved_mode = "direct_bearer"
    if response.status_code != 200:
        raise SyncError(f"Performance stats HTTP {response.status_code} via {resolved_mode}")
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type:
        payload = response.json()
        # Some deployments return the report body under content/contentType.
        if isinstance(payload, list):
            rows = payload
            normalized = []
            for row in rows:
                if isinstance(row, dict):
                    normalized.append(
                        {
                            "date": str(row.get("date") or row.get("Date") or ""),
                            "campaign_id": str(row.get("campaignId") or row.get("campaign_id") or row.get("id") or ""),
                            "campaign_name": str(row.get("campaignName") or row.get("campaign_name") or row.get("name") or ""),
                            "spend": _number(row.get("spend") or row.get("expense")),
                            "impressions": int(round(_number(row.get("impressions") or row.get("shows")))),
                            "clicks": int(round(_number(row.get("clicks")))),
                            "orders": int(round(_number(row.get("orders")))),
                            "revenue": _number(row.get("revenue") or row.get("ordersMoney") or row.get("orders_sum")),
                            "raw": row,
                        }
                    )
            return [row for row in normalized if row["date"] and row["campaign_id"]], resolved_mode
        raise SyncError("Performance stats returned JSON metadata instead of a daily report")
    return parse_ads_daily_csv(response.text), resolved_mode


def ads_campaign_matrix(rows: list[dict[str, Any]]) -> list[list[Any]]:
    matrix: list[list[Any]] = [ADS_CAMPAIGN_DAY_HEADER]
    for row in sorted(rows, key=lambda item: (item["date"], str(item["campaign_id"]))):
        matrix.append(
            [
                row["date"],
                row["campaign_id"],
                row["campaign_name"],
                row["spend"],
                row["impressions"],
                row["clicks"],
                row["orders"],
                row["revenue"],
                json.dumps(row["raw"], ensure_ascii=False, separators=(",", ":")),
            ]
        )
    return matrix


def ads_day_matrix(rows: list[dict[str, Any]]) -> list[list[Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"spend": 0.0, "impressions": 0, "clicks": 0, "orders": 0, "revenue": 0.0, "raw": []}
    )
    for row in rows:
        bucket = grouped[row["date"]]
        bucket["spend"] += row["spend"]
        bucket["impressions"] += row["impressions"]
        bucket["clicks"] += row["clicks"]
        bucket["orders"] += row["orders"]
        bucket["revenue"] += row["revenue"]
        bucket["raw"].append({"campaign_id": row["campaign_id"], "campaign_name": row["campaign_name"], "spend": row["spend"]})
    matrix: list[list[Any]] = [ADS_DAY_HEADER]
    for row_date in sorted(grouped):
        bucket = grouped[row_date]
        matrix.append(
            [
                row_date,
                round(bucket["spend"], 6),
                bucket["impressions"],
                bucket["clicks"],
                bucket["orders"],
                round(bucket["revenue"], 6),
                json.dumps(bucket["raw"], ensure_ascii=False, separators=(",", ":")),
            ]
        )
    return matrix


def write_status(book, values: list[Any]) -> None:
    ws = get_or_create_worksheet(book, "OZON_SYNC_STATUS", rows=100, cols=len(STATUS_HEADER))
    existing = ws.get_all_values()
    if not existing or existing[0] != STATUS_HEADER:
        ws.clear()
        ws.update(values=[STATUS_HEADER], range_name="A1", value_input_option="RAW")
    ws.append_row(values, value_input_option="RAW")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync YAPEX Ozon finance and Performance API data into the inventory spreadsheet.")
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--finance-days", type=int, default=186)
    parser.add_argument("--ads-days", type=int, default=62)
    args = parser.parse_args()

    today = datetime.now(timezone.utc).date()
    finance_start = today - timedelta(days=max(args.finance_days - 1, 0))
    ads_start = today - timedelta(days=max(args.ads_days - 1, 0))
    run_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    book = google_book(args.spreadsheet_id)
    cfg = read_config(book)
    finance_status = "not_run"
    ads_status = "not_run"
    message_parts: list[str] = []
    finance_rows = 0
    finance_latest = ""
    ads_rows = 0
    ads_mode = ""

    try:
        operations = fetch_finance_transactions(cfg, finance_start, today)
        if not operations:
            raise SyncError("Finance API returned zero operations; refusing to erase FIN_TX")
        finance_rows = len(operations)
        finance_latest = max(str(row.get("operation_date") or "") for row in operations)
        fin_ws = get_or_create_worksheet(book, "FIN_TX", rows=max(finance_rows + 20, 1000), cols=len(FIN_TX_HEADER))
        overwrite_worksheet(fin_ws, finance_matrix(operations))
        finance_status = "ok"
    except Exception as exc:  # noqa: BLE001 - status must survive provider/API failures.
        finance_status = "error"
        message_parts.append(f"finance:{type(exc).__name__}:{exc}")

    try:
        stats_path = os.getenv("PERF_STATS_PATH", DEFAULT_PERF_STATS_PATH).strip() or DEFAULT_PERF_STATS_PATH
        auth_mode = os.getenv("PERF_AUTH_MODE", "auto")
        ads_rows_raw, ads_mode = fetch_ads_daily(cfg, ads_start, today, auth_mode, stats_path)
        if not ads_rows_raw:
            raise SyncError("Performance API returned zero campaign-day rows; refusing to erase ADS_DAY")
        ads_rows = len(ads_rows_raw)
        ads_ws = get_or_create_worksheet(book, "ADS_DAY", rows=max(ads_rows + 20, 1000), cols=len(ADS_DAY_HEADER))
        overwrite_worksheet(ads_ws, ads_day_matrix(ads_rows_raw))
        campaign_ws = get_or_create_worksheet(book, "ADS_CAMPAIGN_DAY", rows=max(ads_rows + 20, 1000), cols=len(ADS_CAMPAIGN_DAY_HEADER))
        overwrite_worksheet(campaign_ws, ads_campaign_matrix(ads_rows_raw))
        ads_status = "ok"
    except Exception as exc:  # noqa: BLE001 - finance remains useful when Performance API needs auth repair.
        ads_status = "error"
        message_parts.append(f"ads:{type(exc).__name__}:{exc}")

    write_status(
        book,
        [
            run_at,
            cfg.seller_client_id,
            finance_start.isoformat(),
            today.isoformat(),
            finance_rows,
            finance_latest,
            ads_start.isoformat(),
            today.isoformat(),
            ads_rows,
            ads_mode,
            finance_status,
            ads_status,
            " | ".join(message_parts)[:1000],
        ],
    )
    print(
        json.dumps(
            {
                "seller_client_id": cfg.seller_client_id,
                "finance_status": finance_status,
                "finance_rows": finance_rows,
                "finance_latest_operation_date": finance_latest,
                "ads_status": ads_status,
                "ads_campaign_rows": ads_rows,
                "ads_auth_mode": ads_mode,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    if finance_status != "ok":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
