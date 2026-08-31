from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import time
from pathlib import Path

import gspread
from cryptography.exceptions import InvalidTag
from google.auth.exceptions import TransportError as GoogleTransportError
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError
from gspread.http_client import HTTPClient
from requests.exceptions import ChunkedEncodingError as RequestsChunkedEncodingError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from build_encrypted_app import decrypt_html_with_recovery, extract_payload, load_key, sync
from physical_sku import validate_and_enrich_snapshot
from validate_deploy import load_manifest


GOOGLE_CONNECT_TIMEOUT_SECONDS = 10
GOOGLE_READ_TIMEOUT_SECONDS = 45
GOOGLE_READ_ATTEMPTS = 3


class BoundedGoogleHTTPClient(HTTPClient):
    """Give every Google request a finite connect/read deadline."""

    def __init__(self, auth, session=None):
        super().__init__(auth, session)
        self.timeout = (GOOGLE_CONNECT_TIMEOUT_SECONDS, GOOGLE_READ_TIMEOUT_SECONDS)


def credentials():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise SystemExit("missing GOOGLE_SERVICE_ACCOUNT_JSON; refusing GPT/browser fallback")
    return Credentials.from_service_account_info(
        json.loads(raw),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )


def _is_transient_google_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            GoogleTransportError,
            RequestsChunkedEncodingError,
            RequestsConnectionError,
            RequestsTimeout,
        ),
    ):
        return True
    if not isinstance(exc, APIError):
        return False
    code = getattr(exc, "code", None)
    if code in (408, 429) or (isinstance(code, int) and code >= 500):
        return True
    if code == 403:
        detail = getattr(exc, "error", None)
        errors = (detail.get("errors") or []) if isinstance(detail, dict) else []
        return any(isinstance(item, dict) and item.get("domain") == "usageLimits" for item in errors)
    return False


def _read_snapshot(spreadsheet_id: str) -> list[dict]:
    client = gspread.authorize(credentials(), http_client=BoundedGoogleHTTPClient)
    worksheet = client.open_by_key(spreadsheet_id).worksheet("SKU决策快照")
    return worksheet.get_all_records(default_blank=None, numericise_ignore=["all"])


def snapshot_rows(spreadsheet_id: str) -> list[dict]:
    for attempt in range(1, GOOGLE_READ_ATTEMPTS + 1):
        try:
            rows = _read_snapshot(spreadsheet_id)
            break
        except (
            APIError,
            GoogleTransportError,
            RequestsChunkedEncodingError,
            RequestsConnectionError,
            RequestsTimeout,
        ) as exc:
            if attempt == GOOGLE_READ_ATTEMPTS or not _is_transient_google_error(exc):
                raise
            delay = min(2 ** (attempt - 1), 4) + random.uniform(0, 0.5)
            print(f"Transient Google API failure; retrying {attempt + 1}/{GOOGLE_READ_ATTEMPTS} in {delay:.1f}s")
            time.sleep(delay)
    identity_fields = ("主库行", "一级系统", "产品族", "产品名称/SKU")
    missing_keys = [
        index
        for index, row in enumerate(rows, start=2)
        if not str(row.get("SKU_KEY") or "").strip()
        and any(str(row.get(field) or "").strip() for field in identity_fields)
    ]
    if missing_keys:
        raise SystemExit(f"business rows missing SKU_KEY: {missing_keys[:10]}")
    result = [dict(row) for row in rows if str(row.get("SKU_KEY") or "").strip()]
    keys = [str(row["SKU_KEY"]).strip() for row in result]
    if len(keys) != len(set(keys)):
        raise SystemExit("duplicate SKU_KEY in snapshot")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Google snapshot, enforce physical-SKU evidence gates, and update encrypted PWA.")
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--current-envelope", type=Path, default=Path("app.enc.json"))
    parser.add_argument("--output", type=Path, default=Path("app.enc.json"))
    parser.add_argument("--manifest", type=Path, default=Path("pwa-data-version.json"))
    parser.add_argument("--recovery-envelope", type=Path, default=Path("recovery/app-baseline.enc.json"))
    parser.add_argument("--status", type=Path, default=Path("sync-status.json"))
    args = parser.parse_args()
    key_value = os.getenv("PWA_APP_KEY")
    if not key_value:
        raise SystemExit("missing PWA_APP_KEY; refusing unencrypted output")

    key = load_key(key_value)
    try:
        manifest = load_manifest(args.manifest)
        previous_html, _ = decrypt_html_with_recovery(args.current_envelope, args.recovery_envelope, key)
        previous_payload = extract_payload(previous_html)
        previous_rows = previous_payload.get("records")
        if not isinstance(previous_rows, list):
            raise ValueError("existing decrypted payload records must be an array")
        previous_records = len(previous_rows)
        if manifest["records"] != previous_records:
            raise ValueError("existing decrypted payload record count does not match manifest")
        if (previous_payload.get("stats") or {}).get("total") != previous_records:
            raise ValueError("existing decrypted payload stats total does not match manifest")
    except (OSError, ValueError, KeyError, TypeError, InvalidTag) as exc:
        raise SystemExit(f"invalid existing production manifest; refusing sync: {exc}") from exc

    rows = snapshot_rows(args.spreadsheet_id)
    rows, gate_audit = validate_and_enrich_snapshot(rows)
    allow_record_drop = os.getenv("ALLOW_RECORD_DROP", "").lower() == "true"
    if len(rows) < previous_records and not allow_record_drop:
        raise SystemExit(
            f"source record count dropped from {previous_records} to {len(rows)}; "
            "set ALLOW_RECORD_DROP=true only after verifying the deletion"
        )

    temp = Path("/tmp/sku-snapshot.private.json")
    temp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    result = sync(
        args.current_envelope,
        temp,
        args.output,
        args.manifest,
        key,
        partial=False,
        expected_records=len(rows),
        recovery_envelope_path=args.recovery_envelope,
    )
    if (
        not args.recovery_envelope.exists()
        or hashlib.sha256(args.output.read_bytes()).digest() != hashlib.sha256(args.recovery_envelope.read_bytes()).digest()
    ):
        args.recovery_envelope.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.output, args.recovery_envelope)
    result["physicalSkuAudit"] = {
        field: gate_audit[field]
        for field in ("physicalSkuGate", "records", "blocked", "rulesVersion")
    }
    args.status.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
