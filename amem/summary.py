"""Deterministic, model-free session summary.

A summary that costs nothing, needs no API key and always produces the same
text for the same observations. It is deliberately boring: five sections, six
items each, titles only. Full bodies stay one MCP call away.
"""
from __future__ import annotations

SECTIONS = (
    ("做了什么决定", "decision"),
    ("踩了什么坑", "bug"),
    ("怎么修好的", "bugfix"),
    ("发现了什么", "discovery"),
    ("还没做完", "todo"),
)


def render_markdown_summary(obs: list[dict], per_section: int = 6) -> str:
    by_kind: dict[str, list[dict]] = {}
    for o in obs:
        by_kind.setdefault(o.get("kind", "fact"), []).append(o)

    lines = ["## 这次会话"]
    for title, kind in SECTIONS:
        items = by_kind.get(kind, [])
        lines.append(f"### {title}")
        if items:
            lines.extend(f"- {o['title']}" for o in items[:per_section])
        else:
            lines.append("- 无")
    return "\n".join(lines)
