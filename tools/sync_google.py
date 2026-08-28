from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from build_encrypted_app import load_key, sync


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


def snapshot_rows(spreadsheet_id: str) -> list[dict]:
    worksheet = gspread.authorize(credentials()).open_by_key(spreadsheet_id).worksheet("SKU决策快照")
    rows = worksheet.get_all_records(default_blank=None, numericise_ignore=["all"])
    result = [dict(row) for row in rows if str(row.get("SKU_KEY") or "").strip()]
    keys = [str(row["SKU_KEY"]).strip() for row in result]
    if len(keys) != len(set(keys)):
        raise SystemExit("duplicate SKU_KEY in snapshot")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Read the authoritative Google snapshot and update the encrypted PWA.")
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--current-envelope", type=Path, default=Path("app.enc.json"))
    parser.add_argument("--output", type=Path, default=Path("app.enc.json"))
    parser.add_argument("--manifest", type=Path, default=Path("pwa-data-version.json"))
    parser.add_argument("--status", type=Path, default=Path("sync-status.json"))
    args = parser.parse_args()
    key_value = os.getenv("PWA_APP_KEY")
    if not key_value:
        raise SystemExit("missing PWA_APP_KEY; refusing unencrypted output")
    rows = snapshot_rows(args.spreadsheet_id)
    temp = Path("/tmp/sku-snapshot.private.json")
    temp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    result = sync(
        args.current_envelope,
        temp,
        args.output,
        args.manifest,
        load_key(key_value),
        partial=False,
        expected_records=len(rows),
    )
    args.status.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
