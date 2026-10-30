# 迭代账本

每天至少 5 次真实改动。这里只记「改了什么 / 为什么 / 踩到什么」，
不记流水账，也不记「优化了一下代码」这种看不出信息的句子。

倒序排，最新的在最上面。

## 2026-10-30 · 第 1 周 · AgentLedger 起步（对标 claude-mem）

这一天的目标不是做完整产品，而是把**从零到能用的骨架**走通：
存储 → 抽取 → 向量 → MCP → HTTP/前端 → CLI。每一步都要能单独跑。

| # | 时间 | commit | 改动 |
|---|------|--------|------|
| 1 | 22:22 | `6d709a3` | 脚手架、每周调研流水线、SQLite 存储层 |
| 2 | 22:24 | `9b1d3b9` | 确定性抽取 + 本地 TF-IDF 向量 |
| 3 | 22:25 | `9d34576` | 无依赖 MCP stdio server，三层查询 |
| 4 | 22:27 | `6fd0147` | FastAPI + 零构建 web 查看器 |
| 5 | 22:40 | `待提交` | CLI（init/add/ingest/search/timeline/summarize/serve/mcp/export） |

### 1. 存储层：先把「可解释」钉死

`amem/store.py`，一张 SQLite 文件，WAL，不用 ORM。四张表：
`sessions / observations / summaries / embeddings`。

关键决定：`observations.source_ref` 从第一天就有。claude-mem 的观察是模型
压缩过的，追溯回去基本靠猜；我们留一个指向原始片段的引用，作为后面
「为什么记了这条」的答案。

踩到的坑：**FTS5 的 `unicode61` 分词器把一整段中文当一个 token**。
`MATCH '搜索'` 对「中文搜索改好了」完全不命中。试过在触发器里做拆词，
没用——分词发生在索引写入时，SQL 层改不了。
最后改成：查询里只要含 CJK，就走 Python 侧的 bigram 重叠打分
（子串命中 100 分，bigram 比例 ×60），FTS5 只服务英文。

### 2. 抽取：确定性的「翻车」比漂亮的摘要更值钱

`amem/extract.py`。规则状态机，不调模型。它盯的是会话里真正有信息量的时刻：

- 命令失败 → `bug`，importance 0.85
- 紧接着的下一条成功 → `bugfix`「修好了：…」，0.9
- 文本里出现「决定 / 取舍 / instead of」→ `decision`，0.8

`ls`/`cd`/`cat` 这种噪声命令直接跳过——不跳过的话记忆库两小时就被垃圾填满。

`LLMExtractor` 是可选的：设了 `AMEM_LLM_URL` 才会用，失败自动回退到规则，
永远不因为模型挂了就丢掉一整段会话。

### 3. 向量：本地优先不是口号，是默认值

`amem/embed.py`。默认 `HashingEmbedder`：blake2b 分桶到 512 维，
子线性 tf + 语料学到的 idf。名字叫 `local-hash-tfidf`，写进库里，
将来换模型时能一眼看出「这条向量是谁算的」。

不需要 torch、不需要下载模型、不需要网络。想要更好的语义，再开
`AMEM_EMBED_BACKEND=ollama`。

### 4. MCP：三层查询是产品设计的核心

`amem/mcp.py`，手写 JSON-RPC over stdio，没用 SDK。

- `search` → 只回 id/kind/title/120 字片段，**省 token 是第一目标**
- `timeline` → 某条观察前后发生了什么
- `get_observations` → 真的要正文时再取，一次最多 20 条

先给索引再给正文，是这类工具唯一能长期用下去的方式。
工具内部抛异常一律返回 `isError`，进程不能死——MCP server 崩了，
用户那边的 agent 会话也跟着崩。

### 5. CLI：一条命令就能验证「它到底能不能用」

`amem init / add / ingest / search / timeline / summarize / serve / mcp / export`。
全部 stdlib，除了 `serve` 需要 `[server]` extra。

顺手把 `render_markdown_summary` 从 `server.py` 挪到 `amem/summary.py`：
CLI 不该为了生成一个摘要去 import FastAPI。

### 这天踩到的两个真 bug（都写进了 commit message）

1. **SQLite 跨线程**：FastAPI 的同步端点跑在线程池里，默认 `sqlite3`
   连接不允许跨线程，于是每个请求都 `ProgrammingError`。
   改 `check_same_thread=False`——WAL + 短写 + 单进程，这个用法是安全的。
   这个 bug 只有在真正跑起 HTTP 服务时才会出现，单元测试全绿也发现不了。
2. **`save_embedder` 不建目录**：写到 `~/.agentledger/` 时目录不存在，
   直接 `FileNotFoundError`。修法是 `mkdir(parents=True)`，
   同时让 server 把索引存在**自己 db 旁边**，而不是硬编码 home 目录。

### 明天

- 用真实的 DSH 会话 JSONL 跑一遍 `ingest`，看规则抽取的精度到底怎样
- MCP server 接进真实 harness 配置，验证 `search → timeline → get` 的调用手感
- 调研脚本的 GitHub search 422（查询语法没编码）
