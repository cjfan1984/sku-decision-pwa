import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import build_encrypted_app as app


def snapshot(key="0001|A", name="A", row=1):
    return {
        "SKU_KEY": key,
        "主库行": row,
        "产品名称/SKU": name,
        "当前优先级": "A",
        "当前阶段": "DEMAND_RESEARCH",
        "当前动作": "补正式采购价",
        "快照更新时间": "2026-08-28",
        "实际/核价采购成本CNY": "待补/未闭环",
        "跨境物流": "待补/未闭环",
        "毛重kg": "待补/未闭环",
        "包装尺寸cm": "待补/未闭环",
        "候选货源1": "待补/未闭环",
    }


def dashboard_html(payload):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f'<!doctype html><p>把 1 个 SKU 压缩成今天最值得做的 3 件事</p><script id="payload" type="application/json">{encoded}</script>'


def test_round_trip_and_partial_merge_updates_dynamic_count():
    key = bytes(range(32))
    payload = {"records": [snapshot()], "queues": {}, "stats": {"total": 1}, "generated_at": "old", "_python_v4": {}}
    encrypted = app.encrypt_html(dashboard_html(payload), key)
    assert app.extract_payload(app.decrypt_html_from_value(encrypted, key))["stats"]["total"] == 1


def test_sync_skips_second_random_encryption(tmp_path):
    key = bytes(range(32))
    payload = {"records": [snapshot()], "queues": {}, "stats": {"total": 1}, "generated_at": "old", "_python_v4": {}}
    current = tmp_path / "app.enc.json"
    current.write_text(json.dumps(app.encrypt_html(dashboard_html(payload), key)), encoding="utf-8")
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([snapshot(), snapshot("0002|B", "B", 2)], ensure_ascii=False), encoding="utf-8")
    manifest = tmp_path / "version.json"

    first = app.sync(current, rows, current, manifest, key, partial=True, expected_records=2)
    assert first["state"] == "CHANGED"
    first_ciphertext = current.read_text(encoding="utf-8")
    html = app.decrypt_html(current, key)
    assert "把 2 个 SKU 压缩" in html
    assert len(app.extract_payload(html)["records"]) == 2

    second = app.sync(current, rows, current, manifest, key, partial=True, expected_records=2)
    assert second["state"] == "NO_CHANGE"
    assert current.read_text(encoding="utf-8") == first_ciphertext
