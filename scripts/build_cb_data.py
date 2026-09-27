"""
可转债数据管道 — Stage 1: 基准指数 + 全量日线 + 强赎退市记录

阶段:
  1. 中证转债指数(000832) — 基准收益曲线
  2. 可转债全量列表 — 代码/上市日/退市日/评级/规模
  3. 逐只日线数据 — 价格/转股溢价率/成交额
  4. 强赎/退市记录 — 幸存者偏差处理
  5. point-in-time快照 — 每月调仓日可用标的

用法: python scripts/build_cb_data.py [--stage 1|2|3|4|5]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd, numpy as np
from datetime import datetime, timedelta, date
from loguru import logger
from data.storage import save_meta, load_meta
from monitoring.alerts import send_alert
import time
import akshare as ak

CB_DIR = Path("data_store/convertible_bonds")
CB_DIR.mkdir(exist_ok=True)
DAILY_DIR = CB_DIR / "daily"
DAILY_DIR.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════════
# Stage 1: 中证转债指数
# ══════════════════════════════════════════════════════════════════
def build_index():
    """获取中证转债指数日线数据(000832)。"""
    logger.info("Stage 1: 中证转债指数...")

    dfs = []
    # akshare bond_zh_hs_daily 对 sh000832 只到2018年
    # 用 bond_cb_index_jsl 获取近期数据(含 idx_price)
    try:
        idx_jsl = ak.bond_cb_index_jsl()
        if not idx_jsl.empty:
            idx_jsl['date'] = pd.to_datetime(idx_jsl['price_dt'])
            idx_jsl = idx_jsl.rename(columns={'idx_price': 'close'})
            dfs.append(idx_jsl[['date', 'close']])
            logger.info(f"  bond_cb_index_jsl: {len(idx_jsl)}行, {idx_jsl.date.min().date()}→{idx_jsl.date.max().date()}")
    except Exception as e:
        logger.warning(f"  bond_cb_index_jsl失败: {e}")

    # Also try stock_zh_index_daily for 000832 (中证转债指数)
    try:
        idx_em = ak.stock_zh_index_daily(symbol="sh000832")
        if not idx_em.empty:
            idx_em = idx_em.rename(columns={'date': 'date', 'close': 'close'})
            if 'date' in idx_em.columns:
                idx_em['date'] = pd.to_datetime(idx_em['date'])
                dfs.append(idx_em[['date', 'close']])
                logger.info(f"  stock_zh_index_daily: {len(idx_em)}行, {idx_em.date.min().date()}→{idx_em.date.max().date()}")
    except Exception as e:
        logger.warning(f"  stock_zh_index_daily失败: {e}")

    if dfs:
        idx_all = pd.concat(dfs).drop_duplicates('date').sort_values('date')
        idx_all['close'] = pd.to_numeric(idx_all['close'], errors='coerce')
        idx_all = idx_all.dropna()
        save_meta("cb_index", idx_all)
        logger.info(f"  ✓ 转债指数: {len(idx_all)}行, {idx_all.date.min().date()}→{idx_all.date.max().date()}")
    else:
        logger.error("  无法获取转债指数数据")

# ══════════════════════════════════════════════════════════════════
# Stage 2: 可转债全量列表
# ══════════════════════════════════════════════════════════════════
def build_universe():
    """获取所有可转债的基本信息: 代码/上市日/退市日/评级/规模/转股价"""
    logger.info("Stage 2: 可转债全量列表...")

    # 当前存续
    try:
        cov = ak.bond_zh_cov()
        logger.info(f"  当前列表: {len(cov)}只")
    except Exception as e:
        logger.error(f"  bond_zh_cov失败: {e}")
        return

    # 补充历史已退市 — 尝试 bond_cb_jsl 获取更全的列表
    records = []
    for _, r in cov.iterrows():
        records.append({
            'code': str(r.get('债券代码', '')),
            'name': str(r.get('债券简称', '')),
            'stock_code': str(r.get('正股代码', '')),
            'stock_name': str(r.get('正股简称', '')),
            'list_date': str(r.get('上市时间', '')),
            'issue_size': float(r.get('发行规模', 0) or 0),
            'rating': str(r.get('信用评级', '')),
            'convert_price': float(r.get('转股价', 0) or 0),
        })

    df = pd.DataFrame(records)
    save_meta("cb_universe", df)
    logger.info(f"  ✓ 转债列表: {len(df)}只")

# ══════════════════════════════════════════════════════════════════
# Stage 3: 逐只日线数据
# ══════════════════════════════════════════════════════════════════
def build_daily(batch_size=50, sleep_sec=2, force_refresh=False):
    """下载每只可转债的日线数据(含价格/溢价率)。"""
    # 2026-09-27: 非交易日跳过——国庆 10-01~10-08 长假期间 19:45 日更 cron
    # 会空转 8 次×1051 次 API 且 akshare 返回同一份旧数据(无意义重写+限流风险);
    # 交易日 19:45 时 17:00 日更链已把今日写入日历, 本检查不误伤
    try:
        from data.storage import load_meta as _lm
        _cal = _lm("trade_calendar")
        if not _cal.empty:
            _tds = set(_cal["trade_date"].astype(str).tolist())
            if _tds and date.today().strftime("%Y-%m-%d") not in _tds:
                logger.info(f"{date.today()} 非交易日，跳过日线刷新")
                return
    except Exception:
        pass  # 日历不可用时不拦(周更 cron 在周一 06:30, 日历由日更链维护)
    logger.info("Stage 3: 逐只日线数据...")

    universe = load_meta("cb_universe")
    if universe.empty:
        logger.error("  请先运行 Stage 2")
        return

    codes = universe['code'].tolist()
    total = len(codes)
    done = 0
    skip = 0

    for i, code in enumerate(codes):
        fpath = DAILY_DIR / f"{code}.parquet"
        if fpath.exists():
            # 2026-09-20 修复: 旧逻辑对已有文件一律跳过——CB 日线自
            # 08-06 起整体冻结无人察觉(闸门十二形状), 快照链随之停摆。
            # 改为: 陈旧文件(最后bar>10天前)重拉覆盖, 新鲜文件才跳过。
            # force_refresh: 前缀bug修复后的一次性全量重拉(错前缀文件
            # 可能带着错券的"新鲜"日期, 靠新鲜度跳不过去)
            try:
                if not force_refresh:
                    _d = pd.read_parquet(fpath, columns=["date"])
                    _mx = str(_d["date"].max())[:10] if not _d.empty else "1900-01-01"
                    if _mx >= (date.today() - timedelta(days=10)).strftime("%Y-%m-%d"):
                        skip += 1
                        continue
            except Exception:
                pass  # 读失败按不存在处理, 走下载

        try:
            # 2026-09-20 修复: 旧"先试sh、空才试sz"启发式对深市转债
            # (12xxxx)返回错券/截断史(127115被写成2016年截断, 一批
            # 12xxxx被写成08-06截断)——非空即保留, sz 分支永不执行。
            # 改为按代码段定交易所: 11=沪, 12/127/128=深。
            prefix = "sh" if code.startswith("11") else "sz"
            df = ak.bond_zh_hs_cov_daily(symbol=f"{prefix}{code}")
            if not df.empty:
                df.to_parquet(fpath)
                done += 1
        except Exception as e:
            pass  # bond may be delisted, skip

        if (i + 1) % batch_size == 0:
            logger.info(f"  {i+1}/{total}: 新增{done}, 跳过{skip}")
            time.sleep(sleep_sec)

    logger.info(f"  ✓ 日线: 新增{done}只, 已有{skip}只, 共{total}只")

# ══════════════════════════════════════════════════════════════════
# Stage 4: 强赎/退市记录
# ══════════════════════════════════════════════════════════════════
def build_redemption():
    """获取强赎公告记录 — 强赎是转债退市的主因,也是幸存者偏差的主要来源。"""
    logger.info("Stage 4: 强赎/退市记录...")

    redemptions = []

    # 尝试 bond_cb_redeem_jsl — 强赎/到期记录
    # 2026-09-23 列名变更修复: jsl 接口 09-21 起返回 代码/名称/现价/.../最后交易日/
    # 到期日/强赎价/强赎状态 (旧列名 债券代码/债券简称/强赎日期/强赎价格 已失效,
    # 旧解析把 317 行空字段静默入库 → 快照退市判定失效, 与闸门十四/十六同族)。
    # 语义: redeem_date=最后交易日(该日起不可持有); 无最后交易日的行=未临近
    # 到期, 仍在交易, 不记退市。解析后 0 有效行即抛(禁止再静默覆盖)。
    try:
        redeem = ak.bond_cb_redeem_jsl()
        if not redeem.empty:
            logger.info(f"  bond_cb_redeem_jsl: {len(redeem)}条")
            parsed = 0
            for _, r in redeem.iterrows():
                code = str(r.get('代码', '')).strip()
                name = str(r.get('名称', '')).strip()
                last_d = str(r.get('最后交易日', '')).strip()
                if not last_d or last_d.lower() in ("nat", "nan", "none", ""):
                    continue  # 无最后交易日 → 未临近, 不记
                if len(code) != 6 or not code.isdigit():
                    continue
                try:
                    pd.Timestamp(last_d)
                except Exception:
                    continue
                px = r.get('强赎价', 0)
                try:
                    redeem_price = float(px) if pd.notna(px) else 0.0
                except Exception:
                    redeem_price = 0.0
                redemptions.append({
                    'code': code, 'name': name,
                    'redeem_date': last_d[:10],
                    'redeem_price': redeem_price,
                    'type': '强赎/到期',
                })
                parsed += 1
            if parsed == 0:
                raise ValueError("bond_cb_redeem_jsl 解析出 0 条有效记录"
                                 "(列名/格式疑似再变), 拒绝覆盖赎回表")
            logger.info(f"  解析有效 {parsed} 条(最后交易日已定)")
    except Exception as e:
        logger.warning(f"  bond_cb_redeem_jsl失败: {e}")

    # 日线终止的三值判定(2026-09-14, 本周第五例"缺数据被读成事件"的修正):
    # 存续 / 已退市 / 数据缺失(未知)——未知既不留在快照(防僵尸, 由快照
    # 构建的价格行检查自然排除), 也不记为退出(防假删, 2026-09-14 影子
    # 重建的 390 只"退出"多为停摆误判)。分段规则:
    #   近端(today-60 ~ today-30): 用当前存续列表交叉——"现在是否存续"
    #     是当下事实, 无前视问题(2021 年的缺口不能用 2026 的列表回答,
    #     当前列表只能回答"现在"不能回答"那时")
    #   历史(< today-60): 只用当时数据; 缺失 → 标未知, 不推断
    universe = load_meta("cb_universe")
    unknown_count = 0
    unknown_recent_count = 0
    if not universe.empty:
        from datetime import date as _date, timedelta
        _today = _date.today()
        _cut30 = (_today - timedelta(days=30)).strftime("%Y-%m-%d")
        _cut60 = (_today - timedelta(days=60)).strftime("%Y-%m-%d")
        # 2026-09-24: 未知总量被已退市无记录券占满(705/724), 恒报假警——拆出
        # "近90-60天变安静"的可疑子集(数据停摆 vs 已退市的判别正是这一窗)
        _cut90 = (_today - timedelta(days=90)).strftime("%Y-%m-%d")
        # 当前存续列表(仅用于近端交叉)
        cur_codes = set(universe["code"].astype(str).tolist())
        for _, r in universe.iterrows():
            code = str(r['code'])
            fpath = DAILY_DIR / f"{code}.parquet"
            if fpath.exists():
                df = pd.read_parquet(fpath)
                if not df.empty:
                    last_date = str(df['date'].max())[:10]
                    if last_date >= _cut30:
                        continue  # 近期活跃 → 存续
                    if any(rd['code'] == code for rd in redemptions):
                        continue  # 强赎记录已覆盖
                    if _cut90 <= last_date < _cut30:
                        # 近端(90-30天): 交叉当前存续列表——"现在不在列表"是
                        # 当下事实, 90 天窗口同样无前视问题(2026-09-24 由 60
                        # 天扩到 90: 60-90 天前退市的券因 jsl 仅回 10 行无记录
                        # 被标未知, 恒报假警; 与 30-60 窗同一 PIT 论据)
                        if code in cur_codes:
                            continue  # 仍在列表 → 数据停摆, 存续
                        redemptions.append({
                            'code': code, 'name': r.get('name', ''),
                            'redeem_date': last_date, 'redeem_price': 0,
                            'type': '已退市(近端,不在存续列表)'})
                    else:
                        # 历史段: 数据缺失 → 未知, 不推断退市
                        unknown_count += 1
                        if last_date >= _cut90:
                            unknown_recent_count += 1
    # 未知计数落盘(健康行消费, 超阈值=P0; 2026-09-14;
    # 2026-09-24 增 unknown_recent 供健康行做"数据停摆"判据)
    import json
    try:
        json.dump({"unknown_daily_gaps": unknown_count,
                   "unknown_recent": unknown_recent_count,
                   "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                  open("logs/cb_data_state.json", "w"))
    except Exception:
        pass

    if redemptions:
        df_r = pd.DataFrame(redemptions)
        save_meta("cb_redemptions", df_r)
        n_event = int((df_r['type'] == '强赎/到期').sum())
        logger.info(f"  ✓ 退市记录: {len(df_r)}条 (强赎/到期{n_event}只 + 推断{len(df_r)-n_event}只)")
    else:
        logger.info("  ⚠ 无退市记录")

# ══════════════════════════════════════════════════════════════════
# Stage 5: point-in-time 快照
# ══════════════════════════════════════════════════════════════════
def build_snapshots(start="2019-01-01", end=None, freq="MS", append=True,
                    out_name="cb_snapshots"):
    # 2026-09-21: 生产默认 append=True——周一 06:30 cron 直跑本函数,
    """
    生成每月初的存续可转债快照(point-in-time):
    - 已上市
    - 未退市(未强赎)
    - 含当日价格/转股溢价率/双低值
    append=True: 增量模式, 与旧快照合并去重而非覆盖(2026-08-31 补洞用)
    end=None → 今天(2026-09-14 修复: 原硬编码 2025-12-31 导致周更把
    快照表整体覆盖成过期版本, 库 max(date) 从 08-01 倒退到 2025-12-01)
    out_name: 输出表名(2026-09-14 影子表协议: 重建先落影子表 diff 后切换)
    """
    if end is None:
        from datetime import date as _date
        end = _date.today().strftime("%Y-%m-%d")
    logger.info(f"Stage 5: point-in-time快照 {start}→{end} → {out_name}...")

    universe = load_meta("cb_universe")
    redemptions = load_meta("cb_redemptions") if (CB_DIR.parent/"meta"/"cb_redemptions.parquet").exists() else pd.DataFrame()

    # 构建退市日期映射
    delist_map = {}
    if not redemptions.empty:
        for _, r in redemptions.iterrows():
            code = str(r['code'])
            d = str(r['redeem_date'])[:10]
            if d:
                delist_map[code] = d

    cal = load_meta("trade_calendar")
    all_dates = sorted(cal['trade_date'].tolist()) if not cal.empty else []
    snap_dates = pd.date_range(start, end, freq=freq)

    snapshots = []
    for snap_date in snap_dates:
        snap_str = snap_date.strftime("%Y-%m-%d")
        # 找最近的交易日
        valid_dates = [d for d in all_dates if d <= snap_str]
        if not valid_dates: continue
        trade_date = valid_dates[-1]

        bonds_at_date = []
        for _, r in universe.iterrows():
            code = str(r['code'])
            list_date = str(r.get('list_date', ''))[:10]
            if not list_date or list_date > snap_str:
                continue  # 未上市
            delist_d = delist_map.get(code, '2099-12-31')
            if delist_d < snap_str:
                continue  # 已退市

            # 读取当日价格
            fpath = DAILY_DIR / f"{code}.parquet"
            price = None; premium = None
            if fpath.exists():
                df = pd.read_parquet(fpath)
                df['date'] = pd.to_datetime(df['date'])
                row = df[df['date'] == trade_date]
                if not row.empty:
                    price = float(row.iloc[0]['close'])
                    premium = float(row.iloc[0].get('premium', row.iloc[0].get('转股溢价率', None)) or 0)

            if price and price > 0:
                dblow = price + 100 * (premium / 100) if premium else price
                bonds_at_date.append({
                    'code': code,
                    'name': r.get('name', ''),
                    'price': price,
                    'premium': premium,
                    'dblow': round(dblow, 2),
                    'rating': r.get('rating', ''),
                    'size': r.get('issue_size', 0),
                })

        if bonds_at_date:
            snap_df = pd.DataFrame(bonds_at_date)
            snap_df['snap_date'] = snap_str
            snapshots.append(snap_df)

        if len(snapshots) % 12 == 0:
            logger.info(f"  {snap_str}: {len(bonds_at_date)}只存续")

    if snapshots:
        all_snaps = pd.concat(snapshots, ignore_index=True)
        # 2026-09-20 修复: append 合并先于闸门——增量语义下"新∪旧≥旧"
        # 由构造保证, 单调性闸门在合并后的表上跑(旧顺序误拒合法增量)
        if append:
            old = load_meta("cb_snapshots")
            if not old.empty:
                old["snap_date"] = pd.to_datetime(old["snap_date"])
                all_snaps["snap_date"] = pd.to_datetime(all_snaps["snap_date"])
                all_snaps = (pd.concat([old, all_snaps], ignore_index=True)
                             .drop_duplicates(subset=["code", "snap_date"], keep="last"))
                logger.info(f"  ✓ 快照增量合并: 旧{len(old)}条 + 新{len(snapshots)}个月 → 共{len(all_snaps)}条")
        # 单调性闸门(2026-09-14): "库不得变旧"三件套——右端点/行数/逐月覆盖,
        # 单独右端点只守 max, 漏掉中段空洞(闸门十二精神, fail-fast)
        try:
            old_snaps = load_meta("cb_snapshots")
            old_max = (str(old_snaps["snap_date"].max())[:10]
                       if not old_snaps.empty else None)
        except Exception:
            old_snaps, old_max = None, None
        new_max = str(all_snaps["snap_date"].max())[:10]
        if old_max and new_max < old_max:
            raise ValueError(f"单调性闸门: 新快照 max={new_max} < 库内 max="
                             f"{old_max}, 拒绝写入(写库不得让数据变旧)——"
                             f"请检查 end 参数/数据源")
        if old_snaps is not None and not old_snaps.empty:
            old_n, new_n = len(old_snaps), len(all_snaps)
            # 2026-09-24 修订(用户裁定): 行数硬检查改相对阈值——09-23 实锤
            # 重拉日线清洗冻结价尾行使行数合法下降(−284/−0.8%), 硬检查误报;
            # 总行数 −2% / 单月 −5% 才拒(仍拦"中段删空")。行数下降但未超
            # 阈值时, 打印被删行明细摘要进日志+邮件(不阻断)——阈值管拒绝,
            # 摘要管可解释性。
            if new_n < old_n:
                ok_idx = old_snaps.set_index(["code", "snap_date"])
                nk_idx = all_snaps.set_index(["code", "snap_date"])
                removed = ok_idx.index.difference(nk_idx.index)
                detail = []
                for code, sd in list(removed)[:20]:
                    p_old = ok_idx.loc[(code, sd), "price"]
                    try:
                        f = pd.read_parquet(DAILY_DIR / f"{code}.parquet")
                        f = f.sort_values("date")
                        last_c = float(f["close"].iloc[-1])
                        last_d = str(f["date"].iloc[-1])[:10]
                        tail = abs(float(p_old) - last_c) < 0.01
                    except Exception:
                        last_c, last_d, tail = None, "?", False
                    detail.append(f"{code}@{str(sd.date())[:7]} 旧价{p_old:.2f}"
                                  f" 尾行={tail} (文件止{last_d})")
                rel = (old_n - new_n) / old_n
                msg = (f"快照行数下降 {old_n}→{new_n} ({rel:.1%}), "
                       f"被删 {len(removed)} 行. 明细(前20): " + " | ".join(detail))
                if rel > 0.02:
                    raise ValueError(f"单调性闸门: {msg} —— 超过2%阈值, 拒绝写入")
                logger.warning(msg)
                send_alert(f"[CB快照] 行数下降未超阈值(不阻断): {msg}",
                           level="warning")
            old_m = old_snaps["snap_date"].astype(str).str[:7].value_counts()
            new_m = all_snaps["snap_date"].astype(str).str[:7].value_counts()
            bad = [k for k in old_m.index
                   if new_m.get(k, 0) < old_m[k] * 0.95]
            if bad:
                raise ValueError(f"单调性闸门: 月度覆盖下降超5% {bad[:5]}, "
                                 f"拒绝写入(中段缺失)")
        # 第四条: 相邻月成分 churn(2026-09-14)——三条单调闸门保护"库不得
        # 变旧", 保护不了"新数据不得是坏的"(影子重建 01→09 近 100% churn
        # 三条全放行)。CB 真实月度 churn<5%, >20% 拒写; 影子表只告警
        # (影子是调查工件, 坏形态正是要落盘分析的)。
        all_snaps["snap_date"] = pd.to_datetime(all_snaps["snap_date"])
        months = sorted(all_snaps["snap_date"].dt.strftime("%Y-%m").unique())
        max_churn, max_pair = 0.0, ""
        for m1, m2 in zip(months, months[1:]):
            s1 = set(all_snaps[all_snaps["snap_date"].dt.strftime("%Y-%m") == m1]
                     ["code"].astype(str))
            s2 = set(all_snaps[all_snaps["snap_date"].dt.strftime("%Y-%m") == m2]
                     ["code"].astype(str))
            if not s1 or not s2:
                continue
            j = len(s1 & s2) / len(s1 | s2)
            if 1 - j > max_churn:
                max_churn, max_pair = 1 - j, f"{m1}→{m2}"
        if max_churn > 0.20:
            msg = (f"成分churn闸门: {max_pair} churn={max_churn:.0%} > 20%, "
                   f"疑似坏数据(停摆/错源)")
            if out_name == "cb_snapshots":
                raise ValueError(msg + ", 拒绝写入")
            logger.warning(f"[影子表放行] {msg}")

        save_meta(out_name, all_snaps)
        logger.info(f"  ✓ 快照: {len(snapshots)}个月, 共{len(all_snaps)}条记录")
        # 依赖表挂钩: CB 写库范围进 write_scopes, 与 CB 结论交集自动标需复核
        try:
            from conclusion_deps import record_scope
            record_scope("build_cb_data", ["CB"],
                         str(all_snaps["snap_date"].min())[:10], new_max,
                         "rewrite", note="CB快照重建")
        except Exception as e:
            logger.debug(f"写库范围记录失败(不阻断): {e}")

# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--stage", type=int, default=0, help="1-5, 0=all")
    args = p.parse_args()

    stages = {
        1: build_index,
        2: build_universe,
        3: build_daily,
        4: build_redemption,
        5: build_snapshots,
    }

    if args.stage == 0:
        for s in [1, 2, 3, 4, 5]:
            logger.info(f"\n{'='*60}\n  Stage {s}\n{'='*60}")
            try:
                stages[s]()
            except Exception as e:
                logger.error(f"Stage {s} 失败: {e}")
    else:
        stages[args.stage]()
