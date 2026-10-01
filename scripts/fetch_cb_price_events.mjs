// CB 转股价变动事件全量摄取 (2026-10-01, 下修标记 PIT 框架数据层)
// 通过本地 apocdata MCP server (stdio) 逐券拉 cb-price-chg, 输出 JSONL。
// 用法: node scripts/fetch_cb_price_events.mjs <codes.json> <out.jsonl>
import { spawn } from "child_process";
import { readFileSync, writeFileSync, appendFileSync, existsSync, unlinkSync } from "fs";
// 脚本位于 scripts/, node 按脚本位置解析裸说明符——用绝对路径指到 MCP 安装目录
const SDK = "/root/quant/.claude/mcp/node_modules/@modelcontextprotocol/sdk/dist/esm";
const { Client } = await import(`${SDK}/client/index.js`);
const { StdioClientTransport } = await import(`${SDK}/client/stdio.js`);

const [codesFile, outFile] = process.argv.slice(2);
const codes = JSON.parse(readFileSync(codesFile, "utf8"));
if (existsSync(outFile)) unlinkSync(outFile);

const client = new Client({ name: "quant-bulk", version: "1.0.0" });
const transport = new StdioClientTransport({
  command: "node",
  args: ["/root/quant/.claude/mcp/node_modules/@apocdata-info/mcp-server/dist/index.js"],
});
await client.connect(transport);

let ok = 0, empty = 0, fail = 0;
for (let i = 0; i < codes.length; i++) {
  const ts = codes[i];
  try {
    const res = await client.callTool({ name: "cb-price-chg", arguments: { tsCode: ts, limit: 20 } });
    const data = res?.content?.find((c) => c.type === "text")?.text;
    if (!data) { empty++; continue; }
    // 响应文本带 <!-- meta ... --> 前置注释, 剥掉再 parse
    const jsonPart = data.replace(/^<!--[\s\S]*?-->\s*/, "");
    let parsed;
    try { parsed = JSON.parse(jsonPart); } catch { parsed = { raw: jsonPart }; }
    const rows = parsed?.data ?? [];
    if (Array.isArray(rows) && rows.length === 0) { empty++; continue; }
    appendFileSync(outFile, JSON.stringify({ ts_code: ts, rows }) + "\n");
    ok++;
  } catch (e) {
    fail++;
  }
  if ((i + 1) % 100 === 0) console.error(`进度 ${i + 1}/${codes.length}: 有数据${ok} 空${empty} 失败${fail}`);
}
console.error(`完成: 有数据${ok} 空${empty} 失败${fail}`);
await client.close();
