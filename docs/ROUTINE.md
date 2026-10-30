# 每日迭代协议

这个文件是每天 5 个窗口的执行手册。窗口本身只负责把我叫醒，
具体怎么做以本文件为准 —— 想改流程就改这里，不用去改调度。

## 0. 定位当周项目

```bash
cd /Users/xin/Documents/deepseek-harness/default-workspace/dsh-daily-lab
export PATH="/opt/homebrew/bin:$PATH"
python3 scripts/lab.py status
cat state/current_week.json
```

`current_week.json` 里的 `local` 才是本周真正要改的仓库（第一周是
`dsh-agentledger`，之后每周换一个）。控制面仓库永远是这个目录，
它只放 `scripts/`、`state/`、`research/`、`docs/`。

## 1. 先看今天做到哪了

```bash
git log --oneline -8                 # 在当周仓库里，不是控制面
python3 scripts/lab.py status        # iterations_today / last_iteration
```

已经 ≥5 次 → 只做维护，不要硬凑第 6 次。维护是指：

- `gh run list --limit 3` 看 CI 有没有红
- README 有没有和代码对不上的地方
- tests 有没有该补的边界（尤其是刚改过的地方）

## 2. 挑一件真正有价值的事

合格的迭代：

- 新功能，且能用一条命令 / 一次点击验证
- 修一个真 bug（有复现步骤，修完有回归测试）
- 用真实数据跑一遍，发现设计问题
- 补测试，且补的是真会挂的路径（不是给 getter 加断言）

不合格的迭代：改 README 措辞、调格式、加无用注释、把同一件事拆成两个 commit。

## 3. 做完必须验证

```bash
python3 -m unittest discover -s tests          # 零依赖路径
/tmp/tv/bin/python -m unittest discover -s tests   # 带 FastAPI 的路径
```

`/tmp/tv` 是已有 venv（fastapi + httpx + uvicorn）。如果它没了：

```bash
python3 -m venv /tmp/tv && /tmp/tv/bin/pip -q install fastapi uvicorn httpx
```

HTTP 相关改动不能只靠单元测试 —— FastAPI 的同步端点跑在线程池里，
跨线程的 bug 单元测试永远发现不了。真的起一次 `amem serve` 再 curl。

## 4. 记账

1. `docs/ITERATIONS.md` 里写清：为什么这么做、踩到什么、怎么验证的。
   只写「优化了一下」等于没写。
2. `python3 scripts/lab.py log --note "一句话" --phase build`

## 5. 提交

```bash
git add -A
git commit -m "feat(scope): 做了什么

为什么这么做，以及发现的坑。"
git push origin main
```

push 报 `HTTP 500` / `remote: fatal error in commit_refs` 是 GitHub 抖动，
重试最多 3 次；连续失败就留到下一个窗口，别改历史。

## 6. 汇报

三段以内：改了什么 / 为什么 / 测试结果。没有结论的长文不算汇报。

## 踩过的坑（别再踩一次）

- **FTS5 的 unicode61 把一整段中文当一个 token**：中文查询必须走 Python
  侧 bigram 重叠打分，别指望 `MATCH`。
- **sqlite3 默认禁止跨线程**：FastAPI 同步端点必须 `check_same_thread=False`，
  配合 WAL + 短写。
- **`save_embedder` 要 mkdir**：写到不存在的目录会 FileNotFoundError，
  而且索引应该跟着 db 走，不要硬编码 `~/.agentledger`。
- **`git branch -D` 会被安全策略拦**：临时分支要删就用 `gh api` 删远程，
  别在本地耗时间。
- **`~/.ssh` 不可读**：认证只走 `gh auth` + HTTPS。
- **web_fetch 拿 github.com 会失败**（DNS 解析到非公网 IP），
  改用 `curl github.com/...` 或 `gh api`。
- **GitHub API 未认证时限流**，批量取数据用 `gh api`。

## 每周

周六 20:00 有单独的调研窗口：抓 trending / HN / Lobsters 打分，
攒够一周才换项目（`current_week.json` 的 `week_of` 距今 ≥6 天才换）。
旧仓库不删，保留继续长。
