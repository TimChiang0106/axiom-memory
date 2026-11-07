"""Render the database into a human-readable markdown mirror.

Postgres is the source of truth. The export directory is a generated view —
one file per memory plus a MEMORY.md index — meant for browsing, grepping, and
committing to git as a backup. Edits to these files do not flow back.
"""

import json
from pathlib import Path

from axiom.models import Memory
from axiom.store import MemoryStore


def render_memory(memory: Memory) -> str:
    # json.dumps produces valid single-line YAML scalars, quoting included.
    return (
        "---\n"
        f"name: {memory.name}\n"
        f"title: {json.dumps(memory.title, ensure_ascii=False)}\n"
        f"description: {json.dumps(memory.description, ensure_ascii=False)}\n"
        f"type: {memory.type}\n"
        f"category: {memory.category}\n"
        f"provenance: {memory.provenance}\n"
        f"source_app: {memory.source_app}\n"
        f"created_at: {memory.created_at.isoformat()}\n"
        f"updated_at: {memory.updated_at.isoformat()}\n"
        f"related: {json.dumps(memory.related)}\n"
        "---\n\n"
        f"{memory.content}\n"
    )


def render_index(memories: list[Memory]) -> str:
    lines = ["# Memory Index", ""]
    order = ["you", "people", "areas", "topics"]
    titles = {
        "you": "You",
        "people": "People",
        "areas": "Areas",
        "topics": "Topics",
    }
    by_category: dict[str, list[Memory]] = {cat: [] for cat in order}
    for m in memories:
        by_category.setdefault(m.category, []).append(m)

    for cat in order:
        items = by_category.get(cat, [])
        if not items:
            continue
        lines.append(f"## {titles.get(cat, cat.title())}")
        lines.append("")
        for m in sorted(items, key=lambda x: x.name):
            lines.append(f"- [{m.name}]({cat}/{m.name}.md) — {m.description} ({m.type})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


async def export_all(store: MemoryStore, out_dir: Path) -> int:
    """Write every memory to out_dir/{category}/{name}.md, prune files for forgotten memories."""
    memories = await store.all_memories()
    out_dir.mkdir(parents=True, exist_ok=True)

    valid_paths = {out_dir / m.category / f"{m.name}.md" for m in memories}
    for stale in out_dir.rglob("*.md"):
        if stale.name != "MEMORY.md" and stale not in valid_paths:
            stale.unlink()

    for sub in list(out_dir.iterdir()):
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()

    for memory in memories:
        cat_dir = out_dir / memory.category
        cat_dir.mkdir(parents=True, exist_ok=True)
        (cat_dir / f"{memory.name}.md").write_text(render_memory(memory), encoding="utf-8")

    (out_dir / "MEMORY.md").write_text(render_index(memories), encoding="utf-8")
    return len(memories)
