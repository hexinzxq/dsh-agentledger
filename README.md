# AgentLedger — 本地优先的 agent 会话记忆

> 每周调研一个市面上的 AI 项目，自己重做一遍前后端。第 1 周对标
> [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem)。

claude-mem 干的事：把 agent 会话里的工具调用做成「观察」，压缩成摘要，下次会话注入上下文；
配 SQLite + MCP 四个查询工具 + 一个 web 查看器。很完整，但它默认往云端走
（cmem.ai、observer 模型），本地要么把 key 交出去，要么掏钱。

AgentLedger 的取舍：

- **本地优先**：默认零网络。抽取规则是确定性的正则/状态机；embedding 先用本地
  TF-IDF，愿意的话再换 Ollama / OpenAI 兼容端点。
- **可解释**：每条记忆都能追到「哪次会话、第几轮、原始片段」，不给你黑盒摘要。
- **少依赖**：后端 FastAPI + SQLite，前端一个静态页面，没有 Node 构建链。

## 快速开始

不需要 Node、不需要数据库、不需要 API key。Python 3.10+ 就够。

```bash
git clone https://github.com/hexinzxq/dsh-agentledger && cd dsh-agentledger

# 1. 起库（默认 ~/.agentledger/memory.db，可用 AMEM_DB 覆盖）
python3 -m amem.cli init

# 2. 记两条，试试中文
python3 -m amem.cli add "中文搜索坏过" --kind bug \
    --body "FTS5 的 unicode61 把整段中文当一个 token"
python3 -m amem.cli add "改成 bigram 重叠打分" --kind bugfix

# 3. 搜
python3 -m amem.cli search 分词
python3 -m amem.cli timeline 2        # 看 #2 前后发生了什么
python3 -m amem.cli summarize 1
```

从会话记录导入（DSH / Claude Code 的 JSONL 都行）：

```bash
python3 -m amem.cli ingest ~/.dsh/sessions/xxx.jsonl --harness dsh
python3 -m amem.cli ingest ~/.claude/projects/xxx.jsonl --harness claude
```

Web 界面（需要额外依赖）：

```bash
pip install -e ".[server]"
python3 -m amem.cli serve            # http://127.0.0.1:8407
```

作为 MCP server 挂到 agent 上：

```bash
python3 -m amem.cli mcp
# 读 AMEM_DB，stdin/stdout 走 JSON-RPC，暴露 4 个工具：
#   search / timeline / get_observations / save_memory
```

## 几个设计上的选择

**三层查询，先给索引再给正文。** `search` 只回 id + 标题 + 120 字片段；
要细节再 `get_observations`。一次把 20 条正文塞进上下文是最贵的错。

**确定性抽取优先。** 规则抽取的结果永远一样，出了问题能 diff；
模型抽取是可选的加成（`AMEM_LLM_URL`），挂了自动退回规则。

**中文不是补丁。** 存储、检索、摘要、CLI 输出都按中文场景设计过；
CJK 查询走 bigram，不走 FTS5 那个只会切英文的分词器。

## 项目结构

```
amem/
  store.py     SQLite + FTS5 + CJK bigram 混合检索
  extract.py   规则抽取（可选 LLM 抽取）
  embed.py     本地 TF-IDF 向量，可换 Ollama/OpenAI
  summary.py   确定性摘要，零成本
  mcp.py       MCP stdio server（手写 JSON-RPC）
  server.py    FastAPI 接口
  cli.py       amem 命令行
  web/         零构建查看器（原生 HTML/CSS/JS）
scripts/lab.py 每周调研：GitHub trending / HN / Lobsters → 打分 → 候选
docs/ITERATIONS.md 迭代账本
```

## 测试

```bash
python3 -m unittest discover -s tests     # 40 个用例，零第三方依赖
```

`tests/test_server.py` 需要 `pip install -e ".[server]"`，没装会自动 skip。

## 调研

每周的候选池和打分在 [`research/reports/`](research/reports)，原始快照在
[`research/snapshots/`](research/snapshots)。2026-10-30 的结论：
claude-mem（10.5 万星）是「agent 长记忆」这个方向里最热且最可复刻的一个。

## 迭代

每天至少 5 次真实改动，账本在 [`docs/ITERATIONS.md`](docs/ITERATIONS.md)。
