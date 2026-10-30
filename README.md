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

## 现在能跑什么

```
python3 scripts/lab.py research     # 抓 GitHub trending / HN / Lobsters，打分出报告
python3 -m amem --help              # 待补：记忆服务 CLI
```

## 调研

每周的候选池和打分在 [`research/reports/`](research/reports)，原始快照在
[`research/snapshots/`](research/snapshots)。2026-10-30 的结论：
claude-mem（10.5 万星）是「agent 长记忆」这个方向里最热且最可复刻的一个。

## 迭代

每天至少 5 次真实改动，账本在 [`docs/ITERATIONS.md`](docs/ITERATIONS.md)。
