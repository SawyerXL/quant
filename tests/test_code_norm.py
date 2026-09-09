"""data.code_norm 单元测试 (2026-09-09 normalize 修复件)。

覆盖三类历史 bug 的根因断言:
  1. 命名空间歧义: 000001 既是个股(SZ)也是指数(SH), 裸键必须靠
     source market 消歧, 指数走 SH 前缀键
  2. 后缀规则唯一出处: 511360→SH(漏'5'前缀废单根因), 110/113→SH,
     92/43/83/87→BJ
  3. 价格量级自洽: 个股>1000 拒绝(指数点位混入), 指数<100 拒绝
     (个股价混入指数)
运行: .venv/bin/python -m pytest tests/test_code_norm.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from data.code_norm import (normalize_code, strip_suffix, assert_stock_code,
                            index_key, board_of, price_identity_ok,
                            is_ambiguous)


def test_normalize_basic_prefixes():
    assert normalize_code("511360") == "511360.SH"   # 沪短债ETF(9/8废单根因)
    assert normalize_code("600519") == "600519.SH"
    assert normalize_code("000063") == "000063.SZ"
    assert normalize_code("300750") == "300750.SZ"
    assert normalize_code("688012") == "688012.SH"
    assert normalize_code("110059") == "110059.SH"   # 沪转债
    assert normalize_code("113050") == "113050.SH"
    assert normalize_code("123456") == "123456.SZ"   # 深转债
    assert normalize_code("920100") == "920100.BJ"   # 北交所新号段
    assert normalize_code("430047") == "430047.BJ"   # 北交所旧号段(9/9补)
    assert normalize_code("830799") == "830799.BJ"


def test_normalize_with_market_hint():
    # 歧义码必须用 market 消歧
    assert normalize_code("000001", "SZ") == "000001.SZ"  # 平安银行
    assert normalize_code("000001", "SH") == "000001.SH"  # 上证指数
    with pytest.raises(ValueError):
        normalize_code("000001", "HK")


def test_already_suffixed_passthrough():
    assert normalize_code("000001.SZ") == "000001.SZ"
    assert normalize_code("sh000001") == "000001.SH"  # 源格式市场前缀→规范后缀
    assert normalize_code("SZ399006") == "399006.SZ"
    assert normalize_code("000001.SH") == "000001.SH"


def test_strip_and_index_key():
    assert strip_suffix("000001.SZ") == "000001"
    assert strip_suffix("SH000001") == "000001"
    assert index_key("000001", "SH") == "SH000001"
    assert index_key("399006", "SZ") == "SZ399006"


def test_assert_stock_code_rejects_index_namespace():
    with pytest.raises(ValueError):
        assert_stock_code("SH000001")   # 指数键进入个股路径
    with pytest.raises(ValueError):
        assert_stock_code("SZ399006")   # 399xxx 指数
    with pytest.raises(ValueError):
        assert_stock_code("399317")     # 裸 399xxx 也拒绝
    assert assert_stock_code("000001") == "000001"  # 歧义段裸键允许(已由源消歧)


def test_price_identity():
    # 个股: >1000 拒绝, 白名单除外
    assert not price_identity_ok("000063", 3942.5)
    assert not price_identity_ok("000001", 4028.9)   # 指数点位混入平安银行
    assert price_identity_ok("000063", 33.84)
    assert price_identity_ok("600519", 1291.5)       # 茅台白名单
    # 指数: 千点级允许, 个股价级拒绝
    assert price_identity_ok("SH000001", 4028.9, is_index=True)
    assert not price_identity_ok("SH000001", 11.59, is_index=True)


def test_board_of():
    assert board_of("600519") == "主板"
    assert board_of("300750") == "创业板"
    assert board_of("688012") == "科创板"
    assert board_of("920100") == "北交所"


def test_is_ambiguous():
    assert is_ambiguous("000001")
    assert is_ambiguous("399006")
    assert not is_ambiguous("600519")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
