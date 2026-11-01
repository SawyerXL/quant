"""预测前消息面预检的纯函数测试（不联网）。"""
import pandas as pd

import scripts.news_precheck as npc


def test_scan_catches_policy_meeting(tmp_path, monkeypatch):
    feed = tmp_path / "feed.csv"
    pd.DataFrame({
        "source": ["sina_global"],
        "pub_time": ["2026-11-01 15:12:03"],
        "title": ["【央行、证监会将于明天召开座谈会】一些证券公司和基金公司"
                  "接到通知，研究推动支持资本市场工具使用。"],
    }).to_csv(feed, index=False)
    monkeypatch.setattr(npc, "FEED", feed)
    out = npc.scan(["2026-11-01"])
    assert out["n_material"] == 1
    assert out["n_policy_hit"] == 1
    assert out["no_material_event"] is False
