# QMT 内置 Python 移植（独立目录）

> 2026-11-02 券商确认：只能用 QMT 内置 Python。
> **本目录与 `execution/` 完全隔离**；不改现有 `fetch_and_execute` /
> `qmt_client` / `trader` / `cash_sweep` / `qmt_snapshot` 的任何行为。
> 移植代码只作为 QMT 模型源码，由人工粘贴/导入 QMT 模型编辑器运行。

## 目录

- `model_p1_readonly.py`：P1 只读 spike（无 `passorder`）：
  - `init` 注册 `run_time` 定时器；
  - 定时读本地篮子 CSV（`ContextInfo.load_stk_vol_list`）；
  - 调 `get_trade_detail_data` 打印持仓数量；
  - 不做任何下单。
- 后续 P2/P3 模型另开文件；不修改现有执行链。

## 使用

1. QMT → 模型研究 → 新建 Python 模型，编码 `gbk`；
2. 粘贴 `model_p1_readonly.py`；
3. 把 `ACCOUNT_ID` 改成实盘/仿真账号；
4. 篮子文件放 `C:/quant_live/signals/`，格式
   `600000.SH,100,600004.SH,200,`（代码,数量成对）；
5. 加入“模型交易”→ 模拟，运行；观察日志。
6. P1 通过标准：读到篮子、读到持仓、定时器触发、无报错；**不下单**。
