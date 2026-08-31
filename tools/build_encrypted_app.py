from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PAYLOAD_RE = re.compile(r'(<script id="payload" type="application/json">)(.*?)(</script>)', re.S)
PENDING = ("待补", "待核", "待研究", "待询价", "未闭环", "待计算", "缺")
GAPS = [
    ("purchase_price", "核实同规格采购价/正式 PI", "实际/核价采购成本CNY", 20),
    ("cross_border_logistics", "补齐精确跨境物流成本", "跨境物流", 20),
    ("gross_weight", "核实单件包装毛重", "毛重kg", 18),
    ("package_dimensions", "核实包装长宽高", "包装尺寸cm", 16),
    ("supplier_evidence", "补齐三家同规格货源证据", "__suppliers__", 15),
    ("record_updated_at", "核实该 SKU 的记录更新时间", "快照更新时间", 12),
    ("platform_fee", "建立 Ozon/WB 分平台费率表", "平台费金额/费率", 8),
    ("domestic_packaging", "建立国内运费与包材分档规则", "国内运费/包材", 7),
    ("fixed_cost", "确认固定成本分摊规则", "固定成本", 5),
    ("ads_returns_reserve", "确认广告与退货预留规则", "广告/退货预留", 5),
]


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    raw = text(value).replace(",", "").replace("¥", "").replace("RUB", "").strip()
    percent = raw.endswith("%")
    raw = raw.rstrip("%").strip()
    try:
        result = float(raw)
    except ValueError:
        return None
    return result / 100 if percent else result


def present(value: Any) -> bool:
    raw = text(value)
    return bool(raw and not any(token in raw for token in PENDING))


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def load_key(value: str) -> bytes:
    key = b64url_decode(value.strip())
    if len(key) != 32:
        raise ValueError("PWA key must decode to exactly 32 bytes")
    return key


def decrypt_html_from_value(envelope: dict[str, Any], key: bytes) -> str:
    if envelope.get("alg") != "AES-256-GCM" or envelope.get("compression") != "gzip":
        raise ValueError("unsupported app envelope")
    compressed = AESGCM(key).decrypt(
        b64url_decode(envelope["iv"]),
        b64url_decode(envelope["ciphertext"]),
        None,
    )
    return gzip.decompress(compressed).decode("utf-8")


def decrypt_html(envelope_path: Path, key: bytes) -> str:
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    if not isinstance(envelope, dict):
        raise ValueError("encrypted app envelope must be a JSON object")
    return decrypt_html_from_value(envelope, key)


def decrypt_html_with_recovery(envelope_path: Path, recovery_path: Path | None, key: bytes) -> tuple[str, str]:
    try:
        return decrypt_html(envelope_path, key), "current"
    except (OSError, ValueError, KeyError, TypeError, InvalidTag):
        if recovery_path is None or not recovery_path.exists() or recovery_path == envelope_path:
            raise
        return decrypt_html(recovery_path, key), "recovery"


def encrypt_html(html: str, key: bytes) -> dict[str, Any]:
    iv = os.urandom(12)
    compressed = gzip.compress(html.encode("utf-8"), compresslevel=9, mtime=0)
    ciphertext = AESGCM(key).encrypt(iv, compressed, None)
    return {
        "v": 1,
        "alg": "AES-256-GCM",
        "compression": "gzip",
        "iv": b64url_encode(iv),
        "ciphertext": b64url_encode(ciphertext),
    }


def extract_payload(html: str) -> dict[str, Any]:
    match = PAYLOAD_RE.search(html)
    if not match:
        raise ValueError("dashboard payload marker not found")
    return json.loads(match.group(2))


def inject_payload(html: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html, count = PAYLOAD_RE.subn(lambda match: match.group(1) + encoded + match.group(3), html, count=1)
    if count != 1:
        raise ValueError("dashboard payload replacement failed")
    total = len(payload["records"])
    html = re.sub(r"把\s*\d+\s*个 SKU 压缩", f"把 {total} 个 SKU 压缩", html, count=1)
    return html


def supplier_count(record: dict[str, Any]) -> int:
    return sum(text(record.get(f"候选货源{i}")).startswith(("http://", "https://")) for i in range(1, 4))


def missing_gaps(record: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for code, label, field, weight in GAPS:
        if field == "__suppliers__":
            missing = supplier_count(record) < 3
        elif field in {"实际/核价采购成本CNY", "毛重kg"}:
            missing = number(record.get(field)) is None
        elif field == "包装尺寸cm":
            missing = len(re.findall(r"\d+(?:\.\d+)?", text(record.get(field)))) < 3
        elif field == "快照更新时间":
            missing = not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text(record.get(field)))
        else:
            missing = not present(record.get(field))
        if missing:
            result.append({"code": code, "label": label, "weight": weight})
    return result


def quality(record: dict[str, Any]) -> dict[str, Any]:
    gaps = missing_gaps(record)
    maximum = sum(item[3] for item in GAPS)
    missing_weight = sum(item["weight"] for item in gaps)
    score = max(0, round((maximum - missing_weight) / maximum * 100))
    stage = text(record.get("当前阶段"))
    readiness = "STOPPED" if stage == "STOP" else "BLOCKED" if gaps else "READY"
    primary = gaps[0] if gaps else None
    if primary:
        primary = {
            **primary,
            "severity": "CRITICAL" if primary["weight"] >= 18 else "HIGH",
            "owner": "PROCUREMENT" if primary["code"] in {"purchase_price", "supplier_evidence"} else "LOGISTICS",
            "is_only_sku_blocker": len(gaps) == 1,
        }
    return {
        "sku_completeness_percent": score,
        "decision_completeness_percent": score,
        "decision_readiness": readiness,
        "primary_blocker": primary,
        "missing_gap_codes": [item["code"] for item in gaps],
        "missing_gap_labels": [item["label"] for item in gaps],
    }


def derived_fields(record: dict[str, Any]) -> dict[str, Any]:
    stage = text(record.get("当前阶段")) or "DEMAND_RESEARCH"
    profit = number(record.get("单件净利润CNY"))
    margin = number(record.get("净利率"))
    if margin is not None and margin > 1.5:
        margin /= 100
    calculable = profit is not None and margin is not None
    pass_gate = calculable and (profit > 10 or margin > 0.15)
    stopped = stage == "STOP"
    q = quality(record)
    record.update(
        {
            "15%准入线": "通过" if pass_gate else "未通过" if calculable else "待计算",
            "20%自动发布线": "达到20%" if margin is not None and margin >= 0.20 else "未达到20%" if calculable else "待计算",
            "业务三态": "停止" if stopped else "保留" if pass_gate else "待核",
            "是否可算利润": "是" if calculable else "否",
            "利润闸门": "OR门槛通过" if pass_gate else "双门槛未过" if calculable else "待补成本",
            "自动化锁": "LOCK_STOP" if stopped else "OPEN_REVIEW",
            "状态机关键缺口": "、".join(q["missing_gap_labels"][:5]) or "无",
            "状态机阶段原因": text(record.get("唯一阻塞点")) or text(record.get("当前动作")),
            "有效净利润排名": record.get("有效净利润排名"),
            "有效净利润CNY": profit if pass_gate else None,
            "有效净利率": margin if pass_gate else None,
            "有效利润供应商/MOQ": record.get("有效利润供应商/MOQ"),
            "有效利润证据状态": record.get("有效利润证据状态"),
            "_python_v4": {
                "engine_version": "zero-gpt-1.0.0",
                "quality": q,
                "task_candidates": [],
                "sourcing": (record.get("_python_v4") or {}).get("sourcing"),
            },
        }
    )
    return record


def queue_for(record: dict[str, Any]) -> dict[str, Any]:
    q = record["_python_v4"]["quality"]
    stage = text(record.get("当前阶段"))
    priority = text(record.get("当前优先级")).upper()
    stopped = stage == "STOP"
    base = {"S": 90, "A+": 85, "A": 75, "B": 55, "C": 35}.get(priority, 30)
    level = "暂停" if stopped else "P1" if base >= 75 else "P2" if base >= 55 else "P3"
    row = int(number(record.get("主库行")) or number(record.get("_sheet_row")) or 0)
    return {
        "Queue_ID": f"Q-{row:04d}" if row else f"Q-{hashlib.sha256(text(record.get('SKU_KEY')).encode()).hexdigest()[:8]}",
        "SKU_KEY": record.get("SKU_KEY"),
        "主库行": row or None,
        "SKU": record.get("产品名称/SKU"),
        "当前阶段": stage,
        "优先级分": -999 if stopped else base,
        "队列等级": level,
        "缺口数": len(q["missing_gap_codes"]),
        "缺失字段": "、".join(q["missing_gap_labels"]),
        "下一步最有价值动作": text(record.get("当前动作")) or (q["missing_gap_labels"][0] if q["missing_gap_labels"] else "进入人工验收"),
        "预期信息价值": "无继续投入价值" if stopped else "高" if level == "P1" else "中",
        "停止条件": "已触发停止；除非出现高等级新证据否则不重启" if stopped else "净利润≤¥10且净利率≤15%；高认证/安全风险；连续2轮无新增有效证据",
        "尝试次数": "",
        "最近尝试": None,
        "队列状态": "暂停" if stopped else "排队",
        "Ozon28天件数": number(record.get("Ozon竞品/市场销量")),
        "WB30天下单": number(record.get("WB竞品/市场销量")),
        "备注": text(record.get("唯一阻塞点")) or text(record.get("研究状态")),
    }


def rebuild_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    stages = [text(record.get("当前阶段")) for record in records]
    profits = [number(record.get("有效净利润CNY")) for record in records]
    margins = [number(record.get("有效净利率")) for record in records]
    return {
        "total": len(records),
        "can_order": sum(stage in {"READY", "READY_TO_LIST", "PROFIT_GATE_OK"} for stage in stages),
        "inquiry": sum(stage != "STOP" for stage in stages),
        "stop": stages.count("STOP"),
        "model_profit_ok": stages.count("MODEL_PROFIT_OK"),
        "weight_check": stages.count("WEIGHT_CHECK"),
        "price_check": stages.count("PRICE_CHECK"),
        "demand_research": stages.count("DEMAND_RESEARCH"),
        "cost_check": stages.count("COST_CHECK"),
        "local_cost_check": stages.count("LOCAL_COST_CHECK"),
        "hard_stop": stages.count("STOP"),
        "effective_profit_rows": sum(value is not None for value in profits),
        "p1": sum(text(record.get("当前优先级")).upper() in {"S", "A+", "A"} and text(record.get("当前阶段")) != "STOP" for record in records),
        "p2": sum(text(record.get("当前优先级")).upper() == "B" and text(record.get("当前阶段")) != "STOP" for record in records),
        "calc": sum(value is not None for value in profits),
        "second_pass": sum((profit is not None and profit > 10) or (margin is not None and margin > 0.15) for profit, margin in zip(profits, margins)),
        "second_fail": sum(profit is not None and margin is not None and profit <= 10 and margin <= 0.15 for profit, margin in zip(profits, margins)),
        "gte20": sum(margin is not None and margin >= 0.20 for margin in margins),
        "mid": sum(margin is not None and 0.15 < margin < 0.20 for margin in margins),
        "low": sum(margin is not None and 0 <= margin <= 0.15 for margin in margins),
        "negative": sum(profit is not None and profit < 0 for profit in profits),
        "pending": sum(profit is None for profit in profits),
    }


def rebuild_python_summary(payload: dict[str, Any]) -> dict[str, Any]:
    records = payload["records"]
    gap_counts = {code: 0 for code, *_ in GAPS}
    readiness: dict[str, int] = {}
    blocker_counts: dict[str, int] = {}
    scores = []
    for record in records:
        q = record["_python_v4"]["quality"]
        scores.append(q["sku_completeness_percent"])
        readiness[q["decision_readiness"]] = readiness.get(q["decision_readiness"], 0) + 1
        for code in q["missing_gap_codes"]:
            gap_counts[code] += 1
        primary = q.get("primary_blocker")
        if primary:
            code = primary["code"]
            blocker_counts[code] = blocker_counts.get(code, 0) + 1
    return {
        "total_skus": len(records),
        "cross_border_hard_stops": payload["stats"]["stop"],
        "explicit_local_research": sum(text(record.get("当前阶段")) == "LOCAL_COST_CHECK" for record in records),
        "route_conflicts": 0,
        "all_routes_stops": 0,
        "active_p1": payload["stats"]["p1"],
        "legacy_model_profit_ok": payload["stats"]["model_profit_ok"],
        "strict_gate_pass_on_imported_values": payload["stats"]["second_pass"],
        "verified_profit": 0,
        "unknown_update_dates": gap_counts["record_updated_at"],
        "proposed_task_candidates": sum(len(record["_python_v4"]["quality"]["missing_gap_codes"]) for record in records),
        "sku_task_candidates": sum(bool(record["_python_v4"]["quality"]["missing_gap_codes"]) for record in records),
        "global_task_candidates": 4,
        "gap_counts": gap_counts,
        "data_quality": {
            "average_sku_completeness_percent": round(sum(scores) / len(scores)) if scores else 0,
            "average_decision_completeness_percent": round(sum(scores) / len(scores)) if scores else 0,
            "decision_readiness_counts": readiness,
            "primary_blocker_counts": blocker_counts,
        },
        "sourcing": {
            "eligible_p1_skus": payload["stats"]["p1"],
            "target_candidates_per_sku": 3,
            "candidate_count": 0,
            "exact_candidate_count": 0,
            "formal_pi_count": 0,
            "profit_eligible_count": 0,
            "status_counts": {},
            "unattended_provider_enabled": False,
        },
    }


def merge_payload(payload: dict[str, Any], snapshot_rows: list[dict[str, Any]], *, partial: bool = False) -> dict[str, Any]:
    existing = {text(record.get("SKU_KEY")): record for record in payload.get("records", [])}
    updates = {text(row.get("SKU_KEY")): row for row in snapshot_rows if text(row.get("SKU_KEY"))}
    keys = list(existing) if partial else []
    for key in updates:
        if key not in keys:
            keys.append(key)
    records = []
    for key in keys:
        base = dict(existing.get(key, {}))
        base.update({field: value for field, value in updates.get(key, {}).items() if field})
        base["_sheet_row"] = int(number(base.get("主库行")) or len(records) + 2)
        records.append(derived_fields(base))
    records.sort(key=lambda record: (number(record.get("主库行")) or 10**9, text(record.get("SKU_KEY"))))
    if len(records) != len({text(record.get("SKU_KEY")) for record in records}):
        raise ValueError("duplicate SKU_KEY after merge")
    payload["records"] = records
    payload["queues"] = {record["SKU_KEY"]: queue_for(record) for record in records}
    payload["stats"] = rebuild_stats(records)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    payload["generated_at"] = f"{now.date().isoformat()}｜{len(records)} SKU 程序化快照"
    stable = json.dumps({"records": records, "queues": payload["queues"], "stats": payload["stats"]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(stable.encode()).hexdigest()
    py = payload.get("_python_v4") or {}
    py.update(
        {
            "engine_version": "zero-gpt-1.0.0",
            "release_id": f"zero-gpt-{digest[:16]}",
            "source_payload_sha256": digest,
            "summary": rebuild_python_summary(payload),
        }
    )
    for task in py.get("global_tasks") or []:
        task["affected_skus"] = sum(text(record.get("当前阶段")) != "STOP" for record in records)
        task["observed_missing_skus"] = len(records)
    payload["_python_v4"] = py
    return payload


def sync(
    envelope_path: Path,
    snapshot_path: Path,
    output_path: Path,
    manifest_path: Path,
    key: bytes,
    *,
    partial: bool = False,
    expected_records: int | None = None,
    recovery_envelope_path: Path | None = None,
) -> dict[str, Any]:
    html, template_source = decrypt_html_with_recovery(envelope_path, recovery_envelope_path, key)
    payload = extract_payload(html)
    rows = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("snapshot JSON must contain an array")
    payload = merge_payload(payload, rows, partial=partial)
    if expected_records is not None and len(payload["records"]) != expected_records:
        raise ValueError(f"record count mismatch: expected {expected_records}, got {len(payload['records'])}")
    html = inject_payload(html, payload)
    dataset_sha = payload["_python_v4"]["source_payload_sha256"]
    previous = {}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
    current_envelope_matches = False
    if output_path.exists() and previous.get("envelopeSha256"):
        current_envelope_matches = hashlib.sha256(output_path.read_bytes()).hexdigest() == previous["envelopeSha256"]
    if (
        template_source == "current"
        and current_envelope_matches
        and previous.get("datasetSha256") == dataset_sha
        and previous.get("records") == len(payload["records"])
    ):
        return {"state": "NO_CHANGE", "records": len(payload["records"]), "datasetSha256": dataset_sha}
    envelope = encrypt_html(html, key)
    output_path.write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")
    encrypted_sha = hashlib.sha256(output_path.read_bytes()).hexdigest()
    manifest = {
        "schema": "SKU-DECISION-PWA-V1",
        "records": len(payload["records"]),
        "version": dataset_sha[:16],
        "datasetSha256": dataset_sha,
        "envelopeSha256": encrypted_sha,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"state": "RECOVERED" if template_source == "recovery" else "CHANGED", **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the encrypted SKU dashboard without GPT or an LLM API.")
    parser.add_argument("--current-envelope", type=Path, default=Path("app.enc.json"))
    parser.add_argument("--snapshot-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("app.enc.json"))
    parser.add_argument("--manifest", type=Path, default=Path("pwa-data-version.json"))
    parser.add_argument("--recovery-envelope", type=Path, default=Path("recovery/app-baseline.enc.json"))
    parser.add_argument("--key-env", default="PWA_APP_KEY")
    parser.add_argument("--partial", action="store_true")
    parser.add_argument("--expected-records", type=int)
    args = parser.parse_args()
    key_value = os.getenv(args.key_env)
    if not key_value:
        raise SystemExit(f"missing secret environment variable: {args.key_env}")
    result = sync(
        args.current_envelope,
        args.snapshot_json,
        args.output,
        args.manifest,
        load_key(key_value),
        partial=args.partial,
        expected_records=args.expected_records,
        recovery_envelope_path=args.recovery_envelope,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
