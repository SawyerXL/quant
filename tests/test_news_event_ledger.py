"""消息面事件台账：分类/material 的高精度边界（2026-10-26 三层纪律）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import news_event_ledger as nel


def test_domestic_monetary_material():
    cat, hits = nel._classify("央行降准0.5个百分点，释放长期流动性")
    assert cat == "金融战略/货币"
    assert "降准" in hits
    assert nel._material("央行降准0.5个百分点，释放长期流动性", cat) is True


def test_company_research_not_material():
    title = "中信证券研报：AI行业下一个投入方向，维持跑赢行业评级"
    cat, _ = nel._classify(title)
    assert nel._material(title, cat) is False


def test_foreign_central_bank_goes_external():
    cat, _ = nel._classify("超级央行周来了：美联储、欧洲央行将公布利率决议")
    assert cat == "外部"
    assert nel._material("超级央行周来了：美联储、欧洲央行将公布利率决议", cat) is True


def test_weak_external_diplomacy_not_material():
    title = "伊朗外交部发言人：以色列选举结果对德黑兰并不重要"
    cat, _ = nel._classify(title)
    assert cat == "外部"
    assert nel._material(title, cat) is False


def test_geopolitical_oil_material():
    title = "美官员称中东美军接到命令后96小时内可发起针对伊朗的重大军事行动"
    cat, _ = nel._classify(title)
    assert cat == "外部"
    assert nel._material(title, cat) is True


def test_property_local_policy_material():
    title = "成都出台实施意见，推行现房销售定金制"
    cat, _ = nel._classify(title)
    assert cat == "产业政策"
    assert nel._material(title, cat) is True
