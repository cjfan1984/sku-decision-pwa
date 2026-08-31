from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Iterable


PLACEHOLDER_TOKENS = ("待补", "未闭环", "待核", "待研究", "待锁", "待确认", "缺")
EXACT_EVIDENCE_TOKENS = ("精确规格", "精确sku", "精确 sku", "商品卡", "exact")
NEGATIVE_EVIDENCE_TOKENS = (
    "禁止", "不能", "不可", "不继承", "不得继承", "禁止继承", "不得用于",
    "错类", "撤销", "不是", "误配", "误判", "不作为", "仅用于排除", "仅排除",
)

BLIND_RIVET_TOKENS = ("抽芯铆钉", "抽芯铆", "blind rivet", "вытяжн")
RIVET_NUT_TOKENS = ("拉铆螺母", "铆螺母", "rivet nut", "резьбов")
VACUUM_ROLL_TOKENS = ("真空卷", "卷袋", "vacuum roll", "vacuum sealer roll")
VACUUM_BAG_TOKENS = ("真空袋", "预切袋", "pre-cut bag", "vacuum bag")
ADAPTER_TOKENS = ("转换头", "转换接头", "adapter", "adaptor")
DRILL_TOKENS = ("钻头", "drill bit", "сверл")

THREAD_RE = re.compile(r"(?<![a-z0-9])m\s*(3|4|5|6|8|10|12)(?!\d)", re.I)
DIM_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)(?:\s*(?:mm|毫米))?", re.I)
PCS_RE = re.compile(r"(?<!\d)(\d{1,5})\s*(?:pcs?|pieces?|件|枚|颗|条|支|只|片|卷)(?!\w)", re.I)
# Only explicit kg-pack wording is a package identity. Do not treat product weight,
# tensile rating or shipping weight such as 0.15kg / 18kg as a kg package.
KG_PACK_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*kg\s*(?:装|pack|package)\b", re.I)
POWER_RE = re.compile(r"(?<!\d)(\d{2,5})\s*w(?!\w)", re.I)


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _has_any(text: str, tokens: Iterable[str]) -> bool:
    return any(token.lower() in text for token in tokens)


def _identity_text(row: dict) -> str:
    fields = ("产品族", "产品名称/SKU", "完整目标产品规格")
    return " | ".join(str(row.get(field) or "") for field in fields).lower()


def _evidence_text(row: dict) -> str:
    # These fields can carry source/spec notes. Current action is intentionally
    # excluded so a sentence such as "禁止继承拉铆螺母枪证据" is not itself evidence.
    fields = ("实际首选货源规格", "规格差异", "证据匹配层级")
    return " | ".join(str(row.get(field) or "") for field in fields).lower()


def _affirmative_token(text: str, tokens: Iterable[str]) -> bool:
    """True only when a token is not locally negated/rejected."""
    lowered = text.lower()
    for token in tokens:
        needle = token.lower()
        start = 0
        while True:
            pos = lowered.find(needle, start)
            if pos < 0:
                break
            window = lowered[max(0, pos - 28): pos + len(needle) + 36]
            if not any(neg.lower() in window for neg in NEGATIVE_EVIDENCE_TOKENS):
                return True
            start = pos + len(needle)
    return False


def classify_family(row: dict) -> str:
    text = _identity_text(row)
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
    text = _identity_text(row)
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


def _placeholder_only_spec(value: object) -> bool:
    reduced = _clean(value)
    if not reduced:
        return True
    for token in PLACEHOLDER_TOKENS:
        reduced = reduced.replace(token, "")
    reduced = re.sub(r"规格|参数|信息|属性|目标|产品|型号|待定|bom", "", reduced, flags=re.I)
    reduced = re.sub(r"[\s|｜/、,，;；:：()（）\[\]【】_.+\-]+", "", reduced)
    return not reduced


def validate_row(row: dict) -> list[str]:
    identity_text = _identity_text(row)
    evidence_text = _evidence_text(row)
    family = classify_family(row)
    identity = identity_tokens(row)
    problems: list[str] = []

    # Target identity itself must not mix mutually exclusive systems.
    if _has_any(identity_text, BLIND_RIVET_TOKENS) and _has_any(identity_text, RIVET_NUT_TOKENS):
        problems.append("target identity mixes blind rivet and rivet nut")
    if family == "blind_rivet" and identity["threads"]:
        problems.append("M-thread in blind-riveter target identity")

    # Evidence notes may mention rejected alternatives. Only affirmative source
    # evidence from another physical system is treated as contamination.
    if family == "blind_rivet" and _affirmative_token(evidence_text, RIVET_NUT_TOKENS):
        problems.append("rivet-nut evidence attached to blind-riveter target")
    if family == "rivet_nut" and _affirmative_token(evidence_text, BLIND_RIVET_TOKENS):
        problems.append("blind-rivet evidence attached to rivet-nut target")
    if family == "vacuum_roll" and _affirmative_token(evidence_text, VACUUM_BAG_TOKENS):
        problems.append("pre-cut-bag evidence attached to vacuum-roll target")
    if family == "vacuum_bag" and _affirmative_token(evidence_text, VACUUM_ROLL_TOKENS):
        problems.append("vacuum-roll evidence attached to pre-cut-bag target")

    # Packaging is part of SKU identity. Only explicit kg-pack wording counts;
    # product/net/gross weight does not.
    if identity["kg_packs"] and identity["pieces"] and _has_market_signal(row):
        problems.append("target identity mixes kg-pack and piece-pack")

    # Exact-SKU market evidence must contain a usable target identity.
    target_spec = _clean(row.get("完整目标产品规格"))
    if _exact_claim(row) and _has_market_signal(row):
        # A concrete identity can still have one unresolved attribute such as
        # material or certification. Only an absent/placeholder-only identity
        # is invalid; the old `待` + `核` substring rule rejected 27 real rows.
        if _placeholder_only_spec(target_spec):
            problems.append("exact evidence missing target specification")

    return problems


def validate_and_enrich_snapshot(rows: list[dict]) -> tuple[list[dict], dict]:
    keys = [_clean(row.get("SKU_KEY")) for row in rows]
    duplicate_keys = sorted(key for key, count in Counter(keys).items() if key and count > 1)
    if duplicate_keys:
        raise SystemExit(f"duplicate SKU_KEY values in snapshot: {len(duplicate_keys)}")

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

    if blocked:
        reason_counts = Counter(problem for item in blocked for problem in item["problems"])
        summary = ", ".join(f"{reason}={count}" for reason, count in reason_counts.most_common())
        raise SystemExit(f"physical SKU evidence gate blocked {len(blocked)} rows: {summary}")

    audit = {
        "physicalSkuGate": "PASS",
        "records": len(enriched),
        "blocked": 0,
        "families": dict(families.most_common()),
        "rulesVersion": "physical-sku-v2.1-2026-08-29",
    }
    return enriched, audit
