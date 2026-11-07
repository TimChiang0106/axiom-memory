from pathlib import Path

import pytest

from axiom.export import export_all
from axiom.models import ContentEdit
from axiom.store import MemoryStore


async def test_remember_creates_memory(store: MemoryStore):
    result = await store.remember(
        name="prefers-uv-over-pip",
        description="Tim prefers uv over pip for Python projects",
        content="Use `uv` for dependency management in all Python projects.",
        type="preference",
        source_app="test",
    )
    assert result.status == "created"
    assert result.memory is not None
    assert result.memory.name == "prefers-uv-over-pip"


async def test_remember_same_name_updates(store: MemoryStore):
    await store.remember(
        name="current-editor",
        description="Editor Tim uses",
        content="Uses VS Code.",
        type="state",
        source_app="test",
    )
    result = await store.remember(
        name="current-editor",
        description="Editor Tim uses",
        content="Uses Zed.",
        type="state",
        source_app="other-app",
    )
    assert result.status == "updated"
    assert result.memory is not None
    assert result.memory.content == "Uses Zed."
    assert result.memory.source_app == "other-app"


async def test_title_survives_an_update_without_one(store: MemoryStore):
    await store.remember(
        name="current-shell",
        title="目前用的 shell",
        description="Shell Tim uses",
        content="Uses zsh.",
        type="state",
        source_app="test",
    )
    result = await store.remember(
        name="current-shell",
        description="Shell Tim uses",
        content="Uses fish.",
        type="state",
        source_app="test",
    )
    assert result.memory is not None and result.memory.title == "目前用的 shell"
    [summary] = await store.list_all()
    assert summary.title == "目前用的 shell" and summary.label == "目前用的 shell"


async def test_archived_survives_an_update_that_leaves_it_out(store: MemoryStore):
    await store.remember(
        name="old-deploy",
        description="Old deploy flow",
        content="v1",
        type="procedural",
        source_app="test",
        archived=True,
    )
    result = await store.remember(
        name="old-deploy",
        description="Old deploy flow",
        content="v2",
        type="procedural",
        source_app="test",
    )
    assert result.memory is not None and result.memory.archived
    [summary] = await store.list_all()
    assert summary.archived and summary.created_at is not None


async def test_remember_flags_near_duplicate(store: MemoryStore):
    await store.remember(
        name="prefers-uv-over-pip",
        description="Tim prefers uv over pip for Python dependency management",
        content="Use `uv` for dependency management in all Python projects.",
        type="preference",
        source_app="test",
    )
    result = await store.remember(
        name="uv-for-python-deps",
        description="Tim prefers uv over pip for Python dependency management",
        content="Prefers `uv` for Python dependency management in projects.",
        type="preference",
        source_app="test",
    )
    assert result.status == "duplicate_suspected"
    assert [m.name for m in result.similar] == ["prefers-uv-over-pip"]

    forced = await store.remember(
        name="uv-for-python-deps",
        description="Tim prefers uv over pip for Python dependency management",
        content="Prefers `uv` for Python dependency management in projects.",
        type="preference",
        source_app="test",
        allow_duplicate=True,
    )
    assert forced.status == "created"


async def test_recall_matches_keywords(store: MemoryStore):
    await store.remember(
        name="axiom-project",
        description="Axiom is Tim's personal cross-app memory MCP server",
        content="Side project: MCP memory server, Python + FastMCP + Postgres, Cloud Run.",
        type="project",
        source_app="test",
    )
    await store.remember(
        name="favorite-coffee",
        description="Coffee preference",
        content="Drinks oat milk lattes.",
        type="fact",
        source_app="test",
    )
    results = await store.recall("mcp memory server")
    assert results and results[0].name == "axiom-project"


async def test_recall_skips_archived_unless_asked(store: MemoryStore):
    await store.remember(
        name="deploy-target-old",
        description="Old Cloud Run hosting, replaced in October",
        content="The memory server is deployed on Cloud Run.",
        type="fact",
        source_app="test",
        archived=True,
    )
    await store.remember(
        name="deploy-target",
        description="Home box hosting via docker compose",
        content="The memory server is deployed on the home box.",
        type="fact",
        source_app="test",
    )
    assert [m.name for m in await store.recall("memory server deployed")] == ["deploy-target"]
    both = await store.recall("memory server deployed", include_archived=True)
    assert {m.name for m in both} == {"deploy-target", "deploy-target-old"}
    assert [m.archived for m in both if m.name == "deploy-target-old"] == [True]
    nearest = await store.index_nearest("memory server deployed")
    assert [m.name for m in nearest] == ["deploy-target"]


async def test_recall_tracks_usage(store: MemoryStore):
    await store.remember(
        name="axiom-project",
        description="Axiom is Tim's personal cross-app memory MCP server",
        content="Side project: MCP memory server, Python + FastMCP + Postgres.",
        type="project",
        source_app="test",
    )
    before = await store.get("axiom-project")
    assert before is not None
    assert before.use_count == 0
    assert before.last_used_at is None

    await store.recall("mcp memory server")

    after = await store.get("axiom-project")
    assert after is not None
    assert after.use_count == 1
    assert after.last_used_at is not None

    await store.recall("mcp memory server")
    again = await store.get("axiom-project")
    assert again is not None
    assert again.use_count == 2


async def test_recall_does_not_touch_updated_at(store: MemoryStore):
    # Usage tracking must not masquerade as a content change: `state` staleness
    # decisions rely on updated_at, so recall must leave it alone.
    await store.remember(
        name="current-editor",
        description="Editor Tim uses",
        content="Uses Zed.",
        type="state",
        source_app="test",
    )
    before = await store.get("current-editor")
    assert before is not None

    await store.recall("editor Zed")

    after = await store.get("current-editor")
    assert after is not None
    assert after.updated_at == before.updated_at


async def test_recall_matches_chinese_content(store: MemoryStore):
    await store.remember(
        name="gateway-positioning",
        description="Gateway product positioning",
        content="這個產品是賣給金融業 CISO 的 AI 治理閘道,重點是稽核與存取控制。",
        type="project",
        source_app="test",
    )
    results = await store.recall("金融業 治理")
    assert [m.name for m in results] == ["gateway-positioning"]


async def test_recall_decays_stale_memories(store: MemoryStore, pool):
    # Two memories with identical relevance to the query; the only difference is
    # age. Retrieval-based decay must rank the fresh one above the stale one.
    for name in ("kube-fresh", "kube-stale"):
        await store.remember(
            name=name,
            description="kubernetes deployment and cluster config notes",
            content="Kubernetes deployment notes and cluster configuration.",
            type="fact",
            source_app="test",
            allow_duplicate=True,
        )
    await pool.execute(
        "UPDATE memories SET created_at = now() - interval '2000 days', "
        "last_used_at = now() - interval '2000 days' WHERE name = 'kube-stale'"
    )
    results = await store.recall("kubernetes deployment cluster")
    names = [m.name for m in results]
    assert names.index("kube-fresh") < names.index("kube-stale")


async def test_recall_strengthens_frequently_used(pool):
    # Same relevance, same age; with strengthening enabled, the memory
    # recalled many times before must outrank the never-used one (ACT-R
    # base-level activation). It is off by default; this checks the opt-in.
    await pool.execute("TRUNCATE memories, events, memory_versions")
    store = MemoryStore(pool, dedup_threshold=0.35, recall_threshold=0.1, use_count_dampening=4.0)
    for name in ("stack-used", "stack-fresh"):
        await store.remember(
            name=name,
            description="preferred python stack and tooling choices",
            content="Preferred Python stack and tooling choices.",
            type="preference",
            source_app="test",
            allow_duplicate=True,
        )
    await pool.execute("UPDATE memories SET use_count = 30 WHERE name = 'stack-used'")
    results = await store.recall("preferred python stack tooling")
    names = [m.name for m in results]
    assert names.index("stack-used") < names.index("stack-fresh")


async def test_procedural_memory_decays_slower(store: MemoryStore, pool):
    # Same relevance, same age; the procedural memory's longer half-life must
    # rank it above the fact once both have sat unused for a while.
    for name, type_ in (("deploy-workflow", "procedural"), ("deploy-note", "fact")):
        await store.remember(
            name=name,
            description="deploying the axiom server with docker compose",
            content="Deploying the axiom server with docker compose.",
            type=type_,
            source_app="test",
            allow_duplicate=True,
        )
    await pool.execute(
        "UPDATE memories SET created_at = now() - interval '400 days', "
        "last_used_at = now() - interval '400 days'"
    )
    results = await store.recall("deploying axiom docker compose")
    names = [m.name for m in results]
    assert names.index("deploy-workflow") < names.index("deploy-note")


async def test_recall_flags_stale_state(store: MemoryStore, pool):
    # A state memory past the stale threshold comes back flagged; a fresh one
    # and non-state types never do.
    for name, type_ in (("editor-setup-old", "state"), ("editor-setup-new", "state")):
        await store.remember(
            name=name,
            description="current editor setup and plugins",
            content="Current editor setup and plugins.",
            type=type_,
            source_app="test",
            allow_duplicate=True,
        )
    await pool.execute(
        "UPDATE memories SET updated_at = now() - interval '100 days' "
        "WHERE name = 'editor-setup-old'"
    )
    results = {m.name: m for m in await store.recall("editor setup plugins")}
    assert results["editor-setup-old"].possibly_stale
    assert not results["editor-setup-new"].possibly_stale


async def test_consolidation_report(store: MemoryStore, pool):
    await store.remember(
        name="uv-pref",
        description="Tim prefers uv over pip",
        content="Use uv for Python dependency management.",
        type="preference",
        source_app="test",
    )
    await store.remember(
        name="uv-pref-2",
        description="Tim prefers uv over pip",
        content="Use uv for Python dependency management.",
        type="preference",
        source_app="test",
        allow_duplicate=True,
    )
    await store.remember(
        name="old-editor",
        description="editor Tim uses",
        content="Uses Zed.",
        type="state",
        source_app="test",
    )
    await pool.execute(
        "UPDATE memories SET updated_at = now() - interval '400 days', last_used_at = NULL "
        "WHERE name = 'old-editor'"
    )
    await store.remember(
        name="lonely-note",
        description="a note nobody looks up",
        content="Something never recalled.",
        type="fact",
        source_app="test",
    )
    await pool.execute(
        "UPDATE memories SET created_at = now() - interval '400 days' WHERE name = 'lonely-note'"
    )

    report = await store.consolidation_candidates(stale_state_days=90, zombie_days=60)

    assert frozenset(("uv-pref", "uv-pref-2")) in {
        frozenset((d.name_a, d.name_b)) for d in report.duplicates
    }
    assert "old-editor" in {s.name for s in report.stale_state}
    assert "lonely-note" in {z.name for z in report.zombies}


async def test_remember_stores_and_updates_related(store: MemoryStore):
    await store.remember(
        name="axiom-goal",
        description="Axiom goal",
        content="Cross-app shared memory.",
        type="project",
        source_app="test",
    )
    created = await store.remember(
        name="axiom-arch",
        description="Axiom architecture",
        content="Postgres is the source of truth.",
        type="project",
        source_app="test",
        related=["axiom-goal"],
    )
    assert created.memory is not None
    assert created.memory.related == ["axiom-goal"]

    fetched = await store.get("axiom-arch")
    assert fetched is not None
    assert fetched.related == ["axiom-goal"]

    updated = await store.remember(
        name="axiom-arch",
        description="Axiom architecture",
        content="Postgres is the source of truth; markdown is a read-only mirror.",
        type="project",
        source_app="test",
        related=["axiom-goal", "axiom-decision-postgresql"],
    )
    assert updated.memory is not None
    assert updated.memory.related == ["axiom-goal", "axiom-decision-postgresql"]


async def test_forget_scrubs_related_links(store: MemoryStore):
    await store.remember(
        name="axiom-goal",
        description="Axiom goal",
        content="Cross-app shared memory.",
        type="project",
        source_app="test",
    )
    await store.remember(
        name="axiom-arch",
        description="Axiom architecture",
        content="Postgres is the source of truth.",
        type="project",
        source_app="test",
        related=["axiom-goal"],
    )
    assert await store.forget("axiom-goal") is True

    survivor = await store.get("axiom-arch")
    assert survivor is not None
    assert survivor.related == []


async def test_index_nearest_is_bounded_and_ordered(store: MemoryStore):
    await store.remember(
        name="favorite-coffee",
        description="Coffee preference",
        content="Drinks oat milk lattes.",
        type="fact",
        source_app="test",
    )
    await store.remember(
        name="axiom-project",
        description="Axiom memory MCP server",
        content="Side project: MCP memory server.",
        type="project",
        source_app="test",
    )
    await store.remember(
        name="kube-notes",
        description="kubernetes cluster notes",
        content="Cluster configuration notes.",
        type="fact",
        source_app="test",
    )
    nearest = await store.index_nearest("coffee drinks", limit=2)
    assert len(nearest) == 2
    assert nearest[0].name == "favorite-coffee"


async def test_zombies_include_barely_used_memories(store: MemoryStore, pool):
    for name in ("barely-used", "well-used", "fresh-low-use"):
        await store.remember(
            name=name,
            description=f"note {name}",
            content=f"Content for {name}.",
            type="fact",
            source_app="test",
            allow_duplicate=True,
        )
    # Old and surfaced only twice → zombie despite nonzero use_count.
    await pool.execute(
        "UPDATE memories SET created_at = now() - interval '100 days', "
        "last_used_at = now() - interval '100 days', use_count = 2 "
        "WHERE name = 'barely-used'"
    )
    # Old but genuinely used → not a zombie.
    await pool.execute(
        "UPDATE memories SET created_at = now() - interval '100 days', "
        "last_used_at = now() - interval '100 days', use_count = 10 "
        "WHERE name = 'well-used'"
    )

    report = await store.consolidation_candidates(
        stale_state_days=90, zombie_days=60, zombie_max_use_count=2
    )
    zombie_names = {z.name for z in report.zombies}
    assert "barely-used" in zombie_names
    assert "well-used" not in zombie_names
    assert "fresh-low-use" not in zombie_names


async def test_forget(store: MemoryStore):
    await store.remember(
        name="to-be-deleted",
        description="Temporary",
        content="Delete me.",
        type="state",
        source_app="test",
    )
    assert await store.forget("to-be-deleted") is True
    assert await store.forget("to-be-deleted") is False
    assert await store.get("to-be-deleted") is None


async def test_remember_surfaces_neighbors_on_create(store: MemoryStore):
    # A new project note that is similar to the existing plan, but not a
    # duplicate, comes back with the plan as an advisory neighbour so the writer
    # can revise it instead of leaving two records that disagree.
    await store.remember(
        name="gateway-plan",
        description="Gateway rollout execution plan",
        content="Gateway rollout execution plan: machine type n2-standard-8.",
        type="project",
        source_app="test",
    )
    result = await store.remember(
        name="gateway-progress",
        description="Gateway rollout progress so far",
        content="Gateway rollout progress: the first node is serving traffic.",
        type="project",
        source_app="test",
    )
    assert result.status == "created"
    assert "gateway-plan" in {n.name for n in result.neighbors}
    # Neighbours are index summaries, not full bodies.
    assert all(not hasattr(n, "content") for n in result.neighbors)


async def test_overview_behind_its_details_is_flagged_stale(store: MemoryStore, pool):
    # A project overview whose linked detail has moved on past it is flagged
    # possibly_stale on both get and recall; the detail itself is not.
    await store.remember(
        name="widget-overview",
        description="Widget project overview and status",
        content="Widget project overview: status, decisions, open items.",
        type="project",
        source_app="test",
        related=["widget-arch"],
    )
    await store.remember(
        name="widget-arch",
        description="Widget architecture decision",
        content="Widget runs on a single-node Postgres.",
        type="project",
        source_app="test",
        allow_duplicate=True,
    )
    # Pin the overview behind its detail deterministically.
    await pool.execute(
        "UPDATE memories SET updated_at = now() - interval '2 days' WHERE name = 'widget-overview'"
    )

    overview = await store.get("widget-overview")
    detail = await store.get("widget-arch")
    assert overview is not None and overview.possibly_stale
    assert detail is not None and not detail.possibly_stale

    recalled = {m.name: m for m in await store.recall("widget")}
    assert recalled["widget-overview"].possibly_stale


async def test_review_reports_drift_and_orphans(store: MemoryStore, pool):
    await store.remember(
        name="proj-overview",
        description="Project overview and status",
        content="Project overview: status, decisions in force, open items.",
        type="project",
        source_app="test",
        related=["proj-arch"],
    )
    await store.remember(
        name="proj-arch",
        description="Project architecture decision",
        content="Chosen architecture: event-sourced core.",
        type="project",
        source_app="test",
        allow_duplicate=True,
    )
    await store.remember(
        name="proj-notes",
        description="Loose project progress note",
        content="Deployed the staging environment yesterday.",
        type="project",
        source_app="test",
        allow_duplicate=True,
    )
    # Overview predates its linked detail → drift; proj-notes shares the prefix
    # but is not linked from the overview → orphan.
    await pool.execute(
        "UPDATE memories SET updated_at = now() - interval '2 days' WHERE name = 'proj-overview'"
    )

    report = await store.consolidation_candidates(stale_state_days=90, zombie_days=60)

    drift = {d.name: d for d in report.drifted_overviews}
    assert "proj-overview" in drift
    assert "proj-arch" in drift["proj-overview"].newer_related

    orphan_names = {o.name for o in report.orphans}
    assert "proj-notes" in orphan_names
    assert "proj-arch" not in orphan_names  # linked, so not orphaned
    assert "proj-overview" not in orphan_names  # the overview itself never orphans


async def test_forgotten_overview_leaves_its_members_headless(store: MemoryStore):
    async def write(name: str, content: str, **kw) -> None:
        await store.remember(
            name=name,
            description=content,
            content=content,
            type="procedural",
            source_app="test",
            allow_duplicate=True,
            **kw,
        )

    await write("acme-wbs-format", "Acme WBS: three levels, numbered 1.1.1.")
    await write("acme-sow-structure", "Acme SOW sections: scope, milestones, acceptance.")
    await write("wbs-craft", "Define done for every work package before estimating.")
    await write(
        "acme-overview",
        "Job at Acme: status, decisions in force, open items.",
        related=["acme-wbs-format", "wbs-craft"],
    )
    # Still-live overviews keep their members out of the bucket.
    report = await store.consolidation_candidates(stale_state_days=90, zombie_days=60)
    assert report.headless == []

    assert await store.forget("acme-overview") is True
    left = {m.name for m in await store.left_behind("acme-overview")}
    # Prefix members (linked or not) and linked non-prefix members are left behind.
    assert left == {"acme-wbs-format", "acme-sow-structure", "wbs-craft"}

    # Re-homing a memory under another live overview takes it out of the bucket.
    await write("craft-overview", "Personal methodology overview.", related=["wbs-craft"])
    report = await store.consolidation_candidates(stale_state_days=90, zombie_days=60)
    assert {m.name for m in report.headless} == {"acme-wbs-format", "acme-sow-structure"}

    # Retiring the leftovers empties it.
    await store.forget("acme-wbs-format")
    await store.forget("acme-sow-structure")
    report = await store.consolidation_candidates(stale_state_days=90, zombie_days=60)
    assert report.headless == []


async def test_list_all(store: MemoryStore):
    await store.remember(
        name="a-memory",
        description="First",
        content="One.",
        type="fact",
        source_app="test",
    )
    summaries = await store.list_all()
    assert [s.name for s in summaries] == ["a-memory"]
    assert summaries[0].category == "topics"


async def test_remember_category_assignment_and_inference(store: MemoryStore):
    pref = await store.remember(
        name="prefers-dark-mode",
        description="Dark mode preference",
        content="Prefers dark mode across all editors.",
        type="preference",
        source_app="test",
    )
    assert pref.memory is not None
    assert pref.memory.category == "you"

    proj = await store.remember(
        name="acme-agent-pilot",
        description="Acme Bank AI Agent",
        content="Sales agent production pilot for Acme Bank.",
        type="project",
        source_app="test",
    )
    assert proj.memory is not None
    assert proj.memory.category == "areas"

    person = await store.remember(
        name="dana-sales-lead",
        description="Dana sales lead at Globex",
        content="Dana is financial-industry sales lead at Globex.",
        type="fact",
        category="people",
        source_app="test",
    )
    assert person.memory is not None
    assert person.memory.category == "people"


async def test_recall_and_list_filter_by_category(store: MemoryStore):
    await store.remember(
        name="dana-contact",
        description="Dana contact at Globex",
        content="Dana leads finance accounts at Globex.",
        type="fact",
        category="people",
        source_app="test",
    )
    await store.remember(
        name="dana-project-notes",
        description="Notes for Dana project proposal",
        content="Proposal document for Dana regarding GCP migration.",
        type="fact",
        category="areas",
        source_app="test",
    )

    people_recall = await store.recall("Dana", category="people")
    assert len(people_recall) == 1
    assert people_recall[0].name == "dana-contact"

    areas_recall = await store.recall("Dana", category="areas")
    assert len(areas_recall) == 1
    assert areas_recall[0].name == "dana-project-notes"

    people_list = await store.list_all(category="people")
    assert [s.name for s in people_list] == ["dana-contact"]


async def test_export_writes_and_prunes(store: MemoryStore, tmp_path: Path):
    await store.remember(
        name="kept-memory",
        description="Stays",
        content="Content here.",
        type="fact",
        category="topics",
        source_app="test",
    )
    (tmp_path / "stale-memory.md").write_text("old")

    count = await export_all(store, tmp_path)

    assert count == 1
    assert (tmp_path / "topics" / "kept-memory.md").exists()
    assert not (tmp_path / "stale-memory.md").exists()
    index = (tmp_path / "MEMORY.md").read_text()
    assert "## Topics" in index
    assert "[kept-memory](topics/kept-memory.md)" in index
    body = (tmp_path / "topics" / "kept-memory.md").read_text()
    assert body.startswith("---\nname: kept-memory\n")
    assert "category: topics\n" in body
    assert "Content here." in body


async def test_update_without_category_or_related_keeps_them(store: MemoryStore):
    await store.remember(
        name="morning-routine",
        description="How Tim starts the day",
        content="Coffee, then email.",
        type="procedural",
        category="you",
        source_app="test",
        related=["user-profile"],
    )
    result = await store.remember(
        name="morning-routine",
        description="How Tim starts the day",
        content="Coffee, then a walk.",
        type="procedural",
        source_app="test",
    )
    assert result.memory is not None
    assert result.memory.category == "you" and result.memory.related == ["user-profile"]
    cleared = await store.remember(
        name="morning-routine",
        description="How Tim starts the day",
        content="Coffee, then a walk.",
        type="procedural",
        source_app="test",
        related=[],
    )
    assert cleared.memory is not None and cleared.memory.related == []


async def test_remember_reports_missing_links(store: MemoryStore):
    await store.remember(
        name="bank-main",
        description="Main account",
        content="Main account.",
        type="fact",
        source_app="test",
    )
    result = await store.remember(
        name="savings-plan",
        description="Where savings go each month",
        content="Salary goes to [[bank-main]]; spending per [[budget-rules]].",
        type="project",
        source_app="test",
        related=["bank-main", "bank-joint"],
    )
    assert result.status == "created"
    assert result.missing_links == ["budget-rules", "bank-joint"]


async def test_supersedes_archives_and_relinks(store: MemoryStore):
    await store.remember(
        name="main-account-overview",
        description="Main account overview",
        content="Salary lands in the main bank account.",
        type="project",
        source_app="test",
    )
    await store.remember(
        name="tax-filing",
        description="Yearly tax filing steps",
        content="Export statements from [[main-account-overview]].",
        type="project",
        source_app="test",
        related=["main-account-overview"],
    )
    result = await store.remember(
        name="bank-main",
        description="Main account overview",
        content="Salary lands in the main bank account.",
        type="project",
        source_app="test",
        supersedes=["main-account-overview"],
    )
    assert result.status == "created"
    assert result.superseded == ["main-account-overview"] and result.relinked == ["tax-filing"]
    assert result.missing_links == []
    old = await store.get("main-account-overview")
    assert old is not None and old.archived
    filing = await store.get("tax-filing")
    assert filing is not None
    assert filing.content == "Export statements from [[bank-main]]."
    assert filing.related == ["bank-main"]
    # The rewrite is versioned like any content change, so it can be undone.
    [before] = await store.history("tax-filing")
    assert before.content == "Export statements from [[main-account-overview]]."


async def test_supersedes_unknown_name_writes_nothing(store: MemoryStore):
    with pytest.raises(LookupError, match="no-such-memory"):
        await store.remember(
            name="fresh-fact",
            description="A fact",
            content="Something new.",
            type="fact",
            source_app="test",
            supersedes=["no-such-memory"],
        )
    assert await store.get("fresh-fact") is None


async def test_edit_changes_only_the_matched_text(store: MemoryStore):
    await store.remember(
        name="bank-notes",
        title="Bank 錢包",
        description="Bank account notes",
        content="Bank 錢包 holds the savings. The 錢包 is shared.",
        type="fact",
        category="areas",
        source_app="test",
        related=["bank-main"],
    )
    result = await store.edit(
        "bank-notes",
        [
            ContentEdit(old="Bank 錢包", new="Bank 帳戶"),
            ContentEdit(old="The 錢包", new="The 帳戶"),
        ],
        source_app="other-app",
    )
    assert result is not None and result.missing_links == ["bank-main"]
    memory = await store.get("bank-notes")
    assert memory is not None
    assert memory.content == "Bank 帳戶 holds the savings. The 帳戶 is shared."
    assert (memory.title, memory.category, memory.related) == (
        "Bank 錢包",
        "areas",
        ["bank-main"],
    )
    assert memory.source_app == "other-app"

    relinked = await store.edit("bank-notes", [], source_app="test", related=[])
    assert relinked is not None and relinked.missing_links == []


async def test_edit_is_all_or_nothing(store: MemoryStore):
    await store.remember(
        name="bank-notes",
        description="Bank account notes",
        content="錢包 one, 錢包 two.",
        type="fact",
        source_app="test",
    )
    with pytest.raises(ValueError, match="occurs 2 times"):
        await store.edit(
            "bank-notes",
            [ContentEdit(old="one", new="1"), ContentEdit(old="錢包", new="帳戶")],
            source_app="test",
        )
    memory = await store.get("bank-notes")
    assert memory is not None and memory.content == "錢包 one, 錢包 two."
    assert await store.edit("no-such-memory", [], source_app="test") is None


async def test_forget_unwraps_content_links(store: MemoryStore):
    await store.remember(
        name="old-plan",
        description="Old plan",
        content="Superseded plan.",
        type="project",
        source_app="test",
    )
    await store.remember(
        name="new-plan",
        description="New plan",
        content="Replaces [[old-plan]] entirely.",
        type="project",
        source_app="test",
    )
    assert await store.forget("old-plan") is True
    survivor = await store.get("new-plan")
    assert survivor is not None and survivor.content == "Replaces old-plan entirely."
