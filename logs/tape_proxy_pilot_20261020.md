# 盘口 tick 采集/分析管线试跑记录（2026-10-20 14:58，机器值）

- 采集：Windows `collect_tick_daily.py` 手动试跑（部分交易日，至 14:58）；
  **199/200 只**有数据，**942,821 条** tick，32 个字段（五档价量、lastPrice、
  lastClose/open/high/low、tickvol、transactionNum 等），0 采集错误。
- 1 只无 tick（停牌/无行情）；09:15 集合竞价段 lastPrice=0 已在分析中过滤。
- 分析：`scripts/tape_proxy_study.py`（预注册 N=65 的代理定义）。
  - 当日事件 **3,090** 个：ask_executed 443 / ask_vanished 13 / ask_other 1068；
    bid_executed 454 / bid_vanished 16 / bid_other 1096。
  - 30 分钟横截面超额（描述，**不是结论**）：ask_executed +0.367pp、
    bid_executed +0.093pp；vanished 组仅 13/16 个，样本不足以读。
  - aggressor imbalance 十分位：9,489 个 5 分钟观测，描述统计已生成。
- 判读：管线打通；`vanished` 代理偏稀疏（约 29 个/日），40 个交易日约 1,160 个，
  刚过每组 300 的门槛；**不修改定义**（改定义=新预注册）。
- 正式采集从 **2026-10-21** 起；15:10 定时任务每日覆盖当日全量 tick；
  40 个交易日后（约 12 月中）按预注册判读。本文件只证明“能采、能算”，不构成任何效应结论。

## 15:10 定时任务全量试跑（机器值）

- 15:10 任务自动执行完成：**199/200 只、950,053 行、0 错误**，文件
  `H:\quant\data_store\intraday\tick\20261020.parquet`（39,399,101 字节），
  覆盖全交易日（手动试跑截至 14:58 为 942,821 行）。
- 结论：采集任务端到端可用；正式样本从 10-21 起累计。
