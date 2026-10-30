"""MCP server (stdio, JSON-RPC 2.0) exposing memory tools to any agent.

Deliberately dependency-free: the MCP transport is newline-delimited JSON over
stdin/stdout, and the protocol surface we need is tiny (initialize, tools/list,
tools/call). That keeps the server usable inside a `python -m amem.mcp` process
without dragging in an SDK or pinning its version.

Tools follow the token-efficient 3-layer pattern:
  1. `search`          — cheap, returns ids + titles + snippets
  2. `timeline`        — context around one observation
  3. `get_observations`— full bodies, batched ids only
Plus `save_memory` so the agent can write an observation directly.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from .store import Observation, Store

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agentledger", "version": "0.1.0"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": "Search session memory. Returns compact hits (id, kind, title, snippet). "
                       "Use this first; then call get_observations with the ids you want in full.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "what to look for, natural language"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "kind": {"type": "string", "description": "optional filter: decision/bugfix/bug/discovery/todo/fact"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "timeline",
        "description": "Show what was happening around one observation id (a few before and after).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "obs_id": {"type": "integer"},
                "before": {"type": "integer", "default": 3},
                "after": {"type": "integer", "default": 3},
            },
            "required": ["obs_id"],
        },
    },
    {
        "name": "get_observations",
        "description": "Fetch full bodies for a batch of observation ids. Always batch; do not loop.",
        "inputSchema": {
            "type": "object",
            "properties": {"ids": {"type": "array", "items": {"type": "integer"}, "maxItems": 20}},
            "required": ["ids"],
        },
    },
    {
        "name": "save_memory",
        "description": "Store one observation now (use sparingly, for decisions worth keeping).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["decision", "bugfix", "bug", "discovery", "todo", "fact"]},
                "title": {"type": "string", "maxLength": 160},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["kind", "title", "body"],
        },
    },
]


def snippet(text: str, n: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


class MemoryServer:
    def __init__(self, db_path: str, session_id: int | None = None):
        self.store = Store(db_path)
        self.session_id = session_id
        if session_id is None:
            self.session_id = self.store.start_session(
                harness="mcp", project=os.environ.get("AMEM_PROJECT", ""), cwd=os.getcwd()
            )

    # -- tool impls -----------------------------------------------------
    def tool_search(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        hits = self.store.search(str(args.get("query", "")), int(args.get("limit", 10)))
        kind = args.get("kind")
        if kind:
            hits = [h for h in hits if h["kind"] == kind]
        return [
            {"id": h["id"], "kind": h["kind"], "title": h["title"],
             "snippet": snippet(h["body"]), "importance": h["importance"],
             "session_id": h["session_id"], "created_at": h["created_at"]}
            for h in hits
        ]

    def tool_timeline(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.store.timeline(int(args["obs_id"]),
                                   int(args.get("before", 3)), int(args.get("after", 3)))
        return [{"id": r["id"], "kind": r["kind"], "title": r["title"],
                 "snippet": snippet(r["body"], 160)} for r in rows]

    def tool_get(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for i in list(args.get("ids", []))[:20]:
            o = self.store.get_observation(int(i))
            if o:
                out.append({k: o[k] for k in ("id", "session_id", "kind", "title", "body",
                                              "tags", "importance", "created_at", "source_ref")})
        return out

    def tool_save(self, args: dict[str, Any]) -> dict[str, Any]:
        oid = self.store.add_observation(self.session_id, Observation(
            kind=str(args["kind"]), title=str(args["title"])[:160], body=str(args["body"]),
            tags=[str(t) for t in (args.get("tags") or [])], source_ref="mcp:save_memory",
            importance=0.7,
        ))
        return {"id": oid, "ok": True}

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        table = {
            "search": self.tool_search,
            "timeline": self.tool_timeline,
            "get_observations": self.tool_get,
            "save_memory": self.tool_save,
        }
        if name not in table:
            raise ValueError(f"unknown tool {name}")
        return table[name](args)


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(msg: dict[str, Any], server: MemoryServer) -> dict[str, Any] | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            payload = server.dispatch(name, args)
        except Exception as exc:  # noqa: BLE001 - report tool failure, keep serving
            return _result(req_id, {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True})
        return _result(req_id, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]})
    return _error(req_id, -32601, f"method not found: {method}")


def serve(db_path: str, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = MemoryServer(db_path)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        resp = handle(msg, server)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:  # pragma: no cover - entry point
    db = os.environ.get("AMEM_DB", os.path.expanduser("~/.agentledger/memory.db"))
    serve(db)


if __name__ == "__main__":  # pragma: no cover
    main()
