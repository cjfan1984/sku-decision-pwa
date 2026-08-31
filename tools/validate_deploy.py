from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


HEX_64 = re.compile(r"[0-9a-f]{64}")


def _b64url(value: Any, *, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"encrypted envelope {field} is missing")
    try:
        padded = (value + "=" * (-len(value) % 4)).encode("ascii")
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except Exception as exc:
        raise ValueError(f"encrypted envelope {field} is invalid base64url") from exc


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("data manifest is missing") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("data manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("data manifest must be a JSON object")
    if manifest.get("schema") != "SKU-DECISION-PWA-V1":
        raise ValueError("data manifest schema is invalid")
    records = manifest.get("records")
    if not isinstance(records, int) or isinstance(records, bool) or records <= 0:
        raise ValueError("data manifest records must be a positive integer")
    for field in ("datasetSha256", "envelopeSha256"):
        if not isinstance(manifest.get(field), str) or not HEX_64.fullmatch(manifest[field]):
            raise ValueError(f"data manifest {field} must be a lowercase SHA-256")
    return manifest


def validate_deploy(envelope_path: Path, manifest_path: Path) -> dict[str, Any]:
    raw = envelope_path.read_bytes()
    if raw.strip() == b"PLACEHOLDER":
        raise ValueError("refusing to deploy PLACEHOLDER data")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("encrypted envelope is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("encrypted envelope must be a JSON object")
    if envelope.get("v") != 1:
        raise ValueError("encrypted envelope version must be 1")
    if envelope.get("alg") != "AES-256-GCM":
        raise ValueError("encrypted envelope algorithm must be AES-256-GCM")
    if envelope.get("compression") != "gzip":
        raise ValueError("encrypted envelope compression must be gzip")
    if len(_b64url(envelope.get("iv"), field="iv")) != 12:
        raise ValueError("encrypted envelope IV must be 12 bytes")
    if len(_b64url(envelope.get("ciphertext"), field="ciphertext")) < 1024:
        raise ValueError("encrypted envelope ciphertext is unexpectedly small")

    manifest = load_manifest(manifest_path)
    records = manifest["records"]
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != manifest["envelopeSha256"]:
        raise ValueError("encrypted envelope does not match the data manifest")
    return {
        "state": "VALID",
        "records": records,
        "version": manifest.get("version"),
        "envelopeSha256": actual_sha,
    }


def validate_decryption(envelope_path: Path, manifest_path: Path, key_value: str) -> dict[str, Any]:
    from build_encrypted_app import decrypt_html, extract_payload, load_key
    from cryptography.exceptions import InvalidTag

    manifest = load_manifest(manifest_path)
    try:
        payload = extract_payload(decrypt_html(envelope_path, load_key(key_value)))
    except (OSError, ValueError, KeyError, TypeError, InvalidTag) as exc:
        raise ValueError("configured key cannot decrypt the production payload") from exc
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("decrypted payload records must be an array")
    if len(records) != manifest["records"]:
        raise ValueError("decrypted payload record count does not match manifest")
    keys = [str(record.get("SKU_KEY") or "").strip() for record in records]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("decrypted payload SKU_KEY values must be present and unique")
    if (payload.get("stats") or {}).get("total") != manifest["records"]:
        raise ValueError("decrypted payload stats total does not match manifest")
    return {"decryption": "VALID", "records": len(records), "version": manifest.get("version")}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail closed unless a Pages SKU payload is internally consistent.")
    parser.add_argument("--envelope", type=Path, default=Path("app.enc.json"))
    parser.add_argument("--manifest", type=Path, default=Path("pwa-data-version.json"))
    parser.add_argument("--key-env", help="Optionally decrypt and verify records using this environment variable")
    args = parser.parse_args()
    result = validate_deploy(args.envelope, args.manifest)
    if args.key_env:
        key_value = os.getenv(args.key_env)
        if not key_value:
            raise SystemExit(f"missing secret environment variable: {args.key_env}")
        result.update(validate_decryption(args.envelope, args.manifest, key_value))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
