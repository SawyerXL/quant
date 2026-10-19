// CB 下修公告日全量摄取 (2026-10-19, 事件研究数据层)
// 为什么: MCP G2 的 publish_date 是"生效公告日", 真正的信息日=董事会提议公告日
// (约早两周)。事件研究必须用后者, 否则测的是已被定价两周后的残值。
// 用法: node scripts/fetch_cb_revision_announcements.mjs <symbols.json> <out.jsonl>
import { readFileSync, appendFileSync, existsSync, unlinkSync } from "fs";
const SDK = "/root/quant/.claude/mcp/node_modules/@modelcontextprotocol/sdk/dist/esm";
const { Client } = await import(`${SDK}/client/index.js`);
const { StdioClientTransport } = await import(`${SDK}/client/stdio.js`);

const [symbolsFile, outFile] = process.argv.slice(2);
const symbols = JSON.parse(readFileSync(symbolsFile, "utf8"));
if (existsSync(outFile)) unlinkSync(outFile);

const client = new Client({ name: "quant-ann", version: "1.0.0" });
const transport = new StdioClientTransport({
  command: "node",
  args: ["/root/quant/.claude/mcp/node_modules/@apocdata-info/mcp-server/dist/index.js"],
});
await client.connect(transport);

let ok = 0, empty = 0, fail = 0;
for (let i = 0; i < symbols.length; i++) {
  try {
    const res = await client.callTool({
      name: "announcements",
      arguments: {
        symbol: symbols[i], startDate: "20190101", endDate: "20261019",
        q: "向下修正", includeContent: false,
        fields: "title,ann_date", limit: 30,
      },
    });
    const data = res?.content?.find((c) => c.type === "text")?.text;
    if (!data) { empty++; continue; }
    const jsonPart = data.replace(/^<!--[\s\S]*?-->\s*/, "");
    let parsed;
    try { parsed = JSON.parse(jsonPart); } catch { parsed = { raw: jsonPart }; }
    const rows = parsed?.data ?? [];
    appendFileSync(outFile, JSON.stringify({ symbol: symbols[i], rows }) + "\n");
    if (Array.isArray(rows) && rows.length === 0) empty++;
    else ok++;
  } catch (e) {
    fail++;
  }
  if ((i + 1) % 50 === 0) console.error(`进度 ${i + 1}/${symbols.length}: 有数据${ok} 空${empty} 失败${fail}`);
  await new Promise((r) => setTimeout(r, 120));   // 限流保护
}
console.error(`完成: 有数据${ok} 空${empty} 失败${fail}`);
await client.close();
