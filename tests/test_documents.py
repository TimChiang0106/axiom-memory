"""Documents: upsert by name, version history on overwrite, listing."""

from axiom.documents import DocumentStore


async def test_put_get_and_history(documents: DocumentStore):
    status, doc = await documents.put(
        name="work-os-spec", title="Work OS spec", body="v1", project=None, source_app="a"
    )
    assert status == "created"
    status, doc = await documents.put(
        name="work-os-spec", title="Work OS spec", body="v2", project=None, source_app="b"
    )
    assert status == "updated"
    assert doc.body == "v2"

    fetched = await documents.get("work-os-spec")
    assert fetched is not None and fetched.body == "v2"
    versions = await documents.history("work-os-spec")
    assert [v.body for v in versions] == ["v1"]
    assert versions[0].source_app == "a"


async def test_identical_put_is_not_versioned(documents: DocumentStore):
    for _ in range(2):
        await documents.put(name="notes", title="Notes", body="same", project=None, source_app="a")
    assert await documents.history("notes") == []


async def test_list_filters_by_project(documents: DocumentStore):
    await documents.put(name="a-doc", title="A", body="xx", project="axiom", source_app="t")
    await documents.put(name="b-doc", title="B", body="yyy", project=None, source_app="t")
    assert [d.name for d in await documents.list_all(project="axiom")] == ["a-doc"]
    listed = {d.name: d.chars for d in await documents.list_all()}
    assert listed == {"a-doc": 2, "b-doc": 3}


async def test_documents_stay_out_of_recall(documents: DocumentStore, store):
    await documents.put(
        name="secret-plan",
        title="Quarterly plan",
        body="zebra migration",
        project=None,
        source_app="t",
    )
    assert await store.recall("zebra migration") == []
