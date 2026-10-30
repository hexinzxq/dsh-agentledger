"""Command line entry point: ``amem <command>``.

Everything here is stdlib-only except ``serve`` (FastAPI extra) and ``ingest
--llm`` (network). The point of the CLI is that a fresh clone works with a
bare python3: add a memory, search it, summarize it, done.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .embed import embedder_from_env, load_embedder, reindex, save_embedder, semantic_search
from .extract import (RuleExtractor, adapt_claude_transcript, adapt_dsh_transcript,
                      extractor_from_env)
from .store import Observation, Store
from .summary import render_markdown_summary


def default_db() -> str:
    return os.environ.get("AMEM_DB", os.path.expanduser("~/.agentledger/memory.db"))


def _store(args) -> tuple[Store, Path]:
    db = Path(args.db or default_db()).expanduser()
    return Store(db), db.parent


def _print_hits(hits: list[dict]) -> None:
    if not hits:
        print("没搜到。试试更短的关键词，或者 amem recent 看看有什么。")
        return
    for h in hits:
        score = h.get("score") or h.get("rank") or ""
        print(f"#{h['id']:<4} [{h['kind']:<9}] {h['title']}")
        body = (h.get("body") or "").strip().replace("\n", " ")
        if body:
            print(f"        {body[:120]}{'…' if len(body) > 120 else ''}")
        if score != "":
            print(f"        score {score}")


# -- commands -----------------------------------------------------------
def cmd_init(args) -> int:
    store, data_dir = _store(args)
    n = store.stats()
    print(f"库文件: {store.path}")
    print(f"会话 {n['sessions']} · 观察 {n['observations']} · 摘要 {n['summaries']} · "
          f"FTS5 {'开' if n['fts'] else '关（降级 LIKE）'}")
    store.close()
    return 0


def cmd_add(args) -> int:
    store, data_dir = _store(args)
    sid = args.session or store.start_session("manual", args.project, os.getcwd())
    tags = [t.strip() for t in (args.tag or "").split(",") if t.strip()]
    oid = store.add_observation(sid, Observation(
        kind=args.kind, title=args.title, body=args.body or "",
        tags=tags, source_ref=args.source or "cli", importance=args.importance,
    ))
    if not args.session:
        store.end_session(sid)
    print(f"记下了 #{oid}（session {sid}）")
    store.close()
    return 0


def cmd_ingest(args) -> int:
    path = Path(args.transcript).expanduser()
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2
    events = adapt_claude_transcript(path) if args.harness == "claude" else adapt_dsh_transcript(path)
    if not events:
        print("没解析出事件，检查一下 --harness 是不是选错了", file=sys.stderr)
        return 1
    store, data_dir = _store(args)
    extractor = extractor_from_env() if args.llm else RuleExtractor()
    obs = extractor.extract(events)
    sid = store.start_session(args.harness, args.project or str(path.parent), str(path.parent))
    ids = store.add_many(sid, obs)
    store.end_session(sid)
    n = reindex(store, embedder_from_env())
    save_embedder(embedder_from_env(), data_dir)
    print(f"session {sid}: {len(events)} 事件 -> {len(ids)} 条观察（{n} 条向量）")
    for oid in ids[:8]:
        o = store.get_observation(oid)
        print(f"  #{oid} [{o['kind']}] {o['title']}")
    store.close()
    return 0


def cmd_search(args) -> int:
    store, data_dir = _store(args)
    if args.semantic:
        emb = load_embedder(data_dir)
        hits = semantic_search(store, args.query, args.limit, emb)
    else:
        hits = store.search(args.query, args.limit)
    _print_hits(hits)
    store.close()
    return 0


def cmd_recent(args) -> int:
    store, data_dir = _store(args)
    _print_hits(store.recent(args.limit, args.kind))
    store.close()
    return 0


def cmd_timeline(args) -> int:
    store, data_dir = _store(args)
    if not store.get_observation(args.id):
        print(f"没有 #{args.id}", file=sys.stderr)
        return 1
    for o in store.timeline(args.id, args.before, args.after):
        mark = "→" if o["id"] == args.id else " "
        print(f"{mark} #{o['id']:<4} [{o['kind']:<9}] {o['title']}")
    store.close()
    return 0


def cmd_summarize(args) -> int:
    store, data_dir = _store(args)
    if not store.get_session(args.session):
        print(f"没有 session {args.session}", file=sys.stderr)
        return 1
    obs = store.observations_for_session(args.session)
    if not obs:
        print("这个会话没有观察", file=sys.stderr)
        return 1
    text = render_markdown_summary(obs)
    store.set_summary(args.session, text, obs_count=len(obs))
    print(text)
    store.close()
    return 0


def cmd_serve(args) -> int:
    from .server import create_app

    import uvicorn

    host = args.host or os.environ.get("AMEM_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("AMEM_PORT", "8407"))
    app = create_app(args.db or default_db())
    print(f"http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def cmd_mcp(args) -> int:
    from .mcp import serve

    os.environ["AMEM_DB"] = str(Path(args.db or default_db()).expanduser())
    serve(os.environ["AMEM_DB"])
    return 0


def cmd_export(args) -> int:
    store, data_dir = _store(args)
    out = {"version": __version__, "sessions": store.list_sessions(1000),
           "summaries": store.summaries(1000), "observations": []}
    for s in out["sessions"]:
        out["observations"].extend(store.observations_for_session(s["id"]))
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(f"写到 {args.out}")
    else:
        print(text)
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="amem", description="本地会话记忆 · 无云 · 可解释")
    p.add_argument("--db", help="SQLite 路径（默认 AMEM_DB 或 ~/.agentledger/memory.db）")
    p.add_argument("--version", action="version", version=f"agentledger {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="建库并打印状态")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("add", help="手动记一条观察")
    s.add_argument("title")
    s.add_argument("--body", default="")
    s.add_argument("--kind", default="fact",
                   choices=["fact", "decision", "bug", "bugfix", "discovery", "todo", "note"])
    s.add_argument("--tag", default="")
    s.add_argument("--source", default="")
    s.add_argument("--project", default="")
    s.add_argument("--session", type=int, default=0)
    s.add_argument("--importance", type=float, default=0.5)
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("ingest", help="从会话 JSONL 抽取观察")
    s.add_argument("transcript")
    s.add_argument("--harness", default="dsh", choices=["dsh", "claude"])
    s.add_argument("--project", default="")
    s.add_argument("--llm", action="store_true", help="用 AMEM_LLM_URL 的模型抽取")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("search", help="混合检索")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.add_argument("--semantic", action="store_true", help="只用向量")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("recent", help="最近的观察")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.add_argument("--kind")
    s.set_defaults(fn=cmd_recent)

    s = sub.add_parser("timeline", help="某条观察前后发生了什么")
    s.add_argument("id", type=int)
    s.add_argument("--before", type=int, default=3)
    s.add_argument("--after", type=int, default=3)
    s.set_defaults(fn=cmd_timeline)

    s = sub.add_parser("summarize", help="生成确定性摘要")
    s.add_argument("session", type=int)
    s.set_defaults(fn=cmd_summarize)

    s = sub.add_parser("serve", help="起 web 界面（需要 [server] 额外依赖）")
    s.add_argument("--host", default="")
    s.add_argument("--port", type=int, default=0)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("mcp", help="以 MCP stdio server 运行")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("export", help="导出全部记忆为 JSON")
    s.add_argument("--out", default="")
    s.set_defaults(fn=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
