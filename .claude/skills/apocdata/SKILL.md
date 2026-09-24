---
name: apocdata
description: 天启至数 A股数据 MCP(46工具: 行情/估值/财务/股东/资金流/涨跌停/板块/公告/宏观/因子/画像)——只读参考数据源, 用于补缺与交叉验证(如公告解析/CB到期日/研报舆情辅助), 禁止直接喂入信号链。
---

# ApocData MCP 使用纪律

## 用途
只读参考数据源。典型场景: ①公告结构化查询(CB 到期/强赎/下修类, 本项目数据缺口) ②数据交叉验证(与 akshare/新浪/jsl 对照) ③题材/舆情辅助(L3 分类参考)。

## 红线(项目闸门)
- **不进信号链**: 任何要进信号/回测/配置决策的数据必须过项目闸门(独立源验证/量纲/新鲜度), 此源只能作为候选参考。
- **口径声明**: FREE 套餐行情延迟 15 分钟; 引用任何数字必须标 `ApocData` 来源。
- 数据质量怪异的默认立场=信本项目主源(新浪/akshare/jsl), 此源只作对照。

## 工具速查(46个, 主要分组)
- 行情: `quote`/`quotes`(实时快照, 延迟15min) `daily`(日K, 限30条或区间) `ranking`(横向排行)
- 财务: `financials`/`indicators`; 股东: `shareholders`/`holders-change`
- 资金流: `moneyflow`/`capital-flows`; 涨跌停: `limit-up-down`
- 公告: `announcements`/`announcement-detail`(结构化解析+摘要); 板块/宏观/因子/画像: `sectors`/`macro`/`factors`/`profile-full`

## 常见用法
- 查某券公告: `announcements(symbol="123107", limit=10)` — CB 到期/强赎公告的结构化字段
- 交叉验证价格: `quote(symbol="600519")` vs 新浪实时 — 不一致时信新浪
