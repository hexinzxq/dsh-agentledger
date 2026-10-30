#!/usr/bin/env python3
"""dsh-daily-lab control plane.

One file, stdlib only (plus optional httpx). It is the deterministic half of the
weekly loop:

  research  pull public trend sources -> dedupe -> score -> report + candidates.json
  pick      promote one candidate to be the week's project
  status    show where the week is (read by the agent and by humans)
  log       append one iteration entry to the ledger

Everything the LLM agent does that is NOT deterministic lives in the repo as
markdown + a few JSON files, so the next run can pick up where the last left off.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import textwrap
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
RESEARCH = ROOT / "research"
SNAP = RESEARCH / "snapshots"
REPORTS = RESEARCH / "reports"
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai, no tz database needed on-slim pythons

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) dsh-daily-lab/1.0"

AI_KEYWORDS = [
    "ai", "llm", "agent", "agents", "gpt", "claude", "mcp", "rag", "embedding",
    "inference", "transformer", "diffusion", "vision", "multimodal", "prompt",
    "copilot", "assistant", "chatbot", "ollama", "vllm", "langchain", "vector",
    "fine-tune", "finetune", "reasoning", "tool-use", "autonomous",
]
# things that make a repo a bad weekly rebuild target
HARD_REJECT = [
    "awesome-", "curated list", "cheat sheet", "roadmap", "interview",
    "tutorial", "course", "book", "paper list", "papers", "newsletter",
]


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", file=sys.stderr)


def http_get(url: str, timeout: int = 25, headers: dict[str, str] | None = None) -> str:
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*", **(headers or {})})
    with urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed public hosts
        return r.read().decode("utf-8", "replace")


def http_json(url: str, timeout: int = 25):
    return json.loads(http_get(url, timeout=timeout, headers={"Accept": "application/json"}))


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #

def src_github_trending(since: str = "weekly") -> list[dict[str, Any]]:
    """Scrape github.com/trending. Returns one record per repo."""
    out = []
    page = http_get(f"https://github.com/trending?since={since}")
    for art in re.findall(r'<article class="Box-row">(.*?)</article>', page, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+)"', art)
        if not m:
            continue
        full = m.group(1).strip()
        if full.count("/") != 1:
            continue
        desc = re.search(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', art, re.S)
        desc = html.unescape(re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else ""
        stars_total = _num(re.search(r'([\d,]+)\s*</a>\s*</span>', art))
        gained = _num(re.search(r'([\d,]+)\s+stars\s+(?:today|this week)', art))
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', art)
        out.append({
            "source": f"github-trending-{since}",
            "repo": full,
            "url": f"https://github.com/{full}",
            "desc": desc,
            "lang": html.unescape(lang.group(1)).strip() if lang else "",
            "stars": stars_total,
            "gained": gained,
        })
    return out


def _num(m) -> int:
    if not m:
        return 0
    try:
        return int(str(m.group(1)).replace(",", ""))
    except (ValueError, IndexError):
        return 0


def src_hackernews(limit: int = 60) -> list[dict[str, Any]]:
    """Top HN stories; only ones whose title smells like AI/tools."""
    ids = http_json("https://hacker-news.firebaseio.com/v0/topstories.json")[:limit]
    out = []
    for sid in ids:
        try:
            it = http_json(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=10)
        except Exception:  # noqa: BLE001 - a dead item must not kill the run
            continue
        if not it or it.get("type") != "story":
            continue
        title = it.get("title", "")
        if not _is_ai(title + " " + it.get("text", "")):
            continue
        url = it.get("url") or f"https://news.ycombinator.com/item?id={sid}"
        out.append({
            "source": "hackernews",
            "repo": "",
            "url": url,
            "title": title,
            "desc": re.sub(r"<[^>]+>", "", it.get("text", "") or "")[:400],
            "points": it.get("score", 0),
            "comments": it.get("descendants", 0),
            "hn_id": sid,
        })
    return out


def src_lobsters() -> list[dict[str, Any]]:
    try:
        raw = http_get("https://lobste.rs/hottest.json")
    except Exception as e:  # noqa: BLE001
        log(f"lobsters skipped: {e}")
        return []
    out = []
    for it in json.loads(raw)[:40]:
        t = it.get("title", "")
        if not _is_ai(t + " " + (it.get("description") or "")):
            continue
        out.append({
            "source": "lobsters",
            "repo": "",
            "url": it.get("url") or it.get("comments_url", ""),
            "title": t,
            "desc": (it.get("description") or "")[:400],
            "points": it.get("score", 0),
            "comments": it.get("comment_count", 0),
        })
    return out


def src_github_search(days: int = 7) -> list[dict[str, Any]]:
    """GitHub search API: repos created in the last N days, sorted by stars.
    Unauthenticated is heavily rate limited, so this is best-effort."""
    since = (today() - timedelta(days=days)).isoformat()
    q = f"created:>{since} stars:>50 topic:ai OR topic:llm OR topic:agents"
    url = f"https://api.github.com/search/repositories?q={q.replace(' ', '+')}&sort=stars&per_page=25"
    try:
        data = http_json(url)
    except Exception as e:  # noqa: BLE001
        log(f"github search skipped: {e}")
        return []
    out = []
    for it in data.get("items", []):
        out.append({
            "source": "github-search-new",
            "repo": it.get("full_name", ""),
            "url": it.get("html_url", ""),
            "desc": it.get("description") or "",
            "lang": it.get("language") or "",
            "stars": it.get("stargazers_count", 0),
            "gained": it.get("stargazers_count", 0),
            "topics": it.get("topics", []),
        })
    return out


def _is_ai(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in AI_KEYWORDS)


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #

def score_item(it: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(it.get(k, "")) for k in ("repo", "title", "desc")).lower()
    s = 0.0
    gain = it.get("gained") or 0
    total = it.get("stars") or 0
    pts = it.get("points") or 0
    comments = it.get("comments") or 0

    s += min(60.0, gain / 4.0)                 # star velocity dominates
    s += min(15.0, total / 4000.0)             # a bit of accumulated weight
    s += min(25.0, pts * 0.5)                  # HN / lobsters heat
    s += min(10.0, comments * 0.4)             # discussion
    hits = sum(1 for k in AI_KEYWORDS if k in text)
    s += min(12.0, hits * 2.5)                 # on-theme bonus
    if any(b in text for b in HARD_REJECT):
        s -= 40.0
    if len(it.get("desc", "")) < 20:
        s -= 5.0                                # no idea what it does
    return {**it, "score": round(max(0.0, min(100.0, s)), 1)}


def key_of(it: dict[str, Any]) -> str:
    base = (it.get("repo") or it.get("url") or it.get("title") or "").lower().strip()
    base = re.sub(r"^https?://(www\.)?", "", base).rstrip("/")
    return hashlib.sha1(base.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_research(args) -> int:
    SNAP.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    sources = [("github-trending-weekly", lambda: src_github_trending("weekly")),
               ("github-trending-daily", lambda: src_github_trending("daily")),
               ("hackernews", src_hackernews),
               ("lobsters", src_lobsters),
               ("github-search", src_github_search)]
    items: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {"fetched_at": now().isoformat(), "sources": {}}
    for name, fn in sources:
        try:
            got = fn()
        except Exception as e:  # noqa: BLE001 - partial data is fine
            log(f"{name} FAILED: {e}")
            meta["sources"][name] = {"ok": False, "error": str(e)[:200]}
            continue
        meta["sources"][name] = {"ok": True, "count": len(got)}
        log(f"{name}: {len(got)}")
        for it in got:
            k = key_of(it)
            if k in items:
                items[k]["sources"] = sorted(set(items[k]["sources"] + [it["source"]]))
                items[k]["gained"] = max(items[k].get("gained") or 0, it.get("gained") or 0)
                continue
            it["sources"] = [it["source"]]
            it["key"] = k
            items[k] = it

    ranked = sorted((score_item(v) for v in items.values()), key=lambda x: -x["score"])
    stamp = today().isoformat()
    snap = {"date": stamp, "meta": meta, "items": ranked}
    (SNAP / f"{stamp}.json").write_text(json.dumps(snap, ensure_ascii=False, indent=2))

    # candidates.json keeps history of everything ever seen, so we don't re-pick
    cand_path = STATE / "candidates.json"
    hist = json.loads(cand_path.read_text()) if cand_path.exists() else {"seen": {}, "picks": []}
    for it in ranked:
        prev = hist["seen"].get(it["key"])
        hist["seen"][it["key"]] = {
            "repo": it.get("repo") or it.get("title"),
            "url": it.get("url"),
            "first_seen": (prev or {}).get("first_seen", stamp),
            "last_seen": stamp,
            "best_score": max((prev or {}).get("best_score", 0), it["score"]),
            "sources": it["sources"],
        }
    cand_path.write_text(json.dumps(hist, ensure_ascii=False, indent=2))

    report = render_report(ranked, meta, stamp, hist)
    out = REPORTS / f"{stamp}.md"
    out.write_text(report)
    log(f"report -> {out.relative_to(ROOT)}")
    if args.top:
        for it in ranked[: args.top]:
            name = it.get("repo") or it.get("title")
            print(f"{it['score']:5.1f}  {name}  {it.get('gained') or it.get('points') or ''}")
    return 0


def render_report(ranked, meta, stamp, hist) -> str:
    top = ranked[:15]
    lines = [f"# 调研快照 {stamp}", "",
             f"- 抓取时间：{meta['fetched_at']}",
             f"- 去重后条目：{len(ranked)}",
             ""]
    ok = [f"{k}({v.get('count', 0)})" for k, v in meta["sources"].items() if v.get("ok")]
    bad = [f"{k}({v.get('error', '')})" for k, v in meta["sources"].items() if not v.get("ok")]
    lines.append(f"- 来源：{', '.join(ok) or '无'}")
    if bad:
        lines.append(f"- 失败：{'; '.join(bad)}")
    lines += ["", "## 候选 Top 15", "",
              "| # | 分数 | 项目 | 周涨星/热度 | 来源 | 一句话 |",
              "|---|------|------|------|------|--------|"]
    for i, it in enumerate(top, 1):
        name = it.get("repo") or it.get("title") or "?"
        url = it.get("url") or ""
        heat = it.get("gained") or it.get("points") or 0
        desc = (it.get("desc") or "").replace("|", "/").replace("\n", " ")[:90]
        lines.append(f"| {i} | {it['score']} | [{name}]({url}) | {heat} | {','.join(it['sources'])} | {desc} |")
    lines += ["", "## 建议", ""]
    if top:
        p = top[0]
        lines.append(f"头名是 **{p.get('repo') or p.get('title')}**（{p['score']} 分）。"
                     "选它之前先看它有没有可被本地复刻的「最小可运行闭环」："
                     "数据从哪来、模型怎么调、界面长什么样。")
    lines += ["", "## 已选过的（避免重复）", ""]
    picks = hist.get("picks", [])
    if picks:
        for p in picks[-10:]:
            lines.append(f"- {p.get('date')}：{p.get('repo')} — {p.get('angle', '')}")
    else:
        lines.append("- 还没有")
    lines.append("")
    return "\n".join(lines)


def cmd_pick(args) -> int:
    cand_path = STATE / "candidates.json"
    hist = json.loads(cand_path.read_text()) if cand_path.exists() else {"seen": {}, "picks": []}
    repo = args.repo
    if not repo:
        print("need --repo", file=sys.stderr)
        return 2
    hit = next((v for v in hist["seen"].values()
                if (v.get("repo") or "").lower() == repo.lower()), None)
    if not hit:
        print(f"{repo} 不在候选池里，先跑 research 或手动确认", file=sys.stderr)
        return 1
    if any((p.get("repo") or "").lower() == repo.lower() for p in hist["picks"]):
        print(f"{repo} 已经选过了", file=sys.stderr)
        return 1
    hist["picks"].append({"date": today().isoformat(), "repo": repo,
                          "url": hit.get("url"), "angle": args.angle or ""})
    cand_path.write_text(json.dumps(hist, ensure_ascii=False, indent=2))

    cur = state_path("current_week.json")
    cfg = json.loads(cur.read_text()) if cur.exists() else {}
    cfg.update({"week_of": today().isoformat(), "project": repo, "url": hit.get("url"),
                "angle": args.angle or "", "phase": "scaffold",
                "iterations_today": {}, "created_at": now().isoformat()})
    cur.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    print(json.dumps({"picked": repo, "angle": args.angle}, ensure_ascii=False))
    return 0


def state_path(name: str) -> Path:
    STATE.mkdir(exist_ok=True)
    return STATE / name


def cmd_status(args) -> int:
    cur = state_path("current_week.json")
    cfg = json.loads(cur.read_text()) if cur.exists() else {}
    d = today()
    print(f"today={d} {now():%H:%M} tz=Asia/Shanghai")
    if not cfg:
        print("no current week picked yet")
        return 0
    print(f"week_of={cfg.get('week_of')} project={cfg.get('project')} phase={cfg.get('phase')}")
    got = cfg.get("iterations_today", {}).get(d.isoformat(), 0)
    print(f"iterations today ({d}): {got}")
    last = cfg.get("last_iteration")
    if last:
        try:
            age = now() - datetime.fromisoformat(last)
            print(f"last iteration: {age.total_seconds() / 3600:.1f}h ago ({last})")
        except ValueError:
            print(f"last iteration: {last}")
    return 0


def cmd_log(args) -> int:
    cur = state_path("current_week.json")
    cfg = json.loads(cur.read_text()) if cur.exists() else {}
    d = today().isoformat()
    it = cfg.setdefault("iterations_today", {})
    it[d] = it.get(d, 0) + 1
    cfg["last_iteration"] = now().isoformat()
    cfg["phase"] = args.phase or cfg.get("phase")
    cur.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    # The counter lives in state; docs/ITERATIONS.md is written by hand so it
    # can explain *why*, not just *what*. Machine log stays greppable.
    log = state_path("iterations.log")
    with log.open("a") as f:
        f.write(f"{now():%Y-%m-%d %H:%M}  {d} #{it[d]}  {args.note}\n")
    print(f"logged #{it[d]} for {d}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="lab")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("research")
    r.add_argument("--top", type=int, default=10)
    r.set_defaults(fn=cmd_research)

    p = sub.add_parser("pick")
    p.add_argument("--repo", required=True)
    p.add_argument("--angle", default="")
    p.set_defaults(fn=cmd_pick)

    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)

    lg = sub.add_parser("log")
    lg.add_argument("--note", required=True)
    lg.add_argument("--phase", default="")
    lg.set_defaults(fn=cmd_log)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
