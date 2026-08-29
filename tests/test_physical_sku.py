import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import physical_sku as p


def row(name, spec, family="工具", evidence="精确规格＋产品族28天", ozon="100"):
    return {
        "SKU_KEY": f"k|{name}",
        "产品族": family,
        "产品名称/SKU": name,
        "完整目标产品规格": spec,
        "实际首选货源规格": "",
        "规格差异": "",
        "证据匹配层级": evidence,
        "Ozon竞品/市场销量": ozon,
        "WB竞品/市场销量": "",
        "Ozon目标/当前售价RUB": "500",
        "WB目标/当前售价RUB": "",
        "当前动作": "核价",
    }


def test_blind_rivet_and_rivet_nut_are_different_families():
    a = row("双柄抽芯铆钉枪", "2.4×3.2×4.0×4.8×6.4mm blind rivet")
    b = row("拉铆螺母枪", "M3 M4 M5 M6 M8 M10 rivet nut")
    assert p.classify_family(a) == "blind_rivet"
    assert p.classify_family(b) == "rivet_nut"
    assert p.physical_signature(a) != p.physical_signature(b)


def test_m_thread_evidence_blocks_blind_riveter():
    bad = row("抽芯铆钉枪", "2.4–6.4mm；错误附带M3 M4 M5 M6")
    assert "M-thread evidence attached to blind riveter" in p.validate_row(bad)


def test_piece_pack_and_kg_pack_cannot_share_exact_market_evidence():
    bad = row("木工自攻螺丝", "4×60mm 100颗；同证据又写1kg装", family="自攻螺丝")
    assert "kg-pack and piece-pack evidence mixed" in p.validate_row(bad)


def test_vacuum_roll_and_pre_cut_bag_are_separate():
    roll = row("真空卷袋", "28cm×5m 2卷", family="真空耗材")
    bag = row("预切真空袋", "20×30cm 100片", family="真空耗材")
    assert p.classify_family(roll) == "vacuum_roll"
    assert p.classify_family(bag) == "vacuum_bag"
    assert p.physical_signature(roll) != p.physical_signature(bag)


def test_clean_rows_are_enriched_for_pwa():
    rows = [
        row("抽芯铆钉枪", "双柄；2.4/3.2/4.0/4.8/6.4mm blind rivet"),
        row("拉铆螺母枪", "360mm双柄；M3 M4 M5 M6 M8 M10 rivet nut"),
    ]
    enriched, audit = p.validate_and_enrich_snapshot(rows)
    assert audit["physicalSkuGate"] == "PASS"
    assert audit["records"] == 2
    assert enriched[0]["证据闸门"] == "PASS"
    assert enriched[0]["物理SKU签名"]
    assert enriched[0]["物理SKU族"] == "blind_rivet"


def test_duplicate_sku_key_fails_closed():
    a = row("A", "100PCS")
    b = row("B", "200PCS")
    b["SKU_KEY"] = a["SKU_KEY"]
    with pytest.raises(SystemExit, match="duplicate SKU_KEY"):
        p.validate_and_enrich_snapshot([a, b])
