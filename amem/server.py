"""HTTP API + static viewer.

FastAPI is an optional extra (`pip install -e ".[server]"`). Importing this
module without FastAPI raises a clear message instead of a bare ImportError,
because the CLI and MCP paths work fine without it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import FileResponse, JSONResponse
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        'server extras missing: pip install -e ".[server]"'
    ) from exc

from . import __version__
from .embed import embedder_from_env, reindex, save_embedder, semantic_search
from .extract import RuleExtractor, adapt_claude_transcript, adapt_dsh_transcript, events_from_record
from .store import Observation, Store

WEB = Path(__file__).resolve().parent / "web"


def default_db() -> str:
    return os.environ.get("AMEM_DB", os.path.expanduser("~/.agentledger/memory.db"))


class NewSession(BaseModel):
    harness: str = "manual"
    project: str = ""
    cwd: str = ""


class NewObservation(BaseModel):
    kind: str = "fact"
    title: str = Field(min_length=1, max_length=300)
    body: str = ""
    tags: list[str] = []
    source_ref: str = ""
    importance: float = 0.5


class IngestRequest(BaseModel):
    transcript: str = Field(description="path to a JSONL transcript")
    harness: str = Field(default="dsh", description="dsh | claude")
    project: str = ""


def create_app(db_path: str | None = None) -> "FastAPI":
    resolved = db_path or default_db()
    store = Store(resolved)
    data_dir = Path(resolved).expanduser().parent
    embedder = embedder_from_env()
    app = FastAPI(title="AgentLedger", version=__version__)
    app.state.store = store
    app.state.embedder = embedder

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, **store.stats()}

    @app.get("/api/stats")
    def stats():
        kinds = {}
        for r in store.conn.execute(
            "SELECT kind, COUNT(*) c FROM observations GROUP BY kind ORDER BY c DESC"
        ):
            kinds[r["kind"]] = r["c"]
        return {**store.stats(), "kinds": kinds, "embed_model": embedder.name}

    @app.get("/api/sessions")
    def sessions(limit: int = Query(50, ge=1, le=500)):
        return {"items": store.list_sessions(limit)}

    @app.post("/api/sessions", status_code=201)
    def new_session(body: NewSession):
        sid = store.start_session(body.harness, body.project, body.cwd)
        return {"id": sid}

    @app.post("/api/sessions/{sid}/end")
    def end_session(sid: int):
        if not store.get_session(sid):
            raise HTTPException(404, "no such session")
        store.end_session(sid)
        return {"ok": True}

    @app.get("/api/sessions/{sid}/observations")
    def session_obs(sid: int, limit: int = Query(500, ge=1, le=2000)):
        if not store.get_session(sid):
            raise HTTPException(404, "no such session")
        return {"items": store.observations_for_session(sid, limit),
                "summary": store.latest_summary(sid)}

    @app.post("/api/sessions/{sid}/observations", status_code=201)
    def add_obs(sid: int, body: NewObservation):
        if not store.get_session(sid):
            raise HTTPException(404, "no such session")
        oid = store.add_observation(sid, Observation(**body.model_dump()))
        return {"id": oid}

    @app.get("/api/observations")
    def recent(limit: int = Query(20, ge=1, le=200), kind: str | None = None):
        return {"items": store.recent(limit, kind)}

    @app.get("/api/observations/{oid}")
    def one(oid: int):
        row = store.get_observation(oid)
        if not row:
            raise HTTPException(404, "no such observation")
        return row

    @app.get("/api/search")
    def search(q: str = Query(min_length=1), limit: int = Query(10, ge=1, le=100),
               mode: str = Query("auto", pattern="^(auto|keyword|semantic)$")):
        if mode == "semantic" or (mode == "auto" and embedder.name != "local-hash-tfidf"):
            return {"items": semantic_search(store, q, limit, embedder), "mode": "semantic"}
        hits = store.search(q, limit)
        return {"items": hits, "mode": "keyword" if hits else "keyword"}

    @app.get("/api/timeline/{oid}")
    def timeline(oid: int, before: int = Query(3, ge=0, le=50), after: int = Query(3, ge=0, le=50)):
        return {"items": store.timeline(oid, before, after)}

    @app.get("/api/summaries")
    def summaries(limit: int = Query(20, ge=1, le=200)):
        return {"items": store.summaries(limit)}

    @app.post("/api/sessions/{sid}/summarize")
    def summarize(sid: int):
        obs = store.observations_for_session(sid)
        if not obs:
            raise HTTPException(400, "session has no observations")
        text = render_markdown_summary(obs)
        store.set_summary(sid, text, obs_count=len(obs))
        return {"summary": text, "obs_count": len(obs)}

    @app.post("/api/ingest")
    def ingest(body: IngestRequest):
        path = Path(body.transcript).expanduser()
        if not path.exists():
            raise HTTPException(400, f"no such file: {path}")
        events = (adapt_claude_transcript(path) if body.harness == "claude"
                  else adapt_dsh_transcript(path))
        sid = store.start_session(body.harness, body.project or str(path.parent), str(path.parent))
        obs = RuleExtractor().extract(events)
        store.add_many(sid, obs)
        store.end_session(sid)
        indexed = reindex(store, embedder)
        save_embedder(embedder, data_dir)
        return {"session_id": sid, "events": len(events), "observations": len(obs),
                "indexed": indexed}

    @app.post("/api/reindex")
    def do_reindex():
        n = reindex(store, embedder)
        save_embedder(embedder, data_dir)
        return {"indexed": n, "model": embedder.name}

    @app.get("/api/export")
    def export():
        data = {
            "version": __version__,
            "sessions": store.list_sessions(1000),
            "observations": [],
            "summaries": store.summaries(1000),
        }
        for s in data["sessions"]:
            data["observations"].extend(store.observations_for_session(s["id"]))
        return JSONResponse(data)

    @app.get("/")
    def index():
        f = WEB / "index.html"
        if not f.exists():
            return {"error": "web assets missing"}
        return FileResponse(f)

    @app.get("/app.js")
    def app_js():
        return FileResponse(WEB / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    def style_css():
        return FileResponse(WEB / "style.css", media_type="text/css")

    return app


def render_markdown_summary(obs: list[dict]) -> str:
    """Deterministic session summary: what broke, what was decided, what's next."""
    by_kind: dict[str, list[dict]] = {}
    for o in obs:
        by_kind.setdefault(o["kind"], []).append(o)

    def block(title: str, kind: str, empty: str = ""):
        items = by_kind.get(kind, [])
        if not items:
            return [f"- {empty}"] if empty else []
        return [f"- {o['title']}" for o in items[:6]]

    lines = ["## 这次会话"]
    for title, kind in (("做了什么决定", "decision"), ("踩了什么坑", "bug"),
                        ("怎么修好的", "bugfix"), ("发现了什么", "discovery"),
                        ("还没做完", "todo")):
        lines.append(f"### {title}")
        lines.extend(block(title, kind) or ["- 无"])
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - console entry
    import uvicorn

    host = os.environ.get("AMEM_HOST", "127.0.0.1")
    port = int(os.environ.get("AMEM_PORT", "8407"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
