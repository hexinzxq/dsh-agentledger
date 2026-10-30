"""Embeddings and semantic-ish retrieval.

Default: a local hashed TF-IDF embedder, pure Python, no model download, no
network. It is not as good as a real sentence encoder, but it is deterministic,
instant, and good enough to rank "which observation is about the same thing".

Optional backends (set ``AMEM_EMBED_BACKEND``):

* ``ollama`` — ``http://localhost:11434/api/embeddings`` with ``AMEM_EMBED_MODEL``
* ``openai`` — any OpenAI-compatible ``/embeddings`` endpoint

All backends implement ``embed(texts) -> list[list[float]]`` and are L2
normalised, so cosine similarity reduces to a dot product.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Protocol

DIM = 512

_TOKEN = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens plus CJK unigrams + bigrams."""
    text = text.lower()
    toks = _TOKEN.findall(text)
    out: list[str] = []
    cjk_run: list[str] = []
    for t in toks:
        if len(t) == 1 and "\u4e00" <= t <= "\u9fff":
            cjk_run.append(t)
        else:
            if cjk_run:
                out.extend(cjk_run)
                out.extend(a + b for a, b in zip(cjk_run, cjk_run[1:]))
                cjk_run = []
            out.append(t)
    if cjk_run:
        out.extend(cjk_run)
        out.extend(a + b for a, b in zip(cjk_run, cjk_run[1:]))
    return out


def _bucket(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big") % DIM


def _l2(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


class HashingEmbedder:
    """Hashed TF with sublinear term frequency; IDF learned from the corpus."""

    name = "local-hash-tfidf"

    def __init__(self, dim: int = DIM):
        self.dim = dim
        self.df: dict[str, int] = {}
        self.n_docs = 0

    def fit(self, texts: Iterable[str]) -> "HashingEmbedder":
        for t in texts:
            self.n_docs += 1
            for tok in set(tokenize(t)):
                self.df[tok] = self.df.get(tok, 0) + 1
        return self

    def _idf(self, tok: str) -> float:
        if not self.n_docs:
            return 1.0
        return math.log((1 + self.n_docs) / (1 + self.df.get(tok, 0))) + 1.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            counts: dict[str, int] = {}
            for tok in tokenize(t):
                counts[tok] = counts.get(tok, 0) + 1
            for tok, c in counts.items():
                vec[_bucket(tok)] += (1 + math.log(c)) * self._idf(tok)
            out.append(_l2(vec))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"dim": self.dim, "df": self.df, "n_docs": self.n_docs}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HashingEmbedder":
        e = cls(dim=d.get("dim", DIM))
        e.df = d.get("df", {})
        e.n_docs = d.get("n_docs", 0)
        return e


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def semantic_search(store, query: str, limit: int = 10, embedder: Embedder | None = None) -> list[dict[str, Any]]:
    """Rank stored observations by cosine similarity, falling back to the
    keyword search when nothing has been embedded yet."""
    pairs = store.embeddings((embedder or HashingEmbedder()).name)
    if not pairs:
        return store.search(query, limit)
    emb = embedder or HashingEmbedder()
    qv = emb.embed([query])[0]
    scored = []
    for obs_id, vec in pairs:
        s = cosine(qv, vec)
        if s <= 0:
            continue
        obs = store.get_observation(obs_id)
        if obs:
            obs["similarity"] = round(s, 4)
            scored.append(obs)
    scored.sort(key=lambda o: -o["similarity"])
    return scored[:limit]


def reindex(store, embedder: Embedder | None = None) -> int:
    """(Re)embed every observation. Called after ingest and after model change."""
    emb = embedder or HashingEmbedder()
    rows = store.recent(limit=10_000)
    if not rows:
        return 0
    texts = [f"{r['title']} {r['body']} {' '.join(r['tags'])}" for r in rows]
    if isinstance(emb, HashingEmbedder):
        emb.fit(texts)
    vecs = emb.embed(texts)
    for r, v in zip(rows, vecs):
        store.put_embedding(r["id"], v, emb.name)
    return len(rows)


# -- optional remote backends ---------------------------------------------- #

class OllamaEmbedder:
    name = "ollama"

    def __init__(self, model: str = "nomic-embed-text", base: str = "http://localhost:11434"):
        self.model = model
        self.base = base.rstrip("/")
        self.name = f"ollama-{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import urllib.request

        out = []
        for t in texts:
            req = urllib.request.Request(
                f"{self.base}/api/embeddings",
                data=json.dumps({"model": self.model, "prompt": t}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
                out.append(_l2(json.loads(r.read().decode())["embedding"]))
        return out


class OpenAIEmbedder:
    name = "openai"

    def __init__(self, model: str = "text-embedding-3-small",
                 base: str = "https://api.openai.com/v1", api_key: str = ""):
        self.model = model
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.name = f"openai-{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import urllib.request

        req = urllib.request.Request(
            f"{self.base}/embeddings",
            data=json.dumps({"model": self.model, "input": texts}).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
        )
        with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310
            data = json.loads(r.read().decode())
        return [_l2(d["embedding"]) for d in data["data"]]


def embedder_from_env() -> Embedder:
    import os

    backend = os.environ.get("AMEM_EMBED_BACKEND", "local").lower()
    if backend == "ollama":
        return OllamaEmbedder(
            model=os.environ.get("AMEM_EMBED_MODEL", "nomic-embed-text"),
            base=os.environ.get("AMEM_OLLAMA_URL", "http://localhost:11434"),
        )
    if backend == "openai":
        return OpenAIEmbedder(
            model=os.environ.get("AMEM_EMBED_MODEL", "text-embedding-3-small"),
            base=os.environ.get("AMEM_OPENAI_BASE", "https://api.openai.com/v1"),
            api_key=os.environ.get("AMEM_OPENAI_KEY", ""),
        )
    return HashingEmbedder()


def index_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "embedder.json"


def save_embedder(emb: Embedder, data_dir: str | Path) -> None:
    if isinstance(emb, HashingEmbedder):
        p = index_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(emb.to_dict()))


def load_embedder(data_dir: str | Path) -> Embedder:
    p = index_path(data_dir)
    if p.exists():
        try:
            return HashingEmbedder.from_dict(json.loads(p.read_text()))
        except (json.JSONDecodeError, KeyError):
            pass
    return HashingEmbedder()
