"""Turn raw agent events into observations.

Two extractors ship here:

``RuleExtractor`` — deterministic, offline, no model. It looks for the moments
that actually matter in a coding session: a command failing, a test going
green, a commit, a config change, a decision written in prose. This is the
default because it always works and every result is explainable.

``LLMExtractor`` — optional. It hands the transcript to an OpenAI-compatible
chat endpoint (Ollama, vLLM, DeepSeek, whatever) and asks for structured
observations. Used when ``AMEM_LLM_URL`` is set; falls back to rules on error.

The event format accepted by ``extract_events`` is a small, harness-agnostic
list of dicts::

    {"type": "user"|"assistant"|"tool_use"|"tool_result"|"note", ...}

DSH and Claude Code transcripts are adapted into this shape by
:func:`adapt_dsh_transcript` / :func:`adapt_claude_transcript`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .store import Observation

# how much of a command/output we keep; observations are for humans, not logs
MAX_BODY = 1200

KIND_RULES: list[tuple[str, re.Pattern[str], float]] = [
    # (kind, pattern over "cmd\noutput", importance)
    ("bugfix", re.compile(r"(fix|fixed|修复|改成|改为|workaround|resolved)", re.I), 0.75),
    ("bug", re.compile(r"(traceback|error:|failed|failure|exit code [1-9]|报错|失败)", re.I), 0.7),
    ("decision", re.compile(r"(决定|取舍|choose|chose|trade-?off|instead of|rather than|方案)", re.I), 0.8),
    ("discovery", re.compile(r"(原来|发现|interesting|turns out|实际上|实际上是)", re.I), 0.65),
    ("todo", re.compile(r"(todo|fixme|待办|下一步|next step|later)", re.I), 0.5),
]

COMMAND_NOISE = re.compile(
    r"^\s*(ls|pwd|cd|cat|echo|printf|clear|which|whoami|date|history)\b"
)
TEST_CMD = re.compile(
    r"\b(pytest|unittest|npm (run )?test|pnpm (run )?test|yarn test|go test|cargo test|"
    r"vitest|jest|mocha|rspec|phpunit)\b"
)
BUILD_CMD = re.compile(r"\b(npm|pnpm|yarn|bun) (run )?build\b|\bcargo build\b|\bmake\b|\bgo build\b")


def _clip(text: str, limit: int = MAX_BODY) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n…（截断）"


def _first_meaningful_line(text: str) -> str:
    """Pick a line that describes the failure, not the noise around it."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        if re.search(r"(error|exception|traceback|failed|assert|Error:|Exception)", ln, re.I):
            return ln[:200]
    return (lines[-1] if lines else "")[:200]


@dataclass
class Event:
    type: str
    text: str = ""
    name: str = ""
    input: dict[str, Any] | None = None
    output: str = ""
    ok: bool | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(
            type=str(d.get("type", "")),
            text=str(d.get("text") or d.get("content") or ""),
            name=str(d.get("name", "")),
            input=d.get("input") if isinstance(d.get("input"), dict) else None,
            output=str(d.get("output") or d.get("result") or ""),
            ok=d.get("ok") if isinstance(d.get("ok"), bool) else None,
        )


class RuleExtractor:
    """Deterministic observation extraction."""

    name = "rules"

    def __init__(self, max_obs: int = 60):
        self.max_obs = max_obs

    def extract(self, events: Iterable[dict[str, Any] | Event]) -> list[Observation]:
        obs: list[Observation] = []
        pending_fail: Event | None = None
        seen_titles: set[str] = set()
        for i, raw in enumerate(events):
            ev = raw if isinstance(raw, Event) else Event.from_dict(raw)
            ref = f"event:{i}"
            if ev.type == "tool_use":
                cmd = str((ev.input or {}).get("command", "")) if ev.input else ""
                tool = ev.name or str((ev.input or {}).get("tool", ""))
                if cmd:
                    o = self._from_command(cmd, ref, pending_fail)
                else:
                    o = self._from_tool(tool, ev.input or {}, ref)
                if o:
                    obs.append(o)
            elif ev.type in {"tool_result", "result"}:
                if ev.ok is False:
                    pending_fail = ev
                    o = self._from_failure(ev, ref)
                    if o:
                        obs.append(o)
                elif ev.ok is True and pending_fail is not None:
                    obs.append(self._recovery(ev, pending_fail, ref))
                    pending_fail = None
            elif ev.type in {"user", "assistant", "note"}:
                for o in self._from_prose(ev, ref):
                    obs.append(o)
            if len(obs) >= self.max_obs:
                break

        # dedupe by title, keep the higher-importance one
        out: list[Observation] = []
        for o in obs:
            key = re.sub(r"\s+", " ", o.title.lower()).strip()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            out.append(o)
        return out

    # -- builders -------------------------------------------------------
    def _from_command(self, cmd: str, ref: str, pending_fail: Event | None) -> Observation | None:
        first = cmd.strip().splitlines()[0][:200]
        if COMMAND_NOISE.match(first):
            return None
        if TEST_CMD.search(first):
            return Observation("test", f"跑测试：{first}", _clip(cmd), tags=["test"], source_ref=ref, importance=0.6)
        if BUILD_CMD.search(first):
            return Observation("build", f"构建：{first}", _clip(cmd), tags=["build"], source_ref=ref, importance=0.5)
        if re.search(r"\bgit (commit|rebase|cherry-pick|merge|push)\b", first):
            return Observation("commit", f"版本操作：{first}", _clip(cmd), tags=["git"], source_ref=ref, importance=0.55)
        if re.search(r"\b(rm|mv|chmod|sudo|kill|docker|systemctl)\b", first):
            return Observation("ops", f"系统操作：{first}", _clip(cmd), tags=["ops"], source_ref=ref, importance=0.6)
        return Observation("cmd", f"执行：{first}", _clip(cmd), tags=["shell"], source_ref=ref, importance=0.3)

    def _from_tool(self, tool: str, args: dict[str, Any], ref: str) -> Observation | None:
        if not tool:
            return None
        hint = ""
        for key in ("file_path", "path", "pattern", "query", "url", "description"):
            v = args.get(key)
            if isinstance(v, str) and v:
                hint = v
                break
        title = f"调用 {tool}" + (f"：{hint[:80]}" if hint else "")
        body = _clip(json.dumps(args, ensure_ascii=False, indent=2))
        return Observation("tool", title, body, tags=[tool.lower()], source_ref=ref, importance=0.25)

    def _from_failure(self, ev: Event, ref: str) -> Observation | None:
        line = _first_meaningful_line(ev.output)
        if not line:
            return None
        return Observation("bug", f"翻车：{line[:120]}", _clip(ev.output), tags=["failure"],
                           source_ref=ref, importance=0.85)

    def _recovery(self, ev: Event, fail: Event, ref: str) -> Observation:
        fail_line = _first_meaningful_line(fail.output)
        return Observation(
            "bugfix",
            f"修好了：{fail_line[:100]}",
            f"之前：{_clip(fail.output, 400)}\n\n之后：{_clip(ev.output, 400)}",
            tags=["fix"], source_ref=ref, importance=0.9,
        )

    def _from_prose(self, ev: Event, ref: str) -> list[Observation]:
        text = ev.text.strip()
        if len(text) < 12:
            return []
        out = []
        # a short standalone paragraph that states a decision is gold
        for kind, pat, imp in KIND_RULES:
            m = pat.search(text)
            if not m:
                continue
            start = max(0, m.start() - 120)
            snippet = text[start:start + 600].strip()
            title = _clip(first_sentence(snippet), 140)
            out.append(Observation(kind, title, _clip(snippet), tags=[kind],
                                   source_ref=ref, importance=imp))
            break  # one observation per message; avoid double counting
        return out


def first_sentence(text: str) -> str:
    m = re.search(r"[。！？.!?\n]", text.strip())
    if m and m.start() > 3:
        return text.strip()[: m.start()]
    return text.strip()[:160]


# --------------------------------------------------------------------------- #
# transcript adapters
# --------------------------------------------------------------------------- #

def adapt_dsh_transcript(path: str | Path) -> list[dict[str, Any]]:
    """DSH persists session events as JSONL; map the ones we understand.

    Unknown event shapes are skipped rather than guessed at, so a format change
    upstream degrades to fewer observations instead of wrong ones.
    """
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.extend(events_from_record(rec))
    return out


def adapt_claude_transcript(path: str | Path) -> list[dict[str, Any]]:
    """Claude Code writes one JSON object per line with a ``message`` field."""
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("message")
        if isinstance(msg, dict):
            out.extend(events_from_record({"role": msg.get("role"), "content": msg.get("content"),
                                           "is_error": rec.get("isError")}))
    return out


def events_from_record(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort mapping from a generic message record to our Event dicts."""
    events: list[dict[str, Any]] = []
    content = rec.get("content")
    role = rec.get("role") or rec.get("type") or ""
    if isinstance(content, str) and content.strip():
        events.append({"type": "user" if role == "user" else "assistant", "text": content})
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                events.append({"type": "user" if role == "user" else "assistant",
                               "text": block.get("text", "")})
            elif btype == "tool_use":
                events.append({"type": "tool_use", "name": block.get("name", ""),
                               "input": block.get("input") or {}})
            elif btype == "tool_result":
                events.append({"type": "tool_result",
                               "output": _stringify(block.get("content")),
                               "ok": not block.get("is_error", rec.get("is_error", False))})
    elif rec.get("type") == "tool_result":
        events.append({"type": "tool_result", "output": _stringify(rec.get("content")),
                       "ok": not rec.get("is_error", False)})
    return events


def _stringify(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for b in v:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(v)


# --------------------------------------------------------------------------- #
# optional LLM extractor
# --------------------------------------------------------------------------- #

LLM_PROMPT = """你在读一次编程 agent 的会话记录。请挑出 3~8 条以后值得记住的内容，
每条给一个 kind（decision/bugfix/bug/discovery/todo/fact）、一句不超过 60 字的 title、
正文 body 和 tags 数组。只输出 JSON 数组，不要别的。

会话：
{transcript}
"""


class LLMExtractor:
    """OpenAI-compatible chat endpoint. Entirely optional."""

    name = "llm"

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def extract(self, events: Iterable[dict[str, Any] | Event]) -> list[Observation]:
        evs = [e if isinstance(e, Event) else Event.from_dict(e) for e in events]
        transcript = "\n".join(
            f"[{e.type}] {e.name} {json.dumps(e.input or {}, ensure_ascii=False) if e.input else ''} "
            f"{e.text or e.output}"[:1500]
            for e in evs
        )[:24000]
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": LLM_PROMPT.format(transcript=transcript)}],
        }
        try:
            raw = _post_json(f"{self.base_url}/chat/completions", payload, self.api_key, self.timeout)
            content = raw["choices"][0]["message"]["content"]
            arr = _extract_json_array(content)
            return [
                Observation(
                    kind=str(x.get("kind", "fact"))[:32],
                    title=str(x.get("title", ""))[:160],
                    body=_clip(str(x.get("body", ""))),
                    tags=[str(t)[:24] for t in (x.get("tags") or [])][:8],
                    source_ref="llm",
                    importance=float(x.get("importance", 0.6)),
                )
                for x in arr
                if x.get("title")
            ][:20]
        except Exception as exc:  # noqa: BLE001 - degrade, never lose the session
            print(f"[amem] LLM extraction failed ({exc}); falling back to rules")
            return RuleExtractor().extract(evs)


def _post_json(url: str, payload: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [x for x in arr if isinstance(x, dict)]
