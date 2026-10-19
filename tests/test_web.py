"""The /app pages: nothing is readable without auth (Access JWT or service
token), task actions work through the forms, cross-site posts are refused."""

from types import SimpleNamespace

import asyncpg
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette

from axiom.auth import AppAuth
from axiom.store import MemoryStore
from axiom.tasks import TaskStore
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

    async def get_tasks() -> TaskStore:
        return tasks

    async def get_memories() -> MemoryStore:
        return memories

    app = Starlette(routes=build_routes(auth=auth, get_tasks=get_tasks, get_memories=get_memories))
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
