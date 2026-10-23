"""
每日健康行 (2026-09-13 用户批准立项, dead-man's switch 极性反转) —
四次"告警未读"事故(日线挂三天/MCP从未工作/ingestion回退三天/隧道断两天)
的共同点: 系统静默被解读为健康。本脚本每天固定时刻落盘一行健康行:
  最新 bar 日期 / 各数据源最后成功时间 / 隧道状态 / 五审计结果 /
  实盘持仓快照时间。
**缺失这条消息本身 = 告警**——由 p0_alerts 链检查本文件新鲜度实现
(健康行超过 26 小时未更新 → P0 升级)。
不消耗假设预算(基础设施可靠性, 非策略假设)。
建议 cron: 每天 18:10(与 p0 sweep 同链, 先于 sweep 3 分钟):
  7 18 * * * cd /root/quant && .venv/bin/python scripts/daily_health_line.py >> logs/cron_health_line.log 2>&1
"""
import sys, json, subprocess
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from data.storage import load_daily, load_meta

OUT = Path("logs/daily_health_line.json")


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 最新 bar 日期(上证指数)
    sh = load_daily("SH000001", "2026-01-01", "2026-12-31")
    latest_bar = str(sh["date"].max())[:10] if not sh.empty else "?"

    # 各数据源最后成功时间(从日志摘要: 日更完成行)
    update_log = Path("logs/cron_data_update.log")
    last_update = "?"
    if update_log.exists():
        lines = update_log.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in reversed(lines):
            if "数据更新完成" in line:
                last_update = line[:19]
                break

    # 隧道状态(2222 端口可达性)
    import socket
    tunnel = "down"
    try:
        s = socket.create_connection(("127.0.0.1", 2222), timeout=5)
        s.close()
        tunnel = "up"
    except Exception:
        pass

    # 实盘持仓快照时间(最新 positions 文件)
    snap_time = "?"
    neg_positions = {}
    for f in sorted(Path("logs").glob("*.json")):
        if "positions_latest" in f.name:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                snap_time = str(d.get("exported_at", "?"))[:19]
                # 2026-10-21 残差审计待办2: 负持仓必须在健康行可见
                neg_positions = d.get("negative_positions") or {}
            except Exception:
                pass

    # 复权口径: 当前股票持仓的 basis (C 方案第二步; unknown/raw 不得当 qfq)
    adj_holdings, adj_bad, adj_counts = {}, [], {}
    try:
        reg = pd.read_parquet("data_store/meta/adjust_basis_registry.parquet")
        reg_map = dict(zip(reg["code"].astype(str), reg["basis"]))
        adj_counts = {str(k): int(v) for k, v in
                      reg["basis"].value_counts().items()}
        held = set()
        for g in ("g0", "g1"):
            p = Path(f"data_store/meta/signal_a_{g}.json")
            if p.exists():
                sig = json.loads(p.read_text(encoding="utf-8"))
                held |= {str(c).split(".")[0] for c in sig.get("holdings", [])}
        for c in sorted(held):
            b = reg_map.get(c, "not_in_registry")
            adj_holdings[c] = b
            if b != "qfq":
                adj_bad.append(f"{c}:{b}")
    except Exception as e:
        adj_counts = {"error": str(e)[:80]}

    # 五审计结果(单位一致性日志：标记 + 新鲜度)
    # 2026-10-21: 旧实现只看日志文本——日志在 9-15 后从未更新也报 pass/?"，
    # 属"未知≠通过"盲区；改为 2 天内 + 标记才算 pass，否则 stale/fail。
    audit = "?"
    uclog = Path("logs/unit_consistency_check.log")
    if uclog.exists():
        import time as _time
        age_d = (_time.time() - uclog.stat().st_mtime) / 86400.0
        has_ok = "单位一致性通过" in uclog.read_text(
            encoding="utf-8", errors="ignore")
        if has_ok and age_d <= 2:
            audit = "pass"
        elif has_ok:
            audit = f"stale({age_d:.0f}d)"
        else:
            audit = "fail"

    # 结论依赖追踪心跳(2026-09-13 晚): try/except 吞异常会让"追踪器死了"
    # 与"没有结论受影响"同形——心跳缺失/err 必须告警, 不等静默积累
    dep = {"dep_scan": "missing"}
    try:
        from conclusion_deps import health as cdeps_health
        dep = cdeps_health()
    except Exception as e:
        dep = {"dep_scan": "err", "last_error": f"conclusion_deps 导入失败: {e}"}

    # 单变量 harness 运行状态(2026-09-13 晚): 状态=error 才告警
    # (never_run 属正常——harness 按需运行不在日更链)
    sv = {"status": "never_run"}
    svp = Path("logs/single_var_harness_state.json")
    if svp.exists():
        try:
            sv = json.loads(svp.read_text(encoding="utf-8"))
        except Exception as e:
            sv = {"status": "error", "error": f"harness 状态不可读: {e}"}

    # bar 新鲜度: 交易日历感知(2026-09-13晚用户裁定)——分母=上一交易日,
    # 不是 26 小时。周末/节假日不产生告警
    tds = sorted({str(d)[:10] for d in sh["date"]}) if not sh.empty else []
    last_td = max((d for d in tds if d <= now[:10]), default="?")
    bar_fresh = latest_bar >= last_td if last_td != "?" else True

    # CB 持仓冻结状态(2026-09-15): "停用链"在有持仓时不是稳定状态——
    # 6 只 CB 按 09-04 信号持有、冻结调整带到期日; 过期仍未处置 → 告警
    cb_frozen = {}
    cbf = Path("logs/cb_holdings_frozen.json")
    if cbf.exists():
        try:
            cb_frozen = json.loads(cbf.read_text(encoding="utf-8"))
        except Exception:
            pass

    # 守卫拒绝计数(2026-09-15): 拦截是决定不是正常状态——688 千元股
    # 被冻结 8 天无人知晓的教训; 拒绝>0 即告警(阈值0, 每天应恰好0)
    guard_rej = 0
    grp = Path("logs/data_guard_state.json")
    if grp.exists():
        try:
            gr = json.loads(grp.read_text(encoding="utf-8"))
            if gr.get("date") == now[:10]:
                guard_rej = int(gr.get("rejected_rows", 0))
        except Exception:
            pass

    # CB 信号新鲜度(2026-09-14): "正确跳过执行"是最坏的表现形式——
    # 跳过是事件不是正常状态, 任何跳过/降级/回退必须写健康行(闸门十二)
    cb_date = "?"
    cbp = Path("data_store/meta/signal_cb_latest.json")
    if cbp.exists():
        try:
            cb_date = str(json.loads(cbp.read_text(encoding="utf-8"))
                          .get("signal_date", "?"))[:10]
        except Exception:
            cb_date = "?"

    # CB 数据侧新鲜度(2026-09-14): 与信号侧分离——数据层停摆晚一个月才
    # 被信号层"快照过期"报出(下游症状非上游原因); 每一层都要有自己的
    # 新鲜度字段。注意 stage3 对已有文件只增不更(文件冻结在首次下载),
    # 单看 max 会假健康 → 主判据=陈旧文件占比。
    # 2026-09-24 修: 分母限定为存续券(最新快照成员)——741/1051 的"陈旧"
    # 大部分是已退市券(数据合法终止在退市日), 结构性永久告警同型闸门十四;
    # 退市券不进分母后, 告警才能真正反映数据链停摆。
    cb_data_max = "?"
    cb_stale_files = 0
    cb_total_files = 0
    try:
        cb_daily_dir = Path("data_store/convertible_bonds/daily")
        active_codes = set()
        try:
            snaps_meta = load_meta("cb_snapshots")
            if not snaps_meta.empty:
                snaps_meta["snap_date"] = pd.to_datetime(snaps_meta["snap_date"])
                latest = snaps_meta["snap_date"].max()
                active_codes = set(snaps_meta[snaps_meta["snap_date"] == latest]
                                   ["code"].astype(str).tolist())
        except Exception:
            active_codes = set()
        mx = ""
        cutoff10 = (pd.Timestamp(now[:10]) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        for f in cb_daily_dir.glob("*.parquet"):
            if active_codes and f.stem not in active_codes:
                continue
            cb_total_files += 1
            try:
                d = pd.read_parquet(f, columns=["date"])
                if not d.empty:
                    v = str(d["date"].max())[:10]
                    if v > mx:
                        mx = v
                    if v < cutoff10:
                        cb_stale_files += 1
            except Exception:
                pass
        cb_data_max = mx or "?"
    except Exception:
        pass
    cb_unknown = 0
    cb_unknown_recent = 0
    cbs = Path("logs/cb_data_state.json")
    if cbs.exists():
        try:
            st = json.loads(cbs.read_text(encoding="utf-8"))
            cb_unknown = int(st.get("unknown_daily_gaps", 0))
            # 2026-09-24: 告警判据改用 recent(近90-60天变安静=可疑数据停摆);
            # 总量被已退市无记录券占满(705/724), 用作告警=永久假警
            cb_unknown_recent = int(st.get("unknown_recent", cb_unknown))
        except Exception:
            pass

    # 快照状态(2026-09-16 patch重上): "快照没拿到"成为可观测事件
    snap_state = {}
    ssp = Path("logs/qmt_snapshot_state.json")
    if ssp.exists():
        try:
            snap_state = json.loads(ssp.read_text(encoding="utf-8"))
        except Exception:
            snap_state = {"error": "状态文件不可读"}

    alerts = []
    if audit != "pass":
        alerts.append(f"单位一致性审计未通过/过期/缺失: audit={audit}")
    if neg_positions:
        alerts.append(f"负持仓伪影 {len(neg_positions)} 只: "
                      f"{list(neg_positions)[:5]} (执行层已按0隔离, "
                      f"修复窗口 2026-10-29 前)")
    if adj_bad:
        alerts.append(f"股票持仓复权口径非qfq: {adj_bad[:5]} "
                      f"(旧史raw/unknown, 信号与回测需复核; 见 "
                      f"adjust_basis_registry)")
    # bootstrap 是合法运行前的宽限态, 不告警; missing/err 才告警
    if dep.get("dep_scan") in ("missing", "err"):
        alerts.append(f"结论依赖追踪器异常: {dep.get('dep_scan')} "
                      f"{dep.get('last_error') or ''}")
    if sv.get("status") == "error":
        alerts.append(f"harness 上次运行失败: {sv.get('error', '')}")
    if now[:10] in tds and not bar_fresh:
        alerts.append(f"最新bar({latest_bar})落后于今日交易日{now[:10]}, "
                      f"日更未完成或失败")
    # CB 信号与上一交易日差 > 1 → 告警(执行链会静默跳过, 见闸门十二)
    # 2026-09-27: 日期领先日历末(=节假日幽灵信号)不算停滞——09-25 中秋
    # 实证 cb_date 领先 tds 末端被报"停滞"假警
    if cb_date != "?" and tds:
        cb_pos = [i for i, d in enumerate(tds) if d == cb_date]
        ahead = cb_date > tds[-1]
        stale = ((not cb_pos) and (not ahead)) or (
            bool(cb_pos) and (len(tds) - 1 - cb_pos[0]) > 1)
        if stale:
            alerts.append(f"CB信号停滞: signal_date={cb_date}, "
                          f"上一交易日={last_td}")
    # CB 数据侧停滞(周更链; stage3 只增不更, 主判据=陈旧文件占比>10%)
    if cb_total_files and cb_stale_files / cb_total_files > 0.10:
        alerts.append(f"CB数据停滞: {cb_stale_files}/{cb_total_files} 只文件"
                      f"陈旧(>10天), max={cb_data_max}")
    if cb_unknown_recent > 5:
        alerts.append(f"CB日线近90天可疑缺口(未知): {cb_unknown_recent} 只"
                      f"(阈值5; 未知总量{cb_unknown}只多为已退市无记录)")
    if snap_state.get("date") == now[:10] and not snap_state.get("ok"):
        alerts.append(f"今日快照 {snap_state.get('attempts', '?')} 次尝试全败, "
                      f"持仓不可观测")
    if guard_rej > 0:
        alerts.append(f"价格守卫今日拒绝 {guard_rej} 行(冻结即事件, 见闸门十四)")
    # 官方成分归档新鲜度(2026-09-16): 低频关键链最容易静默腐烂——
    # 一年只有 6/12 月两次真正重要, 平时跑不跑看不出区别(CB 链同型)
    members_asof = "?"
    try:
        mf = Path("data_store/meta/csi800_members_official.parquet")
        if mf.exists():
            md = pd.read_parquet(mf, columns=["as_of"])
            if not md.empty:
                members_asof = str(md["as_of"].iloc[0])
        if members_asof != "?" and (
                pd.Timestamp(now[:10]) - pd.Timestamp(members_asof)).days > 8:
            alerts.append(f"官方成分归档陈旧: as_of={members_asof}")
    except Exception:
        pass

    # 阈值重算事件(2026-09-16): 30天冻结与60根窗口不同步, 重算必须可见
    thev = Path("data_store/meta/price_threshold_events.jsonl")
    if thev.exists():
        try:
            big = 0
            for line in thev.read_text(encoding="utf-8").splitlines():
                if not line.strip() or not line.startswith("{"):
                    continue
                e = json.loads(line)
                if e.get("ts", "")[:10] != now[:10]:
                    continue
                o, n = e.get("old_thr"), e.get("new_thr")
                if o and n and abs(n - o) / o > 0.5:
                    big += 1
            if big:
                alerts.append(f"价格阈值重算跳变>50%: {big} 只票")
        except Exception:
            pass
    if cb_frozen.get("expiry") and cb_frozen["expiry"] < now[:10]:
        alerts.append(f"CB持仓冻结到期未处置(expiry={cb_frozen['expiry']}, "
                      f"{len(cb_frozen.get('codes', []))}只)")
    # Windows 代码同步(2026-09-17 Git-Sync 断供8天实锤后接): 状态由
    # 22:10 check_win_sync 写, 健康行读前夜; verified 不一致连续2天=告警
    # (ssh 不可达=unknown 不计, 隧道问题归 tunnel watchdog)
    win_sync = {}
    wsf = Path("logs/win_sync_state.json")
    if wsf.exists():
        try:
            win_sync = json.loads(wsf.read_text(encoding="utf-8"))
        except Exception:
            win_sync = {"error": "状态文件不可读"}
    win_consec = 0
    wcf = Path("logs/win_sync_fail_count.json")
    if wcf.exists():
        try:
            win_consec = int(json.loads(wcf.read_text(encoding="utf-8"))
                             .get("consecutive", 0))
        except Exception:
            pass
    if win_sync.get("synced") is False and win_consec >= 2:
        alerts.append(f"Windows代码同步断供连续{win_consec}天: "
                      f"win={str(win_sync.get('win_head', '?'))[:8]} "
                      f"linux={str(win_sync.get('linux_head', '?'))[:8]}")
    # live-vs-backtest 对账(2026-09-17 实装 cron 17:55 + v2 修复):
    # 每日三路 diff 结果——有事件或产物缺失/陈旧=告警
    lvbc = {}
    lf = Path("logs/live_backtest_check.json")
    if lf.exists():
        try:
            lvbc = json.loads(lf.read_text(encoding="utf-8"))
        except Exception:
            lvbc = {"error": "对账产物不可读"}
        if lvbc.get("events"):
            alerts.append(f"实盘对账事件: {'; '.join(lvbc['events'])}")
        elif (now[:10] in tds
              and str(lvbc.get("snapshot", "")) != last_td):
            # 2026-09-20 修复: 对账 cron 仅交易日跑, 周末 snapshot 停在
            # 上一交易日属正常——基准改最近交易日(原 now[:10] 周末恒误报);
            # 2026-09-27 补: 非交易日(周末/假日)整个新鲜度检查豁免——
            # 09-25 中秋假日快照被报"对账不可行"假警, 国庆长假前必修
            alerts.append(f"实盘对账陈旧/不可行: snapshot={lvbc.get('snapshot')}")
    elif now[:10] in tds:
        alerts.append("实盘对账产物缺失(17:55 cron 未跑或失败)")
    for msg in alerts:
        try:
            from monitoring.alerts import send_alert
            send_alert(msg, level="warning")
        except Exception:
            pass

    # 2026-10-10: 链隔离锁可见性(用户裁定)——锁在场必须被健康行看到,
    # 忘解锁会在无提示下停掉全部交易(含临期强制卖出)。
    # 2026-10-19 修复: 权威锁在 Windows(H:\quant\logs), 原实现只读 Linux
    # 本地文件 → 10-19 锁在 Windows 生效拦下链, 健康行却报 None(盲区)。
    # 现在: SSH 读 Windows 权威文件; 不可达时报 "不可读" 并告警(未知≠无锁)。
    manual_lock = None
    manual_lock_src = None
    win_cmd = ('if exist H:\\quant\\logs\\manual_exec_lock.json '
               '(type H:\\quant\\logs\\manual_exec_lock.json) '
               'else (echo __NO_LOCK__)')
    try:
        _r = subprocess.run(
            ["ssh", "-p", "2222", "-i", "/root/.ssh/id_rsa",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "StrictHostKeyChecking=no",
             "Administrator@127.0.0.1", win_cmd],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20)
        out = (_r.stdout or "").strip()
        if _r.returncode != 0 or not out:
            manual_lock = "不可读(SSH失败)"
            manual_lock_src = "win_ssh_fail"
        elif "__NO_LOCK__" not in out:
            try:
                import json as _j2
                manual_lock = _j2.loads(out).get("date")
            except Exception:
                manual_lock = "在场(不可读)"
            manual_lock_src = "win"
    except Exception:
        manual_lock = "不可读(SSH异常)"
        manual_lock_src = "win_ssh_fail"
    if manual_lock is None and manual_lock_src is None:
        # SSH 正常且明确无锁 → 再看本地镜像(冗余)
        lock_p = Path("logs/manual_exec_lock.json")
        if lock_p.exists():
            try:
                import json as _j2
                manual_lock = _j2.loads(lock_p.read_text(encoding="utf-8")).get("date")
                manual_lock_src = "local"
            except Exception:
                manual_lock = "在场(不可读)"
                manual_lock_src = "local"
    if manual_lock is not None:
        alerts.append(f"链隔离锁: {manual_lock} (源={manual_lock_src})"
                      + (" ⚠非当日=疑似忘解锁"
                         if str(manual_lock) not in ("不可读(SSH失败)", "不可读(SSH异常)")
                         and str(manual_lock) != now[:10] else ""))
        if str(manual_lock).startswith("不可读"):
            alerts.append("链隔离锁状态不可读(SSH失败)——未知≠无锁, 请人工核对")

    # QDII 溢价(18:30 cron): 量不到也是事件(闸门十二); 美股腿入场判据
    qdii = {}
    qp = Path("logs/qdii_premium_monitor.json")
    if qp.exists():
        try:
            qj = json.loads(qp.read_text(encoding="utf-8"))
            prem = []
            for v in qj.get("etfs", {}).values():
                x = v.get("rt_premium_pct", v.get("premium_pct"))
                if isinstance(x, (int, float)):
                    prem.append(x)
            qdii = {"data_gaps": qj.get("data_gaps", []),
                    "us_scan_err": qj.get("us_scan_err", False),
                    "watch_hits": qj.get("watch_hits", []),
                    "min_premium_pct": min(prem) if prem else None}
            if qdii["data_gaps"]:
                alerts.append(f"QDII溢价不可测: {qdii['data_gaps']} "
                              f"(美股腿入场判据缺失)")
        except Exception:
            pass

    # US 独立 track 纸面(14:45 信号/14:50 成交/18:35 mark): 状态可见
    us_track = {}
    usp = Path("logs/us_track_state.json")
    ussig = Path("data_store/meta/signal_us_latest.json")
    if usp.exists():
        try:
            st = json.loads(usp.read_text(encoding="utf-8"))
            sg = (json.loads(ussig.read_text(encoding="utf-8"))
                  if ussig.exists() else {})
            us_track = {
                "as_of": st.get("as_of"), "nav": st.get("nav"),
                "nav_high": st.get("nav_high"), "shares": st.get("shares"),
                "cash": st.get("cash"),
                "signal_date": sg.get("signal_date"),
                "gate_pass": sg.get("gate_pass"),
                "premium_pct": sg.get("premium_pct"),
                "orders_n": len(sg.get("orders") or []),
                "risk_pass": sg.get("risk_pass"),
                "dry_run": sg.get("dry_run")}
            sig_hm = str(sg.get("as_of", ""))[11:16]
            if now[:10] in tds and st.get("as_of") != now[:10]:
                alerts.append(f"US track 纸面未 mark: as_of={st.get('as_of')}")
            if now[:10] in tds:
                if sg.get("signal_date") != now[:10]:
                    alerts.append(f"US track 信号缺失: signal_date={sg.get('signal_date')}")
                elif sig_hm and sig_hm < "14:40":
                    alerts.append(f"US track 信号非14:45窗口: as_of={sg.get('as_of')}")
        except Exception:
            pass

    # qfq漂移门(P1, 08:20 cron): 新除权后源端整体重锚; 只读检测
    qfq_drift = {}
    qdp = sorted(Path("logs").glob("qfq_drift_check_*.json"))
    if qdp:
        try:
            qj = json.loads(qdp[-1].read_text(encoding="utf-8"))
            flags = qj.get("flags") or []
            qfq_drift = {"as_of": qj.get("ts"),
                         "n_checked": qj.get("n_checked"),
                         "pool_n": qj.get("pool_n"),
                         "holdings_n": qj.get("holdings_n"),
                         "flags": flags[:10], "n_flags": len(flags)}
            if flags:
                alerts.append(f"qfq漂移候选 {len(flags)} 只: {flags[:5]} "
                              f"(新除权重锚? 核对公告后重锚)")
        except Exception:
            pass

    # 100万+5万CB卫星纸面账本(2026-10-23): 读最新行; 缺失/读失败必须告警(闸门十二)
    satellite_100w5w = {"error": "missing"}
    sp = Path("logs/satellite_cb_paper.csv")
    if sp.exists():
        try:
            sd = pd.read_csv(sp)
            sl = sd.iloc[-1]
            satellite_100w5w = {
                "date": str(sl["date"]),
                "combined_nav": float(sl.get("combined_nav_100w5w")),
                "sat_ret": float(sl.get("sat_ret_100w5w")),
            }
        except Exception as e:  # noqa: BLE001
            satellite_100w5w = {"error": f"read_fail: {str(e)[:60]}"}
    if satellite_100w5w.get("error"):
        alerts.append(f"卫星100w5w账本异常: {satellite_100w5w['error']}")
    elif last_td != "?" and satellite_100w5w.get("date", "") < last_td:
        alerts.append(f"卫星100w5w账本停滞: 最新{satellite_100w5w['date']} "
                      f"< 上一交易日{last_td} (18:25 cron?)")

    # CB趋势过滤前向纸面(2026-10-21起): 账本停滞=纸面实验静默死亡(闸门十二)
    cb_overlay_paper = {"error": "missing"}
    cop = Path("logs/cb_overlay_paper.csv")
    if cop.exists():
        try:
            od = pd.read_csv(cop)
            ol = od.iloc[-1]
            cb_overlay_paper = {
                "date": str(ol["date"]),
                "signal_prev": float(ol["signal_prev"]),
                "signal_now": int(ol["signal_now"]),
                "paper_nav": float(ol["paper_nav"]),
                "n_days": int(len(od)),
            }
        except Exception as e:  # noqa: BLE001
            cb_overlay_paper = {"error": f"read_fail: {str(e)[:60]}"}
    if cb_overlay_paper.get("error"):
        alerts.append(f"CB趋势过滤纸面账本异常: {cb_overlay_paper['error']}")
    elif last_td != "?" and cb_overlay_paper["date"] < last_td:
        alerts.append(f"CB趋势过滤纸面账本停滞: 最新{cb_overlay_paper['date']} "
                      f"< 上一交易日{last_td}")

    # 每日研究轨道: 状态预测未评分/已评分, 方向台账Brier——18:20/18:25 cron
    # 静默失败时这里必须可见(研究轨道不进生产决策, 但不许无声停摆)
    research_track = {}
    try:
        sfp = Path("logs/state_forecast_ledger.csv")
        if sfp.exists():
            sd = pd.read_csv(sfp)
            valid_rows = (sd[sd["valid"].astype(str) == "ok"]
                          if "valid" in sd.columns else sd)
            scored = valid_rows[valid_rows["outcome_event"].notna()]
            latest_md = str(sd["market_date"].max())
            research_track["state_forecast"] = {
                "latest_market_date": latest_md,
                "n_pending": int(len(valid_rows) - len(scored)),
                "n_scored": int(len(scored)),
                "n_invalid": int(len(sd) - len(valid_rows)),
            }
            if last_td != "?" and latest_md < last_td:
                alerts.append(f"状态预测台账停滞: 最新{latest_md} "
                              f"< 上一交易日{last_td} (18:20 cron?)")
        else:
            research_track["state_forecast"] = {"error": "missing"}
    except Exception as e:  # noqa: BLE001
        research_track["state_forecast"] = {"error": str(e)[:60]}
    try:
        psp = Path("logs/prediction_score.json")
        if psp.exists():
            pj = json.loads(psp.read_text(encoding="utf-8"))
            research_track["direction"] = {
                "n_targets": pj.get("n_targets"),
                "brier": pj.get("brier"),
                "brier_base_rate": pj.get("brier_base_rate"),
                "hit_rate": pj.get("hit_rate"),
                "computed_at": pj.get("computed_at"),
            }
            dts = str(pj.get("computed_at") or "")[:10]
            if last_td != "?" and dts and dts < last_td:
                alerts.append(f"方向预测机评停滞: 最新{dts} "
                              f"< 上一交易日{last_td} (18:25 cron?)")
    except Exception as e:  # noqa: BLE001
        research_track["direction"] = {"error": str(e)[:60]}

    # 方向台账覆盖(2026-10-23): 下一交易日必须有登记(auto-base 18:25)
    direction_coverage = {"error": "missing"}
    try:
        cal = load_meta("trade_calendar")
        ds = (sorted(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d")
                     .unique()) if cal is not None and not cal.empty else [])
        nxt = next((d for d in ds if d > now[:10]), None)
        ledp = Path("logs/prediction_ledger.csv")
        if nxt and ledp.exists():
            led = pd.read_csv(ledp)
            latest_md = str(led["market_date"].astype(str).max())
            direction_coverage = {"next_trade_day": nxt,
                                  "latest_market_date": latest_md,
                                  "ok": latest_md >= nxt}
            if latest_md < nxt:
                alerts.append(f"方向台账缺下一交易日登记: latest {latest_md} "
                              f"< {nxt} (18:25 auto-base?)")
        else:
            direction_coverage = {"error": "no calendar/ledger"}
    except Exception as e:  # noqa: BLE001
        direction_coverage = {"error": str(e)[:60]}
    if direction_coverage.get("error"):
        alerts.append(f"方向台账覆盖检查异常: {direction_coverage['error']}")

    # tick采集新鲜度(Windows 15:10; Linux 18:20检查): 缺失/检查未跑=事件
    tick_freshness = {"error": "missing"}
    tfp = Path("logs/tick_state.json")
    if tfp.exists():
        try:
            tj = json.loads(tfp.read_text(encoding="utf-8"))
            tick_freshness = {k: tj.get(k) for k in
                              ("as_of", "last_trade_day", "latest_file",
                               "n_files", "ok", "error", "retried",
                               "retry_ok")}
            if tj.get("ok") is False:
                alerts.append(f"tick采集缺失: 最新{tj.get('latest_file')} "
                              f"< 上一交易日{tj.get('last_trade_day')} "
                              f"(Windows 15:10任务; 自动重试={tj.get('retry_ok')})")
            if last_td != "?" and str(tj.get("as_of", ""))[:10] < last_td:
                alerts.append(f"tick新鲜度检查未运行: {tj.get('as_of')} "
                              f"< 上一交易日{last_td}")
        except Exception as e:  # noqa: BLE001
            tick_freshness = {"error": f"read_fail: {str(e)[:60]}"}
    if tick_freshness.get("error"):
        alerts.append(f"tick新鲜度状态异常: {tick_freshness['error']}")

    # CB月度调仓验证器状态(2026-10-23): 15:40 每日跑, 跳过也落状态——
    # "非调仓日→跳过"若不进健康行，验证器静默死亡与正常跳过不可区分(闸门十二)
    cb_monthly_verify = {"error": "missing"}
    vsp = Path("logs/cb_monthly_verify_state.json")
    if vsp.exists():
        try:
            vj = json.loads(vsp.read_text(encoding="utf-8"))
            cb_monthly_verify = {k: vj.get(k) for k in
                                 ("date", "status", "expect_snap", "expect_day",
                                  "next_expect_day", "rebalance", "n_snap",
                                  "n_cb", "fails", "warns")}
            vd = str(vj.get("date", ""))
            if last_td != "?" and vd < last_td:
                alerts.append(f"CB月度验证器未运行: 状态{vd} "
                              f"< 上一交易日{last_td}")
            if vj.get("status") in ("fail", "selftest_fail", "no_calendar"):
                alerts.append(f"CB月度验证器异常: status={vj.get('status')} "
                              f"{vj.get('fails') or vd}")
        except Exception as e:  # noqa: BLE001
            cb_monthly_verify = {"error": f"read_fail: {str(e)[:60]}"}
    if cb_monthly_verify.get("error"):
        alerts.append(f"CB月度验证器状态异常: {cb_monthly_verify['error']}")

    # H1 regime 前向纸面(月度, 1日08:00): 只读测量; 月度cron静默失败必须可见
    regime_forward_paper = {"error": "missing"}
    rfp = Path("logs/regime_forward_paper_state.json")
    if rfp.exists():
        try:
            rj = json.loads(rfp.read_text(encoding="utf-8"))
            regime_forward_paper = {k: rj.get(k) for k in
                                    ("as_of", "last_complete_month",
                                     "latest_recorded", "n_rows", "n_scored",
                                     "ok")}
            if rj.get("ok") is False:
                alerts.append(f"regime前向纸面未记录最新完整月: "
                              f"{rj.get('latest_recorded')} < "
                              f"{rj.get('last_complete_month')}")
        except Exception as e:  # noqa: BLE001
            regime_forward_paper = {"error": f"read_fail: {str(e)[:60]}"}
    if regime_forward_paper.get("error"):
        alerts.append(f"regime前向纸面状态异常: {regime_forward_paper['error']}")

    # cron 安装一致性(2026-10-23): deploy 文件更新≠已安装——18:20 状态预测
    # cron 在 deploy 里躺了一天但实机 root crontab 没有(批准不落地同型)。
    cron_install = {"error": "missing"}
    try:
        dep_files = sorted(Path("deploy").glob("cron*_quant_*.txt"))
        inst = Path("/var/spool/cron/crontabs/root")
        if dep_files and inst.exists():
            def _cron_lines(p):
                return {ln.strip() for ln in
                        p.read_text(encoding="utf-8", errors="ignore").splitlines()
                        if ln.strip() and not ln.strip().startswith("#")}
            d_lines, i_lines = _cron_lines(dep_files[-1]), _cron_lines(inst)
            missing, extra = sorted(d_lines - i_lines), sorted(i_lines - d_lines)
            cron_install = {"deploy": dep_files[-1].name, "n_deploy": len(d_lines),
                            "n_installed": len(i_lines),
                            "n_missing": len(missing), "n_extra": len(extra),
                            "missing": [m[:120] for m in missing[:5]],
                            "extra": [m[:120] for m in extra[:5]]}
            if missing:
                alerts.append(f"cron未安装 {len(missing)} 行"
                              f"(deploy有/实机无): {missing[0][:90]}")
            if extra:
                alerts.append(f"cron多出 {len(extra)} 行"
                              f"(实机有/deploy无): {extra[0][:90]}")
        else:
            cron_install = {"error": "deploy 文件或 root crontab 缺失"}
    except Exception as e:  # noqa: BLE001
        cron_install = {"error": str(e)[:80]}
    if cron_install.get("error"):
        alerts.append(f"cron安装检查异常: {cron_install['error']}")

    line = {"ts": now, "latest_bar": latest_bar,
            "last_trading_day": last_td, "bar_fresh": bar_fresh,
            "last_data_update": last_update, "tunnel": tunnel,
            "audit": audit, "positions_snapshot": snap_time,
            "negative_positions": neg_positions,
            "adjust_basis_holdings": adj_holdings,
            "adjust_basis_registry_counts": adj_counts,
            "cb_signal_date": cb_date, "cb_data_max_date": cb_data_max,
            "cb_unknown_gaps": cb_unknown, "cb_unknown_recent": cb_unknown_recent,
            "guard_rejected_rows": guard_rej, "snapshot_state": snap_state,
            "cb_holdings_frozen": cb_frozen, "index_members_as_of": members_asof,
            "win_sync": win_sync, "live_reconcile": lvbc,
            "manual_exec_lock": manual_lock,
            "qdii_premium": qdii, "us_track": us_track,
            "qfq_drift": qfq_drift,
            "satellite_100w5w": satellite_100w5w,
            "cb_overlay_paper": cb_overlay_paper,
            "research_track": research_track,
            "direction_coverage": direction_coverage,
            "regime_forward_paper": regime_forward_paper,
            "tick_freshness": tick_freshness,
            "cb_monthly_verify": cb_monthly_verify,
            "cron_install": cron_install,
            "dep_scan": dep, "sv_harness": sv, "alerts": alerts}
    OUT.write_text(json.dumps(line, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(line, ensure_ascii=False))


if __name__ == "__main__":
    main()
