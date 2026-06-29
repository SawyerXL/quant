"""
ML增强信号 — 不预测股价,只做三件验证有效的事:
  1. 因子权重学习器(XGBoost): 从历史学最优因子权重
  2. 持仓异动检测(Isolation Forest): 比MA10更早发现走势异常
  3. 板块轮动热力图: 行业动量排名+加速/减速
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd, numpy as np
from datetime import datetime, timedelta
from loguru import logger
from data.storage import load_daily, load_meta

# ══════════════════════════════════════════════════════════════════
# 1. 因子权重学习器
# ══════════════════════════════════════════════════════════════════
class FactorWeightLearner:
    """
    用XGBoost学习历史最优因子权重,替代拍脑袋的固定63/27/10。
    训练逻辑: 每月取CSI800截面 → 计算各因子Z-score →
             用XGBoost预测未来1月收益 → 输出特征重要性=最优权重
    """

    def __init__(self):
        self.model = None
        self.feature_importance = {}
        self.last_train_date = None

    def build_training_data(self, panel, amount_panel, stock_info, end_date, lookback_months=36):
        """构建训练集: 过去N个月的因子暴露+下月收益"""
        from scripts.run_backtest_a2 import compute_score_a2
        from factors.utils import zscore, industry_zscore

        all_dates = panel.index
        end_ts = pd.Timestamp(end_date)

        # 找每月截面日期
        monthly_dates = []
        for offset in range(lookback_months, 1, -1):
            target = end_ts - pd.DateOffset(months=offset)
            # 找最近的交易日
            candidates = [d for d in all_dates if d <= target]
            if candidates:
                monthly_dates.append(candidates[-1])

        rows = []
        ind_map = {}
        if stock_info is not None and not stock_info.empty:
            for _, r in stock_info.iterrows():
                ind_map[r['code']] = r.get('industry_l1', '其他')

        for dt in monthly_dates:
            dt_str = str(dt.date())
            # 计算下月收益(作为label)
            fut_end = dt + pd.DateOffset(months=1)
            fut_dates = [d for d in all_dates if d <= fut_end]
            if len(fut_dates) < 5:
                continue
            fut_dt = fut_dates[-1]

            # 因子暴露
            p1m = panel.loc[dt] / panel.loc[max(all_dates[0], dt-pd.Timedelta(days=25))] - 1
            p6m = panel.loc[dt] / panel.loc[max(all_dates[0], dt-pd.Timedelta(days=130))] - 1
            p12m = panel.loc[dt] / panel.loc[max(all_dates[0], dt-pd.Timedelta(days=250))] - 1

            # Sharpe
            start_i = max(0, all_dates.get_loc(dt) - 120)
            rets_6m = panel.iloc[start_i:all_dates.get_loc(dt)+1].pct_change(fill_method=None)
            sharpe = rets_6m.mean() / rets_6m.std().replace(0, np.nan)

            # vol
            vol_20d = panel.iloc[max(0,all_dates.get_loc(dt)-20):all_dates.get_loc(dt)+1].pct_change(fill_method=None).std()

            # turnover
            if amount_panel is not None:
                amt_avg = amount_panel.iloc[max(0,all_dates.get_loc(dt)-20):all_dates.get_loc(dt)+1].mean()
            else:
                amt_avg = pd.Series(1.0, index=panel.columns)

            # 未来收益
            fut_ret = panel.loc[fut_dt] / panel.loc[dt] - 1

            for code in panel.columns:
                if pd.isna(p1m.get(code)) or pd.isna(fut_ret.get(code)):
                    continue
                rows.append({
                    'code': code,
                    'date': dt_str,
                    'mom_1m': float(p1m.get(code, 0)),
                    'mom_6m': float(p6m.get(code, 0)),
                    'mom_12m': float(p12m.get(code, 0)),
                    'sharpe': float(sharpe.get(code, 0)) if not pd.isna(sharpe.get(code, 0)) else 0,
                    'vol_20d': float(vol_20d.get(code, 0)) if not pd.isna(vol_20d.get(code, 0)) else 0,
                    'amt_rank': float(amt_avg.rank(pct=True).get(code, 0.5)),
                    'industry': ind_map.get(code, '其他'),
                    'fut_ret': float(fut_ret.get(code, 0)),
                })

        if not rows:
            logger.warning("训练数据为空")
            return None

        return pd.DataFrame(rows)

    def train(self, panel, amount_panel, stock_info, end_date):
        """训练XGBoost, 返回特征重要性排序"""
        import xgboost as xgb
        df = self.build_training_data(panel, amount_panel, stock_info, end_date)
        if df is None or len(df) < 1000:
            return {}

        # One-hot编码行业
        feature_cols = ['mom_1m', 'mom_6m', 'mom_12m', 'sharpe', 'vol_20d', 'amt_rank']
        X = df[feature_cols].fillna(0)
        y = df['fut_ret'].clip(-0.5, 0.5)  # clip极端值

        # 去除极端离群值
        q_low = y.quantile(0.01)
        q_high = y.quantile(0.99)
        mask = (y >= q_low) & (y <= q_high)
        X = X[mask]
        y = y[mask]

        # 80/20 train/test
        split = int(len(X) * 0.8)
        X_train, X_test = X.iloc[:split], X.iloc[split:]
        y_train, y_test = y.iloc[:split], y.iloc[split:]

        model = xgb.XGBRegressor(
            n_estimators=100, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, random_state=42
        )
        model.fit(X_train, y_train)

        # 特征重要性
        importance = dict(zip(feature_cols, model.feature_importances_))
        # 归一化为权重
        total = sum(importance.values())
        weights = {k: v/total for k, v in importance.items()}
        # 将vol_20d映射为负向(高波动=低权重)
        sorted_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)

        self.model = model
        self.feature_importance = weights
        self.last_train_date = end_date

        # 测试集R²
        test_score = model.score(X_test, y_test)
        logger.info(f"因子权重学习完成: R²={test_score:.4f}, 特征数={len(feature_cols)}, 样本={len(X)}")
        logger.info(f"  权重: {', '.join(f'{k}={v:.0%}' for k,v in sorted_w)}")

        return weights

# ══════════════════════════════════════════════════════════════════
# 2. 持仓异动检测
# ══════════════════════════════════════════════════════════════════
class AnomalyDetector:
    """
    用Isolation Forest检测持仓走势是否出现异常,
    比MA10更早预警——不是因为价格跌破均线,而是量价结构偏离了历史模式。
    """

    def __init__(self):
        self.model = None

    def build_features(self, code, today, lookback=60):
        """为单只股票构建特征向量(最近60天滚动窗口)"""
        start = (pd.Timestamp(today) - timedelta(days=lookback + 30)).strftime("%Y-%m-%d")
        try:
            df = load_daily(code, start, today)
        except Exception:
            return None

        if df.empty or len(df) < 30:
            return None

        df = df.sort_values("date")
        closes = pd.to_numeric(df["close"], errors="coerce").dropna().values
        if len(closes) < 30:
            return None

        cur = closes[-1]

        # 成交量
        if "volume" in df.columns:
            volumes = pd.to_numeric(df["volume"], errors="coerce").fillna(0).values
        elif "amount" in df.columns:
            volumes = pd.to_numeric(df["amount"], errors="coerce").fillna(0).values
        else:
            volumes = np.ones(len(closes))

        # 收益率
        ret_1d = (closes[-1] / closes[-2] - 1) if len(closes) >= 2 else 0
        ret_5d = (closes[-1] / closes[-6] - 1) if len(closes) >= 6 else 0
        ret_20d = (closes[-1] / closes[-21] - 1) if len(closes) >= 21 else 0

        # 波动率
        rets = np.diff(closes[-21:]) / closes[-21:-1] if len(closes) >= 21 else np.array([0])
        vol_5d = np.std(rets[-5:]) if len(rets) >= 5 else 0
        vol_20d = np.std(rets) if len(rets) > 0 else 0

        # MA距离
        ma10 = np.mean(closes[-10:]) if len(closes) >= 10 else cur
        ma20 = np.mean(closes[-20:]) if len(closes) >= 20 else cur
        ma_dist_10 = cur / ma10 - 1
        ma_dist_20 = cur / ma20 - 1

        # 量比
        vol_avg_20 = np.mean(volumes[-21:-1]) if len(volumes) >= 21 else volumes[-1]
        vol_ratio = volumes[-1] / vol_avg_20 if vol_avg_20 > 0 else 1.0

        # 振幅
        high_20d = np.max(closes[-20:]) if len(closes) >= 20 else cur
        low_20d = np.min(closes[-20:]) if len(closes) >= 20 else cur
        amplitude = (high_20d / low_20d - 1) if low_20d > 0 else 0

        # 连涨/连跌天数
        cons_up = 0
        cons_down = 0
        for i in range(len(closes)-1, 0, -1):
            if closes[i] > closes[i-1]:
                if cons_down == 0: cons_up += 1
                else: break
            elif closes[i] < closes[i-1]:
                if cons_up == 0: cons_down += 1
                else: break
            else: break

        # 量价相关性(近20日)
        if len(closes) >= 21:
            vol_chg = np.diff(volumes[-21:])
            price_chg = np.diff(closes[-21:])
            corr = np.corrcoef(vol_chg, price_chg)[0, 1] if len(vol_chg) > 1 and np.std(vol_chg) > 0 and np.std(price_chg) > 0 else 0
        else:
            corr = 0

        return np.array([
            ret_1d, ret_5d, ret_20d,
            vol_5d, vol_20d, vol_ratio,
            ma_dist_10, ma_dist_20,
            amplitude, cons_up, cons_down, corr
        ])

    def fit(self, codes, today):
        """在CSI800上训练Isolation Forest,建立'正常'的基准"""
        from sklearn.ensemble import IsolationForest

        features = []
        for code in codes[:200]:  # 样本200只足够
            f = self.build_features(code, today)
            if f is not None and not np.any(np.isnan(f)):
                features.append(f)

        if len(features) < 50:
            return

        X = np.array(features)
        self.model = IsolationForest(contamination=0.05, random_state=42)  # 5%异常
        self.model.fit(X)
        logger.info(f"异动检测模型训练完成: {len(X)}只样本, 5%异常阈值")

    def detect(self, code, today):
        """检测单只股票是否异常。返回 (is_anomaly, anomaly_score, feature_desc)"""
        if self.model is None:
            return False, 0.0, "模型未训练"

        f = self.build_features(code, today)
        if f is None:
            return False, 0.0, "数据不足"

        f = f.reshape(1, -1)
        pred = self.model.predict(f)[0]    # -1=异常, 1=正常
        score = self.model.score_samples(f)[0]

        is_anomaly = (pred == -1)

        # 描述异常原因
        desc = ""
        if is_anomaly:
            parts = []
            ret_1d = f[0, 0]; vol_ratio = f[0, 5]; corr_pv = f[0, 11]
            if f[0, 2] < -0.15: parts.append("20日跌{mret:.0f}%".format(mret=f[0,2]*100))
            # 放量: 带方向, 量比数值, 含义
            if vol_ratio > 2.0:
                direction = "涨" if ret_1d > 0.01 else ("跌" if ret_1d < -0.01 else "平")
                meaning = "→ 主力主动买入,看多信号" if ret_1d > 0.01 else ("→ 主力出货/抛压,看空信号" if ret_1d < -0.01 else "→ 多空分歧大,待方向确认")
                parts.append("放量{dir}{chg:.1f}%(量比{vr:.1f}x{meaning})".format(
                    dir=direction, chg=ret_1d*100, vr=vol_ratio, meaning=meaning))
            if vol_ratio < 0.3:
                direction = "涨" if ret_1d > 0.01 else ("跌" if ret_1d < -0.01 else "平")
                parts.append("缩量{dir}{chg:.1f}%(量比{vr:.1f}x→ 交投清淡,方向可信度低)".format(
                    dir=direction, chg=ret_1d*100, vr=vol_ratio))
            if f[0, 10] >= 5: parts.append(f"连跌{f[0,10]:.0f}天")
            # 量价背离: 明确方向
            if corr_pv < -0.5 and vol_ratio > 1.5:
                parts.append("量价背离:放量下跌(主力出货特征)")
            elif corr_pv < -0.5:
                parts.append("量价背离:量价反向(关注)")
            if corr_pv > 0.5 and ret_1d > 0.01:
                parts.append("量价配合:放量上涨(健康推升)")
            desc = "; ".join(parts) if parts else "形态偏离历史模式"

        return is_anomaly, score, desc

# ══════════════════════════════════════════════════════════════════
# 3. 板块轮动热力图
# ══════════════════════════════════════════════════════════════════
def sector_rotation_heatmap(panel, stock_info, today, lookback=5):
    """
    计算行业动量排名 + 加速/减速信号。
    返回: DataFrame with columns [industry, n_stocks, mom_1w, mom_1m, accel, signal]
    """
    if stock_info is None or stock_info.empty:
        return pd.DataFrame()

    ind_map = {}
    for _, r in stock_info.iterrows():
        ind_map[r['code']] = r.get('industry_l1', '其他')

    all_dates = panel.index
    today_i = all_dates.get_loc(pd.Timestamp(today)) if pd.Timestamp(today) in all_dates else len(all_dates) - 1

    # 各行业近1周/1月的平均收益
    results = []
    for ind_name in set(ind_map.values()):
        codes_in_ind = [c for c, i in ind_map.items() if i == ind_name and c in panel.columns]
        if len(codes_in_ind) < 3:
            continue

        sub = panel[codes_in_ind]

        # 1周收益
        w1_start = max(0, today_i - 5)
        ret_1w = (sub.iloc[today_i] / sub.iloc[w1_start] - 1).mean()

        # 1月收益
        m1_start = max(0, today_i - 20)
        ret_1m = (sub.iloc[today_i] / sub.iloc[m1_start] - 1).mean()

        # 加速/减速: 比较最近1周vs前3周
        if today_i >= 20:
            ret_prev3w = (sub.iloc[today_i-5] / sub.iloc[max(0, today_i-20)] - 1).mean()
            accel = ret_1w - ret_prev3w
        else:
            accel = 0

        # 量增
        rets = sub.pct_change(fill_method=None).iloc[max(0,today_i-20):today_i+1]
        vol_up = (rets.std().iloc[-1] / rets.std().iloc[:15].mean() - 1) if len(rets) > 15 else 0

        signal = ""
        if ret_1w > 0.03 and accel > 0.01:
            signal = "加速上涨"
        elif ret_1w > 0 and accel < -0.01:
            signal = "上涨减速"
        elif ret_1w < -0.03 and accel < -0.01:
            signal = "加速下跌"
        elif ret_1w < 0 and accel > 0.01:
            signal = "跌势减缓→可能反弹"
        else:
            signal = "横盘"

        results.append({
            'industry': ind_name,
            'n_stocks': len(codes_in_ind),
            'mom_1w': ret_1w,
            'mom_1m': ret_1m,
            'accel': accel,
            'vol_change': vol_up,
            'signal': signal,
        })

    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values('mom_1m', ascending=False)
    return df

def format_rotation_report(df):
    """格式化板块轮动为文本报告"""
    if df.empty:
        return "数据不足"

    lines = ["🔥 板块轮动热力图", "─" * 36]

    # 领涨
    top5 = df.head(5)
    lines.append("  📈 领涨板块:")
    for _, r in top5.iterrows():
        lines.append(f"    {r['industry']:<8} 1周{r['mom_1w']*100:+5.1f}% 1月{r['mom_1m']*100:+5.1f}% {r['signal']}")

    # 领跌
    bot3 = df.tail(3)
    lines.append("  📉 弱势板块:")
    for _, r in bot3.iterrows():
        lines.append(f"    {r['industry']:<8} 1周{r['mom_1w']*100:+5.1f}% 1月{r['mom_1m']*100:+5.1f}% {r['signal']}")

    # 反弹/见顶信号
    rebounds = df[df['signal'].str.contains('反弹')]
    peaks = df[df['signal'].str.contains('减速')]
    if not rebounds.empty:
        lines.append(f"  🔄 可能反弹: {', '.join(rebounds['industry'].values)}")
    if not peaks.empty:
        lines.append(f"  ⚠️ 涨势减缓: {', '.join(peaks['industry'].values)}")

    lines.append("─" * 36)
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 独立测试
    from scripts.run_backtest_a import load_panels, BACKTEST_START
    cal = load_meta("trade_calendar")
    dates = sorted(cal["trade_date"].tolist())
    today = dates[-1]
    print(f"测试日期: {today}")

    # Test anomaly detector
    c800 = load_meta("csi800")
    codes = sorted(c800["code"].tolist())[:100]
    print(f"训练异动检测({len(codes)}只)...")
    ad = AnomalyDetector()
    ad.fit(codes, today)

    # Test on a holding
    for code in ['300408', '603156', '002049']:
        is_anom, score, desc = ad.detect(code, today)
        print(f"  {code}: 异常={is_anom} 分数={score:.3f} {desc}")
