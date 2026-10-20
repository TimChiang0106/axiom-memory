"""The /app pages: nothing is readable without auth (Access JWT or service
token), task actions work through the forms, cross-site posts are refused."""

import io
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import asyncpg
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette

from axiom.auth import AppAuth
from axiom.documents import DocumentStore
from axiom.store import MemoryStore
from axiom.tasks import TaskError, TaskStore
from axiom.web import build_routes

TOKEN = "page-token"
TEAM = "https://team.cloudflareaccess.com"
AUD = "aud-tag"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    def get_signing_key_from_jwt(self, token: str):
        return SimpleNamespace(key=KEY.public_key())


def access_jwt(email: str = "me@example.com", aud: str = AUD, key=KEY) -> str:
    return jwt.encode({"email": email, "aud": aud, "iss": TEAM}, key, algorithm="RS256")


def make_client(pool: asyncpg.Pool, auth: AppAuth) -> httpx.AsyncClient:
    tasks = TaskStore(pool)
    memories = MemoryStore(pool, dedup_threshold=0.35, recall_threshold=0.1)
    documents = DocumentStore(pool)

    async def get_tasks() -> TaskStore:
        return tasks

    async def get_memories() -> MemoryStore:
        return memories

    async def get_documents() -> DocumentStore:
        return documents

    app = Starlette(
        routes=build_routes(
            auth=auth, get_tasks=get_tasks, get_memories=get_memories, get_documents=get_documents
        )
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://axiom.test")


@pytest.fixture
async def client(tasks: TaskStore, pool: asyncpg.Pool):
    auth = AppAuth(
        service_token=TOKEN,
        team_domain=TEAM,
        aud=AUD,
        allowed_emails=frozenset({"me@example.com"}),
        jwks_client=FakeJWKS(),  # type: ignore[arg-type]
    )
    async with make_client(pool, auth) as c:
        yield c


BEARER = {"authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("path", ["/app", "/app/week", "/app/projects", "/app/memory/x"])
async def test_pages_require_auth(client: httpx.AsyncClient, path: str):
    response = await client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/login"


async def test_wrong_credentials_are_refused(client: httpx.AsyncClient):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for headers in (
        {"authorization": "Bearer nope"},
        {"cf-access-jwt-assertion": access_jwt(email="stranger@example.com")},
        {"cf-access-jwt-assertion": access_jwt(aud="other-app")},
        {"cf-access-jwt-assertion": access_jwt(key=other)},
    ):
        assert (await client.get("/app", headers=headers)).status_code == 303
    assert (await client.get("/app", cookies={AppAuth.COOKIE: TOKEN})).status_code == 303


async def test_access_jwt_and_bearer_both_work(client: httpx.AsyncClient):
    ok = await client.get("/app", headers={"cf-access-jwt-assertion": access_jwt()})
    assert ok.status_code == 200
    assert "今天 0 件" in ok.text
    assert (await client.get("/app/projects", headers=BEARER)).status_code == 200


async def test_login_sets_session_cookie(client: httpx.AsyncClient):
    bad = await client.post("/app/login", data={"token": "nope"})
    assert AppAuth.COOKIE not in bad.cookies
    good = await client.post("/app/login", data={"token": TOKEN})
    assert good.status_code == 303
    cookie = good.cookies[AppAuth.COOKIE]
    assert cookie != TOKEN
    page = await client.get("/app/week", cookies={AppAuth.COOKIE: cookie})
    assert page.status_code == 200


async def test_unconfigured_auth_refuses_everything(pool: asyncpg.Pool, tasks: TaskStore):
    async with make_client(pool, AppAuth()) as c:
        assert (await c.get("/app")).status_code == 503
        assert (await c.get("/app/login")).status_code == 503


async def test_task_actions_from_the_page(client: httpx.AsyncClient, tasks: TaskStore):
    task = await tasks.add(title="Loose end")
    page = await client.get("/app", headers=BEARER)
    assert "1 件還沒安排" in page.text

    response = await client.post(
        f"/app/tasks/{task.id}/defer", data={"to": "tomorrow", "next": "/app"}, headers=BEARER
    )
    assert response.status_code == 303
    page = await client.get("/app", headers=BEARER)
    assert "其他 1 件都已排好日期" in page.text
    assert "還沒安排" not in page.text

    await client.post(f"/app/tasks/{task.id}/done", data={}, headers=BEARER)
    done = await tasks.get(task.id)
    assert done is not None and done.status == "done"


async def test_today_orders_by_label_and_drops_the_section_date(
    client: httpx.AsyncClient, tasks: TaskStore
):
    today = await tasks.today()
    for title in ("[有時間再做] Later", "Plain", "[P2] Second", "[P1] First"):
        await tasks.add(title=title, planned_date=today)
    page = (await client.get("/app", headers=BEARER)).text
    order = [page.index(t) for t in ("First", "Second", "Plain", "Later")]
    assert order == sorted(order)
    assert '<span class="tag hi">P1</span>First' in page
    assert "計畫 今天" not in page
    assert "還沒安排" not in page


async def test_cross_origin_post_is_refused(client: httpx.AsyncClient, tasks: TaskStore):
    task = await tasks.add(title="Loose end")
    response = await client.post(
        f"/app/tasks/{task.id}/drop",
        headers={**BEARER, "origin": "https://evil.example"},
    )
    assert response.status_code == 403
    still = await tasks.get(task.id)
    assert still is not None and still.status == "open"


async def test_action_errors_come_back_to_the_page(client: httpx.AsyncClient, tasks: TaskStore):
    task = await tasks.add(title="Loose end")
    await tasks.done(task.id)
    response = await client.post(f"/app/tasks/{task.id}/done", data={}, headers=BEARER)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


async def test_deferred_three_times_is_flagged(client: httpx.AsyncClient, tasks: TaskStore):
    task = await tasks.add(title="Chronic", planned_date=await tasks.today())
    for _ in range(3):
        task = await tasks.defer(task.id, to="someday")
        task = await tasks.update(task.id, planned_date=await tasks.today())
    page = await client.get("/app", headers=BEARER)
    assert task.defer_count == 3
    assert "延了 3 次，還要做嗎？" in page.text  # noqa: RUF001


async def test_quick_add_plans_the_task(client: httpx.AsyncClient, tasks: TaskStore):
    response = await client.post(
        "/app/tasks/new", data={"title": "Call the bank", "when": "tomorrow"}, headers=BEARER
    )
    assert response.status_code == 303
    (task,) = await tasks.find(statuses=["open"])
    assert task.title == "Call the bank"
    assert task.planned_date == await tasks.today() + timedelta(days=1)

    await client.post("/app/tasks/new", data={"title": "Later", "when": "someday"}, headers=BEARER)
    later = [t for t in await tasks.find(statuses=["open"]) if t.title == "Later"]
    assert later[0].someday

    bad = await client.post("/app/tasks/new", data={"title": " ", "when": "today"}, headers=BEARER)
    assert "error=" in bad.headers["location"]


async def test_done_offers_undo_and_reopen_restores(client: httpx.AsyncClient, tasks: TaskStore):
    task = await tasks.add(title="Oops", planned_date=await tasks.today())
    response = await client.post(
        f"/app/tasks/{task.id}/done", data={"next": "/app"}, headers=BEARER
    )
    assert response.headers["location"] == f"/app?undo={task.id}"
    page = await client.get(response.headers["location"], headers=BEARER)
    assert "已完成「Oops」" in page.text
    assert "今天已完成" in page.text

    await client.post(f"/app/tasks/{task.id}/reopen", data={"next": "/app"}, headers=BEARER)
    reopened = await tasks.get(task.id)
    assert reopened is not None and reopened.status == "open" and reopened.done_at is None


async def test_rejected_suggestion_cannot_be_reopened(tasks: TaskStore, store: MemoryStore):
    await store.remember(
        name="p-overview",
        description="p project overview",
        content="x",
        type="project",
        source_app="t",
    )
    task = await tasks.add(
        title="Maybe",
        source="ai_suggested",
        evidence={"trigger": "due_soon", "memories": ["p-overview"]},
    )
    await tasks.reject(task.id, reason="no")
    with pytest.raises(TaskError):
        await tasks.reopen(task.id)


async def test_bulk_moves_every_overdue_task(client: httpx.AsyncClient, tasks: TaskStore):
    today = await tasks.today()
    for title in ("A", "B"):
        await tasks.add(title=title, planned_date=today - timedelta(days=2))
    page = await client.get("/app", headers=BEARER)
    assert "全部移到今天" in page.text
    await client.post("/app/tasks/bulk", data={"to": "today"}, headers=BEARER)
    view = await tasks.today_view()
    assert not view.overdue and len(view.due_today) == 2


async def test_edit_renames_and_clears_dates(client: httpx.AsyncClient, tasks: TaskStore):
    today = await tasks.today()
    task = await tasks.add(title="Typo", planned_date=today, due_date=today)
    page = await client.get(f"/app/tasks/{task.id}?from=/app/week", headers=BEARER)
    assert 'href="/app/week"' in page.text
    await client.post(
        f"/app/tasks/{task.id}/edit",
        data={"title": "Fixed", "planned_date": "", "due_date": "", "project": ""},
        headers=BEARER,
    )
    edited = await tasks.get(task.id)
    assert edited is not None
    assert (edited.title, edited.planned_date, edited.due_date) == ("Fixed", None, None)


async def test_library_project_and_doc_pages(
    client: httpx.AsyncClient, tasks: TaskStore, pool: asyncpg.Pool
):
    store = MemoryStore(pool, dedup_threshold=0.35, recall_threshold=0.1)
    await store.remember(
        name="acme-overview",
        description="Acme project overview",
        type="project",
        content="Status. See doc:acme-spec",
        source_app="t",
    )
    await DocumentStore(pool).put(
        name="acme-spec", title="Acme spec", body="Long text", project="acme", source_app="t"
    )
    await tasks.add(title="Ship console", project="acme")

    project = await client.get("/app/projects/acme", headers=BEARER)
    assert "Ship console" in project.text and "Acme spec" in project.text
    assert 'href="/app/docs/acme-spec"' in project.text
    assert (await client.get("/app/projects/-", headers=BEARER)).status_code == 200
    assert "找不到" in (await client.get("/app/projects/nope", headers=BEARER)).text

    library = await client.get("/app/library", headers=BEARER)
    assert "Acme spec" in library.text and "acme-overview" in library.text
    assert '<a href="/app/library?c=areas">專案與領域<span>' in library.text
    areas = await client.get("/app/library", params={"c": "areas"}, headers=BEARER)
    assert "acme-overview" in areas.text and "Acme spec" not in areas.text
    found = await client.get("/app/library", params={"q": "Acme"}, headers=BEARER)
    assert "acme-overview" in found.text
    doc = await client.get("/app/docs/acme-spec", headers=BEARER)
    assert "Long text" in doc.text


async def test_install_files_need_no_session(client: httpx.AsyncClient):
    manifest = await client.get("/app/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.json()["start_url"] == "/app"
    icon = await client.get("/app/icon-180.png")
    assert icon.status_code == 200 and icon.content.startswith(b"\x89PNG")


async def test_me_page_shows_the_profile_and_what_is_newer(
    client: httpx.AsyncClient, pool: asyncpg.Pool
):
    assert "還沒有個人總覽" in (await client.get("/app/me", headers=BEARER)).text

    store = MemoryStore(pool, dedup_threshold=0.35, recall_threshold=0.1)
    await store.remember(
        name="user-profile",
        description="Who the user is",
        type="fact",
        category="you",
        content="## 身分\nSolutions architect\n\n## 合作\n- **結論先講**\n- 白話",
        source_app="t",
    )
    await store.remember(
        name="likes-tea",
        description="Prefers tea over coffee",
        type="preference",
        category="you",
        content="Tea.",
        source_app="t",
    )
    page = (await client.get("/app/me", headers=BEARER)).text
    assert "<h2>身分</h2>" in page and "<li><strong>結論先講</strong></li>" in page
    assert "有 1 筆關於你的記憶比總覽新" in page
    assert "<h2>偏好" in page and "likes-tea" in page


async def test_memory_page_traces_and_forgets(client: httpx.AsyncClient, pool: asyncpg.Pool):
    store = MemoryStore(pool, dedup_threshold=0.35, recall_threshold=0.1)
    for content in ("First take.", "Second take."):
        await store.remember(
            name="tea-note",
            description="Tea note",
            type="preference",
            category="you",
            content=content,
            source_app="openai-mcp",
        )
    await store.remember(
        name="drinks",
        description="Drinks",
        type="fact",
        content="See tea.",
        related=["tea-note"],
        source_app="claude-user",
    )
    page = (await client.get("/app/memory/tea-note", headers=BEARER)).text
    assert "ChatGPT 最後寫入" in page and "AI 還沒用過" in page
    assert "被這些記憶連結" in page and 'href="/app/memory/drinks"' in page
    assert "歷史版本" in page and "First take." in page

    gone = await client.post(
        "/app/memory/tea-note/forget", data={"next": "/app/library"}, headers=BEARER
    )
    assert gone.status_code == 303 and "forgot=tea-note" in gone.headers["location"]
    assert await store.get("tea-note") is None
    library = await client.get(gone.headers["location"], headers=BEARER)
    assert "已刪除記憶「tea-note」" in library.text


async def test_review_inbox_keeps_what_the_user_keeps(
    client: httpx.AsyncClient, pool: asyncpg.Pool
):
    store = MemoryStore(pool, dedup_threshold=0.35, recall_threshold=0.1)
    await store.remember(
        name="shop-overview",
        description="Shop overview",
        type="project",
        content="S",
        source_app="t",
    )
    await store.remember(
        name="shop-pricing",
        description="Shop pricing",
        type="fact",
        content="P",
        source_app="t",
    )
    page = (await client.get("/app/review", headers=BEARER)).text
    assert "沒連到專案總覽" in page and "shop-pricing" in page

    await client.post(
        "/app/review/dismiss", data={"kind": "orphan", "key": "shop-pricing"}, headers=BEARER
    )
    page = (await client.get("/app/review", headers=BEARER)).text
    assert 'href="/app/memory/shop-pricing"' not in page


async def test_me_page_lists_apps_and_export_downloads(
    client: httpx.AsyncClient, pool: asyncpg.Pool
):
    store = MemoryStore(pool, dedup_threshold=0.35, recall_threshold=0.1)
    await store.remember(
        name="likes-tea",
        description="Tea",
        type="preference",
        category="you",
        content="Tea.",
        source_app="openai-mcp",
    )
    page = (await client.get("/app/me", headers=BEARER)).text
    assert "連線的 AI" in page and "ChatGPT" in page and "1 筆記憶" in page
    assert 'href="/app/export.zip" download' in page

    archive = await client.get("/app/export.zip", headers=BEARER)
    assert archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert {"MEMORY.md", "you/likes-tea.md"} <= set(z.namelist())
