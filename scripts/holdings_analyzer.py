"""
HoldingsAnalyzer — 多因子持仓分析引擎

把 Track A 的 compute_score_a2 引入个股持仓监测：
  - 因子分位(CSI800内排名)
  - 得分趋势(上升/下降/稳定)
  - 行业强弱
  - 超跌反弹检测
  - 成交量异常
  - T+0做T信号(超跌补仓/趋势加仓/放量下跌回避)
  - 加仓判断(因子强+缩量回调=补仓, 因子弱+放量跌=不补)

约束: 本工具不生成买入信号。价格止损永远优先于因子信号。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd, numpy as np
from datetime import date, datetime, timedelta
from loguru import logger
from pathlib import Path
import json

from data.storage import load_daily, load_meta, save_meta
from scripts.run_backtest_a import load_panels, BACKTEST_START
from scripts.run_backtest_a2 import compute_score_a2, get_position_ratio

SCORE_CACHE = Path("logs/score_history.parquet")

# ══════════════════════════════════════════════════════════════════════
class HoldingsAnalyzer:
    """
    预计算CSI800全市场因子得分，为每只持仓提供因子上下文。

    用法:
        analyzer = HoldingsAnalyzer()
        result = analyzer.evaluate(code, name, cost, shares, buy_date, is_etf)
    """

    def __init__(self, today_str: str | None = None):
        if today_str is None:
            today_str = date.today().strftime("%Y-%m-%d")

        # Find most recent trading day (today might be weekend)
        cal = load_meta("trade_calendar")
        cal_dates = sorted(cal["trade_date"].tolist())
        self.today = max(d for d in cal_dates if d <= today_str) if cal_dates else today_str

        self.date_ts = pd.Timestamp(self.today)
        logger.info(f"HoldingsAnalyzer: 交易日={self.today}")

        self._load_universe()
        self._compute_scores()
        self._load_hist_scores()
        self._compute_regime()

    # ── 数据加载 ──────────────────────────────────────
    def _load_universe(self):
        """加载CSI800成分股 + 价格面板 + 行业映射"""
        # CSI800成分
        c800 = load_meta("csi800")
        self.codes = sorted(c800["code"].tolist())
        logger.info(f"  CSI800: {len(self.codes)}只成分股")

        # 价格面板 (420天覆盖12个月动量)
        start = (self.date_ts - timedelta(days=420)).strftime("%Y-%m-%d")
        logger.info(f"  加载价格面板 {start} → {self.today} ...")
        try:
            self.panel, self.amount_panel = load_panels(self.codes, start, self.today)
        except Exception as e:
            logger.error(f"  load_panels 失败: {e}, 使用空面板")
            self.panel = pd.DataFrame()
            self.amount_panel = pd.DataFrame()

        # 行业映射
        info = load_meta("stock_info_full")
        self.industry_map = {}
        if not info.empty:
            info["code"] = info["code"].astype(str).str.zfill(6)
            for _, r in info.iterrows():
                self.industry_map[r["code"]] = r.get("industry_l1", "其他")
        logger.info(f"  行业映射: {len(self.industry_map)}只")

    # ── 因子计分 ──────────────────────────────────────
    def _compute_scores(self):
        """运行 compute_score_a2 → 得分 + 分位 + 行业均值"""
        if self.panel.empty:
            self.score = pd.Series(dtype=float)
            self.score_pct = pd.Series(dtype=float)
            self.ind_score_pct = pd.Series(dtype=float)
            return

        stock_info = None
        if self.industry_map:
            si_df = pd.DataFrame([
                {"code": c, "industry_l1": self.industry_map.get(c, "其他")}
                for c in self.codes
            ])
            stock_info = si_df

        logger.info("  运行 compute_score_a2 ...")
        try:
            self.score = compute_score_a2(
                self.panel, self.date_ts, self.amount_panel, stock_info
            )
        except Exception as e:
            logger.error(f"  compute_score_a2 失败: {e}")
            self.score = pd.Series(0.0, index=self.codes)

        if self.score.empty:
            logger.warning("  compute_score_a2 返回空, 使用零分")
            self.score = pd.Series(0.0, index=self.codes)

        # 因子分位 (在CSI800内的排名百分比)
        self.score_pct = self.score.rank(pct=True)

        # 行业得分分位
        if stock_info is not None and not self.score.empty:
            ind = pd.Series({c: self.industry_map.get(c, "其他")
                           for c in self.score.index})
            score_df = pd.DataFrame({"score": self.score, "industry": ind})
            ind_mean = score_df.groupby("industry")["score"].mean()
            self.ind_score_pct = ind_mean.rank(pct=True)
        else:
            self.ind_score_pct = pd.Series(dtype=float)

        logger.info(f"  得分范围: {self.score.min():.3f} ~ {self.score.max():.3f}")

    # ── 历史得分趋势(缓存) ─────────────────────────────
    def _load_hist_scores(self):
        """从缓存加载历史得分快照, 追加今日。保留最近4期。"""
        self.hist_scores = []
        if SCORE_CACHE.exists():
            try:
                cached = pd.read_parquet(SCORE_CACHE)
                for col in cached.columns:
                    s = cached[col].dropna()
                    if not s.empty:
                        self.hist_scores.append(s)
                logger.info(f"  历史得分缓存: {len(self.hist_scores)}期")
            except Exception as e:
                logger.warning(f"  缓存读取失败: {e}")

        # 追加今日
        if not self.score.empty:
            self.hist_scores.append(self.score)

        # 保留最近4期
        if len(self.hist_scores) > 4:
            self.hist_scores = self.hist_scores[-4:]

        # 写回缓存
        try:
            hist_df = pd.DataFrame({
                f"s{i}": s for i, s in enumerate(self.hist_scores)
            })
            SCORE_CACHE.parent.mkdir(exist_ok=True)
            hist_df.to_parquet(SCORE_CACHE)
        except Exception as e:
            logger.warning(f"  缓存写入失败: {e}")

    # ── 市场 regime ────────────────────────────────────
    def _compute_regime(self):
        """CSI800/MA200 比值 → bull/neutral/bear"""
        idx_df = load_meta("csi800_index")
        if idx_df.empty:
            self.regime_label = "未知"
            return
        idx_df["date"] = pd.to_datetime(idx_df["date"])
        close = idx_df.set_index("date")["close"].sort_index()
        try:
            ratio = get_position_ratio(close, self.date_ts)
        except Exception:
            ratio = 1.0
        if ratio <= 0.30:
            self.regime_label = "熊(空仓)"
        elif ratio >= 1.0:
            self.regime_label = "牛(满仓)"
        else:
            self.regime_label = f"中({ratio:.0%})"
        logger.info(f"  市场regime: {self.regime_label}")

    # ══════════════════════════════════════════════════════════════════
    # 持仓评估 (对外的核心方法)
    # ══════════════════════════════════════════════════════════════════
    def evaluate(self, code: str, name: str, cost_price: float,
                 shares: int, buy_date: str, is_etf: bool = False,
                 base_result: dict | None = None) -> dict:
        """
        对单只持仓做因子增强评估。

        参数:
            code, name, cost_price, shares, buy_date, is_etf — 持仓信息
            base_result — 已有价格分析结果(可选)。如未提供, 返回仅含因子元数据。

        返回:
            dict with keys: code, name, action, reason, score_pct, score_trend,
            sector, sector_score, regime, bounce, vol_ratio, cur, ma10, pnl_pct,
            below_ma, trail_dd, high, cost, shares, mktval, etf
        """
        empty = {
            "code": code, "name": name, "cur": None, "ma10": None,
            "pnl_pct": None, "below_ma": 0, "trail_dd": 0,
            "high": None, "cost": cost_price, "shares": shares,
            "mktval": 0, "etf": is_etf,
        }
        if base_result is None:
            base_result = {**empty, "action": "hold", "reason": ""}
        else:
            for k in empty:
                base_result.setdefault(k, empty[k])

        # ── 两融数据 ───────────────────────────────
        margin_signal = self._get_margin_signal(code)
        base_result["margin_signal"] = margin_signal

        # ── 龙虎榜 ─────────────────────────────────
        dragon_signal = self._get_dragon_signal(code)
        base_result["dragon_signal"] = dragon_signal

        # ── 业绩预警 ───────────────────────────────
        earnings_alert = self._get_earnings_alert(code)
        base_result["earnings_alert"] = earnings_alert

        # ── 因子元数据 ───────────────────────────────
        score_pct = self._get_score_pct(code)
        score_trend = self._get_score_trend(code)
        sector = self.industry_map.get(code, "?")
        sector_score = self._get_sector_pct(sector)
        bounce = self._detect_bounce(code)
        vol_ratio = self._volume_ratio(code)

        base_result["score_pct"] = score_pct
        base_result["score_trend"] = score_trend
        base_result["sector"] = sector
        base_result["sector_score"] = sector_score
        base_result["regime"] = self.regime_label
        base_result["bounce"] = bounce
        base_result["vol_ratio"] = vol_ratio

        # ── 前向预测 & 做T/补仓 ─────────────────────
        pnl_pct = base_result.get("pnl_pct", 0)
        t_signal, add_signal, suggestion, pred_details = self._predict(
            code, score_pct, score_trend, bounce, vol_ratio, pnl_pct
        )
        base_result["t_signal"] = t_signal
        base_result["add_signal"] = add_signal
        base_result["suggestion"] = suggestion
        base_result["pred_details"] = pred_details

        # ── 因子信号覆盖 ─────────────────────────────
        # 价格止损触发 → 保持原信号, 只附加因子元数据
        orig_action = base_result.get("action", "hold")
        if orig_action in ("sell", "reduce", "locked", "nodata", "skip"):
            return base_result

        new_action, new_reason = self._factor_override(
            base_result, score_pct, score_trend, sector_score, bounce, vol_ratio
        )
        base_result["action"] = new_action
        base_result["reason"] = new_reason
        return base_result

    # ── 因子信号决策树 ───────────────────────────────
    def _factor_override(self, base, score_pct, score_trend,
                         sector_score, bounce, vol_ratio):
        """
        价格止损未触发 → 因子信号介入。

        优先级:
          1. cut: 分位<50% + 非反弹 → 基本面走弱, 建议卖出
          2. strong_hold: 分位>80% + 趋势↑ + 行业强
          3. weakening: 趋势↓连续2+期
          4. bounce_upgrade: 反弹 + 分位≥50% → warn→hold
          5. 默认: 保留原信号
        """
        action = base.get("action", "hold")
        reason = base.get("reason", "")

        # CUT: 基本面崩塌
        if score_pct is not None and score_pct < 0.50 and not bounce:
            return ("cut",
                    f"因子走弱(CSI800内{score_pct:.0%}分位, 低于50%), 建议择机卖出")

        # STRONG_HOLD: 因子强劲
        if (action in ("hold", "warn")
                and score_pct is not None and score_pct > 0.80
                and score_trend == "rising"
                and sector_score is not None and sector_score > 0.50):
            extras = [f"因子强劲({score_pct:.0%}分位/CSI800, 行业{sector_score:.0%}分位, 得分↑)"]
            if vol_ratio is not None and vol_ratio > 1.5:
                extras.append(f"放量{vol_ratio:.1f}倍")
            return ("strong_hold", "; ".join(extras))

        # OVERBOUGHT/趋势逆转: 盈利中但出现危险信号 → 预警减仓
        pnl = base.get("pnl_pct", 0) / 100 if base.get("pnl_pct") else 0
        if action == "hold" and pnl > 0.05:
            pred = base.get("pred_details", {})
            at_high = pred.get("at_high_20d", -1)
            mom5 = pred.get("mom_5d", 0)
            vp = pred.get("vol_pattern", "neutral")
            # Near resistance + overbought short-term + distribution → likely pullback
            if at_high > -0.03 and mom5 > 0.08 and vp == "distribution":
                return ("reduce", f"盈利{pnl:+.0%}+近阻力({at_high:+.0%})+放量出货+5日{mom5:+.0%}, 建议减仓锁定利润")
            # Near resistance + factor weakening → reduce
            if at_high > -0.02 and score_trend == "falling":
                return ("reduce", f"盈利{pnl:+.0%}+近阻力({at_high:+.0%})+因子↓, 建议减仓")
            # Short-term overbought + stuck at resistance
            if at_high > -0.01 and mom5 > 0.15 and score_pct is not None and score_pct < 0.70:
                return ("warn", f"短线超买(5日{mom5:+.0%})+顶阻力+因子一般({score_pct:.0%}分位), 注意回调风险")

        # WEAKENING: 得分持续下降
        if score_trend == "falling" and action == "hold":
            return ("weakening",
                    f"因子得分连续下降, 关注风险(当前{score_pct:.0%}分位)")

        # BOUNCE UPGRADE: 超跌反弹
        if bounce:
            # 深度亏损(>50%)不提示抄底 — 底在哪儿都不知道
            pnl = base_result.get("pnl_pct")
            if pnl is not None and pnl <= -0.50:
                return (action, f"{reason} (超跌反弹但亏损>50%, 不抄底)")
            if score_pct is not None and score_pct >= 0.50:
                if action == "warn":
                    return ("hold", f"超跌反弹信号(收阳), 因子尚可({score_pct:.0%}分位) {reason}")
                else:
                    return (action, f"超跌反弹信号(收阳) {reason}")
            else:
                # 反弹但因子弱 → 借反弹减仓
                return ("warn",
                        f"超跌反弹但因子偏弱({score_pct:.0%}分位), 反弹后考虑减仓")

        # 默认: 保留原信号
        return (action, reason)

    # ══════════════════════════════════════════════════════════════════
    # 前向预测 & 做T/补仓信号
    # ══════════════════════════════════════════════════════════════════
    def _predict(self, code, score_pct, score_trend, bounce, vol_ratio, pnl_pct):
        """
        综合多维度给出前向预测和操作建议。
        返回 (t_signal, add_signal, suggestion, details)
        """
        md = self._load_code_mini(code)
        if md is None:
            return "unknown", "wait", "数据不足", {}

        cur = md["cur"]; closes = md["closes"]; volumes = md["volumes"]
        high_20d = md["high_20d"]; low_20d = md["low_20d"]
        at_high = (cur / high_20d - 1) if high_20d > 0 else 0
        at_low  = (cur / low_20d - 1) if low_20d > 0 else 0
        pnl = pnl_pct / 100 if pnl_pct else 0

        # ── 涨跌结构 ──
        cons_up = 0; cons_down = 0
        for i in range(len(closes)-1, 0, -1):
            if closes[i] > closes[i-1]:
                if cons_down == 0: cons_up += 1
                else: break
            elif closes[i] < closes[i-1]:
                if cons_up == 0: cons_down += 1
                else: break
            else: break

        mom_5d = (closes[-1] / closes[-6] - 1) if len(closes) >= 6 else 0
        mom_20d = (closes[-1] / closes[-21] - 1) if len(closes) >= 21 else 0

        # ── 量价结构(近10日) ──
        up_vol_avg = 0; up_days = 0; down_vol_avg = 0; down_days = 0
        for i in range(max(1, len(closes)-10), len(closes)):
            chg = closes[i] / closes[i-1] - 1
            if chg > 0:
                up_vol_avg += volumes[i] if i < len(volumes) else 0; up_days += 1
            elif chg < 0:
                down_vol_avg += volumes[i] if i < len(volumes) else 0; down_days += 1
        up_vol_avg = up_vol_avg / up_days if up_days > 0 else 0
        down_vol_avg = down_vol_avg / down_days if down_days > 0 else 0
        # Accumulation: up days have higher volume than down days
        vol_pattern = "neutral"
        if up_days >= 3 and down_days >= 3 and up_vol_avg > 0 and down_vol_avg > 0:
            if up_vol_avg > down_vol_avg * 1.3:
                vol_pattern = "accumulation"   # 上涨放量, 下跌缩量 → 吸筹
            elif down_vol_avg > up_vol_avg * 1.3:
                vol_pattern = "distribution"   # 下跌放量, 上涨缩量 → 出货

        # ── T+0 做T信号 ──
        t_signal = "none"
        t_reason = ""
        # 超卖补仓: 连跌3天+因子≥70%+缩量+距支撑近
        if (cons_down >= 3 and score_pct is not None and score_pct >= 0.70
                and vol_ratio is not None and vol_ratio < 0.8
                and at_low < 0.05):
            t_signal = "dip_buy"
            t_reason = f"连跌{cons_down}天+因子{score_pct:.0%}分位+缩量+近20日低({at_low:+.1%})"
        # 趋势加仓: 盈利+创新高+因子↑+放量
        elif (pnl > 0.03 and at_high > -0.02 and score_trend == "rising"
                and vol_ratio is not None and vol_ratio > 1.2
                and vol_pattern == "accumulation"):
            t_signal = "trend_ride"
            t_reason = f"盈利{pnl:+.1%}+近20日高({at_high:+.1%})+因子↑+吸筹结构"
        # 反弹做T: 超跌+反弹+因子强
        elif bounce and score_pct is not None and score_pct >= 0.70:
            t_signal = "bounce_t"
            t_reason = f"超跌反弹+因子强({score_pct:.0%}分位), 可T+0高抛"
        # 放量下跌回避
        elif (cons_down >= 2 and vol_ratio is not None and vol_ratio > 1.5
                and score_pct is not None and score_pct < 0.60):
            t_signal = "falling_knife"
            t_reason = f"放量{vol_ratio:.1f}倍下跌+因子弱({score_pct:.0%}分位), 不接飞刀"

        # ── 加仓/补仓判断 ──
        add_signal = "wait"
        add_reason = ""
        if score_pct is not None and score_pct >= 0.70 and score_trend in ("rising", "stable"):
            if pnl > 0.05 and at_high < -0.03 and vol_pattern == "accumulation":
                # 盈利+距压力位远+吸筹 → 可以加仓
                add_signal = "yes_add"
                add_reason = f"盈利中(pnl {pnl:+.1%})+距压力{at_high:.0%}+吸筹结构, 回调可加仓"
            elif pnl < -0.03 and cons_down >= 2 and vol_pattern != "distribution":
                # 亏损+连跌+非出货 → 补仓摊薄
                add_signal = "yes_add"
                add_reason = f"浮亏{abs(pnl):.1%}+连跌{cons_down}天+非出货结构, 可补仓摊薄"
            elif score_trend == "rising" and at_high < -0.05:
                add_signal = "yes_add"
                add_reason = f"因子上升+距压力{at_high:.0%}, 有加仓空间"
        elif score_pct is not None and score_pct < 0.50:
            add_signal = "no_add"
            add_reason = f"因子弱({score_pct:.0%}分位), 不加仓"
        elif vol_pattern == "distribution":
            add_signal = "no_add"
            add_reason = "量价结构=出货, 不加仓"
        elif score_trend == "falling":
            add_signal = "no_add"
            add_reason = "因子趋势↓, 不加仓"

        # ── 综合建议 ──
        parts = []
        if t_signal == "dip_buy": parts.append("📉 超卖+因子强→可T+0抄底做T")
        elif t_signal == "trend_ride": parts.append("📈 趋势延续+吸筹→持有并可加仓做T")
        elif t_signal == "bounce_t": parts.append("🔄 超跌反弹→可T+0高抛, 回落再接")
        elif t_signal == "falling_knife": parts.append("⚠️ 放量下跌+因子弱→不接飞刀")

        if add_signal == "yes_add": parts.append(f"✅ {add_reason}")
        elif add_signal == "no_add": parts.append(f"❌ {add_reason}")
        else: parts.append("⏸ 观察等待")

        suggestion = "; ".join(parts) if parts else "正常持有"

        details = {
            "cons_up": cons_up, "cons_down": cons_down,
            "mom_5d": mom_5d, "mom_20d": mom_20d,
            "at_high_20d": at_high, "at_low_20d": at_low,
            "vol_pattern": vol_pattern, "pnl": pnl,
            "t_reason": t_reason, "add_reason": add_reason,
        }
        return t_signal, add_signal, suggestion, details

    def _load_code_mini(self, code):
        """加载单只股票的迷你面板(最近30天), 返回用于预测的原始数据."""
        start = (self.date_ts - timedelta(days=45)).strftime("%Y-%m-%d")
        try:
            df = load_daily(code, start, self.today)
        except Exception:
            return None
        if df.empty or len(df) < 15:
            return None
        df = df.sort_values("date")
        closes = pd.to_numeric(df["close"], errors="coerce").dropna().values
        # Try to get volume
        if "volume" in df.columns:
            volumes = pd.to_numeric(df["volume"], errors="coerce").fillna(0).values
        elif "amount" in df.columns:
            volumes = pd.to_numeric(df["amount"], errors="coerce").fillna(0).values
        else:
            volumes = np.ones(len(closes))
        if len(closes) < 10:
            return None
        cur = closes[-1]
        high_20d = max(closes[-min(20, len(closes)):])
        low_20d = min(closes[-min(20, len(closes)):])
        return {"cur": cur, "closes": closes, "volumes": volumes,
                "high_20d": high_20d, "low_20d": low_20d}

    # ══════════════════════════════════════════════════════════════════
    # 两融数据
    # ══════════════════════════════════════════════════════════════════
    def _get_margin_signal(self, code: str) -> str | None:
        """获取单只股票的两融信号。优先读缓存,缓存不存在时尝试实时拉取。"""
        cache_file = Path('logs/margin_cache.json')

        # 1. 尝试读缓存
        if not hasattr(self, '_margin_cache'):
            self._margin_cache = {}
            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text())
                    # 两融T+1: 今天的数据明天才有, 缓存日期≤2天都可用
                    cache_date = cached.get('date', '')
                    if cache_date and cache_date >= (pd.Timestamp(self.today) - pd.Timedelta(days=2)).strftime('%Y-%m-%d'):
                        self._margin_cache = cached.get('data', {})
                except Exception:
                    pass

        # 2. 缓存为空时实时拉取(仅在盘前可用)
        if not self._margin_cache:
            try:
                import akshare as ak
                date_str = self.today.replace('-', '')
                sh = ak.stock_margin_detail_sse(date=date_str)
                sz = ak.stock_margin_detail_szse(date=date_str)
                for _, r in sh.iterrows():
                    c = str(r['标的证券代码']).zfill(6)
                    self._margin_cache[c] = {
                        'balance': float(r['融资余额']), 'buy': float(r['融资买入额']),
                        'repay': float(r['融资偿还额']), 'short': float(r['融券余量'])
                    }
                for _, r in sz.iterrows():
                    c = str(r['标的证券代码']).zfill(6)
                    self._margin_cache[c] = {
                        'balance': float(r['融资余额']), 'buy': float(r['融资买入额']),
                        'repay': float(r['融资偿还额']), 'short': float(r['融券余量'])
                    }
                # 保存缓存
                cache_file.parent.mkdir(exist_ok=True)
                cache_file.write_text(json.dumps({'date': self.today, 'data': self._margin_cache}))
            except Exception:
                return None

        # 3. 查信号
        info = self._margin_cache.get(code)
        if not info: return None
        balance, buy, repay, short = info['balance'], info['buy'], info['repay'], info['short']
        if balance == 0: return None

        net_ratio = (buy - repay) / balance
        buy_ratio = buy / balance
        parts = []
        if net_ratio > 0.05: parts.append('融资大幅净买入(杠杆看多)')
        elif net_ratio > 0.01: parts.append('融资净买入')
        elif net_ratio < -0.05: parts.append('融资大幅净偿还(杠杆撤离)')
        elif net_ratio < -0.01: parts.append('融资净偿还')
        if buy_ratio > 0.15: parts.append('杠杆极度活跃')
        if short > 1e6: parts.append('融券大量堆积(空头重仓)')
        elif short > 5e5: parts.append('融券偏高')
        return '; '.join(parts) if parts else '两融中性'

    # ══════════════════════════════════════════════════════════════════
    # 龙虎榜
    # ══════════════════════════════════════════════════════════════════
    def _get_dragon_signal(self, code: str) -> str | None:
        """检查股票是否上龙虎榜,返回净买卖和解读。"""
        if not hasattr(self, '_dragon_cache'):
            self._dragon_cache = {}
            try:
                import akshare as ak
                df = ak.stock_lhb_detail_em()
                for _, r in df.iterrows():
                    c = str(r['代码']).zfill(6)
                    self._dragon_cache[c] = {
                        'net_buy': float(r['龙虎榜净买额']) if pd.notna(r.get('龙虎榜净买额')) else 0,
                        'chg': float(r['涨跌幅']) if pd.notna(r.get('涨跌幅')) else 0,
                        'reason': str(r.get('解读', ''))[:80],
                    }
            except Exception:
                pass
        info = self._dragon_cache.get(code)
        if not info: return None
        net = info['net_buy'] / 1e4  # 万元
        tag = '机构净买入' if net > 1000 else ('机构净卖出' if net < -1000 else '')
        reason = info['reason']
        return f'🔥上榜! 净买{net:+.0f}万 {tag} {reason}'

    # ══════════════════════════════════════════════════════════════════
    # 业绩预警
    # ══════════════════════════════════════════════════════════════════
    def _get_earnings_alert(self, code: str) -> str | None:
        """检查持仓是否有最新业绩预告。"""
        if not hasattr(self, '_earnings_cache'):
            self._earnings_cache = {}
            try:
                import akshare as ak
                df = ak.stock_yjyg_em(date='20260630')
                for _, r in df.iterrows():
                    c = str(r['股票代码']).zfill(6)
                    self._earnings_cache[c] = {
                        'type': str(r.get('预告类型', '')),
                        'chg_min': float(r.get('业绩变动下限', 0) or 0),
                        'chg_max': float(r.get('业绩变动上限', 0) or 0),
                    }
            except Exception:
                pass
        info = self._earnings_cache.get(code)
        if not info: return None
        chg = info.get('chg_max', 0)
        etype = info.get('type', '')
        if chg > 50: return f'📈业绩预增{chg:.0f}%({etype})'
        elif chg < -30: return f'⚠️业绩预减{chg:.0f}%({etype})'
        elif '亏' in etype: return f'🚨业绩预亏({etype})'
        return f'业绩{etype}'

    # ══════════════════════════════════════════════════════════════════
    # 辅助方法
    # ══════════════════════════════════════════════════════════════════
    def _get_score_pct(self, code: str) -> float | None:
        if code in self.score_pct.index:
            return float(self.score_pct[code])
        return None

    def _get_score_trend(self, code: str) -> str:
        """比较当期 vs 2-3期前的得分变化"""
        if len(self.hist_scores) < 2:
            return "unknown"
        current = self.score.get(code, 0) if not self.score.empty else 0
        old_idx = max(0, len(self.hist_scores) - 3)
        old = self.hist_scores[old_idx].get(code, 0) if old_idx < len(self.hist_scores) else 0
        if old == 0:
            return "unknown"
        ratio = current / old if old != 0 else 1
        if ratio > 1.05:
            return "rising"
        elif ratio < 0.95:
            return "falling"
        return "stable"

    def _get_sector_pct(self, sector: str) -> float | None:
        if sector in self.ind_score_pct.index:
            return float(self.ind_score_pct[sector])
        return None

    def _detect_bounce(self, code: str) -> bool:
        """
        超跌反弹检测:
        - 从30日高点跌 > 10%
        - 今日收阳(收盘 > 昨收)
        """
        start = (self.date_ts - timedelta(days=45)).strftime("%Y-%m-%d")
        try:
            df = load_daily(code, start, self.today)
        except Exception:
            return False
        if df.empty or len(df) < 20:
            return False
        df = df.sort_values("date")
        closes = pd.to_numeric(df["close"], errors="coerce").dropna().values
        if len(closes) < 20:
            return False
        cur = closes[-1]
        high_30d = max(closes[-min(30, len(closes)):])
        drop = (cur / high_30d - 1) if high_30d > 0 else 0
        if drop > -0.10:  # not dropped enough (<10%)
            return False
        # 收阳: 今日收盘 > 昨日收盘, 且涨幅 >= 1% (过滤噪声)
        if len(closes) >= 2 and cur > closes[-2] * 1.01:
            return True
        return False

    def _volume_ratio(self, code: str) -> float | None:
        """今日成交额 / 20日均成交额"""
        start = (self.date_ts - timedelta(days=30)).strftime("%Y-%m-%d")
        try:
            df = load_daily(code, start, self.today)
        except Exception:
            return None
        if df.empty or len(df) < 21:
            return None
        df = df.sort_values("date")
        amounts = pd.to_numeric(df["amount"], errors="coerce").dropna().values
        if len(amounts) < 21:
            return None
        recent = amounts[-1]
        avg_20 = amounts[-21:-1].mean()
        if avg_20 <= 0:
            return None
        return float(recent / avg_20)


# ══════════════════════════════════════════════════════════════════════
# 多维度候选股筛选 (修正版 — 2026-06-26 实盘教训)
# ══════════════════════════════════════════════════════════════════════
def screen_candidates(codes, panel, sector_data, fundamentals, signal_info, held_set):
    """
    多维度候选股评分筛选。

    修正要点（经6/26实盘验证）：
    - 过热惩罚权重大于板块/信号加分
    - 20日翻倍股直接淘汰
    - 板块加分上限+2，信号加分上限+2

    参数:
        codes: 候选代码列表
        panel: 价格面板 (index=date, columns=code)
        sector_data: dict {ind: {sig, ret_1m, ...}}
        fundamentals: DataFrame with code index, has 'eps' column
        signal_info: dict {code: {in_buy, in_sell, weight}}
        held_set: set of currently held codes

    返回: 按综合评分降序的列表
    """
    results = []
    for code in codes:
        if code not in panel.columns:
            continue
        cl = panel[code].dropna()
        if len(cl) < 20:
            continue

        cur = cl.iloc[-1]
        ma10 = cl.iloc[-10:].mean() if len(cl) >= 10 else cur
        ret5 = (cur / cl.iloc[-6] - 1) * 100 if len(cl) >= 6 else 0
        ret20 = (cur / cl.iloc[-21] - 1) * 100 if len(cl) >= 21 else 0
        high20 = cl.iloc[-20:].max()
        dh = (cur / high20 - 1) * 100  # distance from 20d high

        # Consecutive up days
        cons_up = 0
        for i in range(len(cl) - 1, 0, -1):
            if cl.iloc[i] > cl.iloc[i - 1]:
                cons_up += 1
            else:
                break

        # ═══ 硬淘汰 ═══
        if ret20 > 80:
            continue  # 20日翻倍以上，直接淘汰
        if ret5 > 30:
            continue  # 5日30%以上，短期极端

        # ═══ 信号信息 ═══
        si = signal_info.get(code, {})
        in_buy = si.get('in_buy', False)
        in_sell = si.get('in_sell', False)

        if in_sell:
            continue  # 信号卖出，不纳入候选

        # ═══ 板块 ═══
        si_full = signal_info.get(code, {})
        ind = si_full.get('industry', '其他')
        sec = sector_data.get(ind, {})
        sec_sig = sec.get('sig', '')
        sec_1m = sec.get('m', 0) * 100 if isinstance(sec.get('m'), float) else 0

        # ═══ 基本面 ═══
        eps = float(fundamentals.loc[code, 'eps']) if code in fundamentals.index else float('nan')
        if pd.isna(eps):
            eps = 0.0

        # ═══ 量比 ═══
        vr = si_full.get('vol_ratio', 1.0) or 1.0

        # ══════════ 评分 ══════════
        score = 0
        reasons = []

        # ── 过热惩罚（权重最高！） ──
        if ret20 > 50:
            score -= 6
            reasons.append(f'20日{ret20:.0f}%⚠️重罚')
        elif ret20 > 30:
            score -= 4
            reasons.append(f'20日{ret20:.0f}%⚠️')
        if ret5 > 15:
            score -= 3
            reasons.append(f'5日{ret5:.0f}%⚠️')
        if cons_up >= 5:
            score -= 3
            reasons.append(f'连涨{cons_up}天⚠️')
        # 正好在最高点+还在涨 = 追高
        if dh > -1 and cons_up >= 2:
            score -= 1
            reasons.append('追高')

        # ── 技术面 (0~3) ──
        if cur > ma10:
            score += 1
            reasons.append('MA10上')
        else:
            score -= 2
            reasons.append('MA10下✗')
        if dh < -5:
            score += 2
            reasons.append(f'空间{dh:.0f}%')
        elif dh < -3:
            score += 1
        elif dh > -1 and cons_up < 2:
            reasons.append('近阻力')

        # ── 基本面 (0~2) ──
        if eps > 1.0:
            score += 2
            reasons.append(f'EPS{eps:.1f}')
        elif eps > 0.3:
            score += 1
        elif eps <= 0:
            score -= 2
            reasons.append('亏损✗')

        # ── 量能 (0~1) ──
        if 0.7 < vr < 3.0:
            score += 1
        elif vr >= 3.0:
            reasons.append(f'高量{vr:.1f}x')

        # ── 板块 (上限+2) ──
        if '加速上涨' in sec_sig:
            score += 2
            reasons.append(f'板块{sec_sig}')
        elif '跌势减缓' in sec_sig:
            score += 1
            reasons.append(f'板块{sec_sig}')
        elif '加速下跌' in sec_sig:
            score -= 1
            reasons.append(f'板块{sec_sig}')
        if sec_1m > 3:
            score += 1

        # ── 信号 (上限+2) ──
        if in_buy:
            score += 2
            reasons.append('信号买入')

        results.append({
            'code': code, 'score': score, 'ret20': ret20, 'ret5': ret5,
            'cons_up': cons_up, 'dh': dh, 'eps': eps, 'vr': vr,
            'ind': ind, 'sec_sig': sec_sig,
            'in_buy': in_buy, 'reasons': ' | '.join(reasons),
            'cur': cur, 'ma10': cur > ma10,
        })

    results.sort(key=lambda x: (x['score'], -x['ret20']), reverse=True)
    return results
