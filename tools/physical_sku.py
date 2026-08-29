from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Iterable


PLACEHOLDER_TOKENS = ("待补", "未闭环", "待核", "待研究", "待锁", "待确认", "缺")
EXACT_EVIDENCE_TOKENS = ("精确规格", "精确sku", "精确 sku", "商品卡", "exact")

BLIND_RIVET_TOKENS = ("抽芯铆钉", "抽芯铆", "blind rivet", "вытяжн")
RIVET_NUT_TOKENS = ("拉铆螺母", "铆螺母", "rivet nut", "резьбов")
VACUUM_ROLL_TOKENS = ("真空卷", "卷袋", "vacuum roll", "vacuum sealer roll")
VACUUM_BAG_TOKENS = ("真空袋", "预切袋", "pre-cut bag", "vacuum bag")
ADAPTER_TOKENS = ("转换头", "转换接头", "adapter", "adaptor")
DRILL_TOKENS = ("钻头", "drill bit", "сверл")

THREAD_RE = re.compile(r"(?<![a-z0-9])m\s*(3|4|5|6|8|10|12)(?!\d)", re.I)
DIM_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)(?:\s*(?:mm|毫米))?", re.I)
PCS_RE = re.compile(r"(?<!\d)(\d{1,5})\s*(?:pcs?|pieces?|件|枚|颗|条|支|只|片|卷)(?!\w)", re.I)
KG_PACK_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*kg\s*(?:装|pack|package)?", re.I)
POWER_RE = re.compile(r"(?<!\d)(\d{2,5})\s*w(?!\w)", re.I)


def _text(row: dict) -> str:
    fields = (
        "产品族",
        "产品名称/SKU",
        "完整目标产品规格",
        "实际首选货源规格",
        "规格差异",
        "证据匹配层级",
        "当前动作",
    )
    return " | ".join(str(row.get(field) or "") for field in fields).lower()


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _has_any(text: str, tokens: Iterable[str]) -> bool:
    return any(token.lower() in text for token in tokens)


def classify_family(row: dict) -> str:
    text = _text(row)
    if _has_any(text, BLIND_RIVET_TOKENS):
        return "blind_rivet"
    if _has_any(text, RIVET_NUT_TOKENS):
        return "rivet_nut"
    if _has_any(text, VACUUM_ROLL_TOKENS):
        return "vacuum_roll"
    if _has_any(text, VACUUM_BAG_TOKENS):
        return "vacuum_bag"
    if _has_any(text, ADAPTER_TOKENS):
        return "tool_adapter"
    if _has_any(text, DRILL_TOKENS):
        return "drill_bit"
    family = _clean(row.get("产品族"))
    return family or "unclassified"


def identity_tokens(row: dict) -> dict:
    text = _text(row)
    dims = sorted({"×".join(m) for m in DIM_RE.findall(text)})
    pieces = sorted({int(v) for v in PCS_RE.findall(text)})
    kg_packs = sorted({float(v) for v in KG_PACK_RE.findall(text)})
    threads = sorted({f"M{v}" for v in THREAD_RE.findall(text)}, key=lambda x: int(x[1:]))
    powers = sorted({int(v) for v in POWER_RE.findall(text)})
    return {
        "dims": dims,
        "pieces": pieces,
        "kg_packs": kg_packs,
        "threads": threads,
        "powers": powers,
    }


def physical_signature(row: dict) -> str:
    identity = identity_tokens(row)
    name = _clean(row.get("产品名称/SKU"))
    spec = _clean(row.get("完整目标产品规格"))
    raw = "|".join(
        [
            classify_family(row),
            name,
            spec,
            ",".join(identity["dims"]),
            ",".join(map(str, identity["pieces"])),
            ",".join(map(str, identity["kg_packs"])),
            ",".join(identity["threads"]),
            ",".join(map(str, identity["powers"])),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _numeric(value: object) -> bool:
    if value in (None, ""):
        return False
    try:
        float(str(value).replace(",", ""))
        return True
    except ValueError:
        return False


def _has_market_signal(row: dict) -> bool:
    return any(
        _numeric(row.get(field))
        for field in (
            "Ozon竞品/市场销量",
            "WB竞品/市场销量",
            "Ozon目标/当前售价RUB",
            "WB目标/当前售价RUB",
        )
    )


def _exact_claim(row: dict) -> bool:
    evidence = _clean(row.get("证据匹配层级"))
    return _has_any(evidence, EXACT_EVIDENCE_TOKENS)


def validate_row(row: dict) -> list[str]:
    text = _text(row)
    family = classify_family(row)
    identity = identity_tokens(row)
    problems: list[str] = []

    # Mutually exclusive product systems: never share sales/price evidence.
    blind = _has_any(text, BLIND_RIVET_TOKENS)
    nut = _has_any(text, RIVET_NUT_TOKENS)
    if blind and nut:
        problems.append("blind-rivet/rivet-nut evidence mixed")
    if family == "blind_rivet" and identity["threads"]:
        problems.append("M-thread evidence attached to blind riveter")

    roll = _has_any(text, VACUUM_ROLL_TOKENS)
    bag = _has_any(text, VACUUM_BAG_TOKENS)
    if roll and bag:
        problems.append("vacuum-roll/pre-cut-bag evidence mixed")

    adapter = _has_any(text, ADAPTER_TOKENS)
    drill = _has_any(text, DRILL_TOKENS)
    if adapter and drill and family != "tool_adapter":
        problems.append("tool-adapter/drill-bit evidence mixed")

    # Packaging is part of SKU identity. A row must not claim two incompatible
    # package bases such as 1 kg and 100 pcs when it has SKU-level market data.
    if identity["kg_packs"] and identity["pieces"] and _has_market_signal(row):
        problems.append("kg-pack and piece-pack evidence mixed")

    # Exact-SKU market evidence must contain a usable target identity.
    target_spec = _clean(row.get("完整目标产品规格"))
    if _exact_claim(row) and _has_market_signal(row):
        if not target_spec or all(token in target_spec for token in ("待", "核")):
            problems.append("exact evidence missing target specification")

    return problems


def validate_and_enrich_snapshot(rows: list[dict]) -> tuple[list[dict], dict]:
    keys = [_clean(row.get("SKU_KEY")) for row in rows]
    duplicate_keys = sorted(key for key, count in Counter(keys).items() if key and count > 1)
    if duplicate_keys:
        raise SystemExit(f"duplicate SKU_KEY in snapshot: {duplicate_keys[:5]}")

    enriched: list[dict] = []
    blocked: list[dict] = []
    families = Counter()
    for row in rows:
        item = dict(row)
        family = classify_family(item)
        signature = physical_signature(item)
        problems = validate_row(item)
        families[family] += 1
        item["物理SKU族"] = family
        item["物理SKU签名"] = signature
        item["证据闸门"] = "BLOCK" if problems else "PASS"
        item["证据闸门原因"] = "；".join(problems) if problems else ""
        enriched.append(item)
        if problems:
            blocked.append({"SKU_KEY": item.get("SKU_KEY"), "problems": problems})

    # Production PWA is fail-closed: a detected cross-system contamination is
    # safer to stop than to publish a plausible but false explosive-product card.
    if blocked:
        sample = "; ".join(f"{x['SKU_KEY']}: {','.join(x['problems'])}" for x in blocked[:8])
        raise SystemExit(f"physical SKU evidence gate blocked {len(blocked)} rows: {sample}")

    audit = {
        "physicalSkuGate": "PASS",
        "records": len(enriched),
        "blocked": 0,
        "families": dict(families.most_common()),
        "rulesVersion": "physical-sku-v2-2026-08-29",
    }
    return enriched, audit
