import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import build_encrypted_app as app
from validate_deploy import load_manifest, validate_deploy, validate_decryption


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def write_valid_pair(tmp_path: Path) -> tuple[Path, Path]:
    envelope = tmp_path / "app.enc.json"
    envelope.write_text(
        json.dumps(
            {
                "v": 1,
                "alg": "AES-256-GCM",
                "compression": "gzip",
                "iv": b64url(b"i" * 12),
                "ciphertext": b64url(b"c" * 2048),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "pwa-data-version.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "SKU-DECISION-PWA-V1",
                "records": 338,
                "version": "0123456789abcdef",
                "datasetSha256": "0" * 64,
                "envelopeSha256": hashlib.sha256(envelope.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return envelope, manifest


def test_valid_envelope_and_manifest_pass(tmp_path):
    envelope, manifest = write_valid_pair(tmp_path)
    assert validate_deploy(envelope, manifest)["records"] == 338


def test_placeholder_fails_closed(tmp_path):
    envelope, manifest = write_valid_pair(tmp_path)
    envelope.write_text("PLACEHOLDER", encoding="utf-8")
    with pytest.raises(ValueError, match="PLACEHOLDER"):
        validate_deploy(envelope, manifest)


def test_hash_mismatch_fails_closed(tmp_path):
    envelope, manifest = write_valid_pair(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["envelopeSha256"] = "f" * 64
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        validate_deploy(envelope, manifest)


def test_invalid_base64url_fails_even_when_manifest_hash_matches(tmp_path):
    envelope, manifest = write_valid_pair(tmp_path)
    data = json.loads(envelope.read_text(encoding="utf-8"))
    data["iv"] = "!" + data["iv"]
    envelope.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    version = json.loads(manifest.read_text(encoding="utf-8"))
    version["envelopeSha256"] = hashlib.sha256(envelope.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(version), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid base64url"):
        validate_deploy(envelope, manifest)


def test_missing_manifest_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        load_manifest(tmp_path / "missing.json")


def test_zero_record_manifest_fails_closed(tmp_path):
    _, manifest = write_valid_pair(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["records"] = 0
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="positive integer"):
        load_manifest(manifest)


def test_keyed_decryption_checks_payload_count_and_key(tmp_path):
    key = bytes(range(32))
    payload = {"records": [{"SKU_KEY": "0001|A"}], "stats": {"total": 1}}
    html = f'<script id="payload" type="application/json">{json.dumps(payload)}</script>'
    envelope = tmp_path / "app.enc.json"
    envelope.write_text(json.dumps(app.encrypt_html(html, key)), encoding="utf-8")
    manifest = tmp_path / "pwa-data-version.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "SKU-DECISION-PWA-V1",
                "records": 1,
                "version": "0123456789abcdef",
                "datasetSha256": "0" * 64,
                "envelopeSha256": hashlib.sha256(envelope.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    key_value = b64url(key)
    assert validate_decryption(envelope, manifest, key_value)["decryption"] == "VALID"
    with pytest.raises(ValueError, match="cannot decrypt"):
        validate_decryption(envelope, manifest, b64url(bytes(reversed(key))))
