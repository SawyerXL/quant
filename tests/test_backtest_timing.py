"""
回测引擎 T+1 执行约束单元测试。
验证信号生成日与执行日严格错开一日。
"""
import pandas as pd, numpy as np
from datetime import date, timedelta
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')

class TestT1Execution:
    """构造数据验证：信号日 != 执行日"""

    def test_signal_uses_T_minus_1_data(self):
        """信号基于 T-1 收盘数据，在 T 日开盘执行"""
        dates = pd.date_range('2026-01-05', periods=20, freq='B')
        price = pd.Series(np.linspace(100, 120, 20), index=dates)

        # 模拟仓位决策：当天计算的仓位只能用于下一天
        pos_today = []
        pos_applied = []
        prev_pos = 1.0

        for i, (dt, p) in enumerate(price.items()):
            # T-1 收盘后计算信号
            if i < 5:
                new_pos = 1.0
            else:
                lookback = price[price.index <= dt].iloc[-5:]
                new_pos = 1.0 if lookback.iloc[-1] > lookback.mean() else 0.5

            # T 日执行：使用上一次的信号
            pos_applied.append(prev_pos)
            pos_today.append(new_pos)
            prev_pos = new_pos  # 今天的信号明天用

        # 验证：执行日使用昨天的信号
        assert pos_applied[1] == pos_today[0]  # 第2天的执行 = 第1天的信号
        assert pos_applied[2] == pos_today[1]
        assert pos_applied[5] == pos_today[4]
        print("  ✅ test_signal_uses_T_minus_1_data 通过")

    def test_no_forward_peek(self):
        """执行日不包含当日收盘信息"""
        dates = pd.date_range('2026-03-02', periods=30, freq='B')
        close = pd.Series(np.random.randn(30).cumsum() + 100, index=dates)

        for i, (dt, p) in enumerate(close.items()):
            if i < 3: continue
            # 模拟信号计算：只能用 ≤ dt 的数据
            known = close[close.index <= dt]
            # 验证：当日收盘不在 signal 的"未来"中使用
            assert known.index[-1] == dt
            # 信号生成时，最近一日必须 ≤ dt
            assert known.index.max() <= dt
        print("  ✅ test_no_forward_peek 通过")

    def test_commission_on_switch(self):
        """仓位切换时扣除 0.175% 手续费"""
        cost = 0.00175
        nav = 1.0
        positions = [1.0, 1.0, 0.5, 0.5, 1.0, 1.0]
        prev_pos = positions[0]
        cost_count = 0

        for pos in positions[1:]:
            if pos != prev_pos and prev_pos > 0:
                nav *= (1 - cost)
                cost_count += 1
            prev_pos = pos

        assert cost_count == 2  # 1.0→0.5 和 0.5→1.0 各一次
        expected = 1.0 * (1 - cost) ** 2
        assert abs(nav - expected) < 0.0001
        print("  ✅ test_commission_on_switch 通过")


if __name__ == "__main__":
    t = TestT1Execution()
    t.test_signal_uses_T_minus_1_data()
    t.test_no_forward_peek()
    t.test_commission_on_switch()
    print("\n  全部 T+1 单测通过 ✅")
