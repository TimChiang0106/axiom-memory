# Axiom

**One memory, every AI.**

AI memory as an MCP server — one brain shared by ChatGPT,
Claude (Code / Desktop / claude.ai), Gemini CLI, and any other MCP client.

Every AI app today keeps its own siloed memory: what ChatGPT knows about you,
Claude doesn't, and none of it is yours. Axiom is a self-hosted memory server
you own — your assistants read and write the same curated facts through MCP,
and the data lives in your Postgres, exportable to plain markdown at any time.

Memories are curated facts (one per record), not conversation logs. Postgres is
the source of truth; a markdown export gives you a human-readable, git-friendly
mirror.

## Features

- **Shared across apps** — ChatGPT, claude.ai, Claude Code, Gemini CLI, and any
  MCP client talk to the same memory over streamable HTTP or local stdio.
- **Curated, not logged** — `remember` stores one fact per record with
  server-side dedup screening; no conversation-log landfill.
- **Hybrid recall** — ranks on a weighted blend of lexical match (full-text +
  trigram) and pgvector semantic similarity (Gemini embeddings), decayed by
  recency. CJK-friendly and cross-lingual. Long content is truncated in
  results (`get` returns the full text); on a miss, recall returns the nearest
  index entries so the caller can re-query.
- **Typed and categorised** — each memory has a lifecycle `type` (preference,
  fact, project, state, reference, procedural) that sets how fast it decays,
  and a thematic `category` (you, people, areas, topics) to filter by. Stale
  `state` memories — and overviews a linked detail has moved on past — come
  back flagged so the model re-checks them.
- **Version history** — every content change and every `forget` snapshots the
  previous version, so a bad overwrite by any client is recoverable through
  `history`.
- **Provenance** — a memory is either `stated` by you or `inferred` by an AI.
  Inferred memories rank lower and come back labelled, so a guess is not
  treated as a fact about you until you confirm it.
- **Overviews** — by convention (see the client instructions), each ongoing
  project keeps one `<project>-overview` memory and you keep one
  `user-profile`, so "where does X stand" and "who am I talking to" are one
  read instead of a pile of fragments. The overview is held to its job: it is
  flagged when a linked detail outdates it, and `review` reports stray project
  notes that never got folded in. Forgetting an overview retires its project:
  whatever it left behind is listed right away and stays in `review` until
  forgotten or re-homed.
- **Write with context** — a new memory comes back with its nearest existing
  neighbours (below the duplicate bar), so the writer can revise the plan or
  overview it supersedes instead of leaving two records that quietly disagree.
- **Episodic layer** — `log_event` captures cheap one-line ambient signals that
  never enter recall; `review` clusters recent events and surfaces recurring
  patterns as candidates for promotion to a real memory, alongside
  near-duplicates, stale state, barely-used memories, drifted overviews, and
  orphaned project notes.
- **Tasks, kept out of memory** — to-dos and deadlines live in a `task` list
  with a planned day, a deadline, or "someday", not in overview prose. AI
  suggestions are allowed but rationed server-side: each needs a fixed
  trigger and cited memories, at most a few a day, look-alikes of open tasks
  or recent rejections are refused, and unanswered ones expire.
- **Documents** — long text (specs, plans, notes) goes in `document`:
  fetched by name, versioned, never part of recall. A memory points at one
  with `doc:<name>`.
- **Work home page** — `/app` is a phone-first page over tasks and project
  overviews: today's list with quick add, the week, each project's tasks and
  progress, and a library to search memories and read documents. Tasks can be
  edited, finished and undone there; memories and documents are read-only.
  Installable to a phone's home screen. Guarded by Cloudflare Access and/or
  the service token.
- **Measurable** — `axiom eval` scores recall (hit@k, MRR) against your own
  labelled queries, so ranking changes are tested instead of guessed.
- **Graceful degradation** — no Gemini key, or the embedding API is slow?
  Recall falls back to lexical-only and nothing breaks.
- **Your data, portable** — `axiom export` dumps everything to markdown for a
  git-friendly mirror; `scripts/backup.sh` takes nightly database dumps,
  optionally copied to Cloud Storage.
- **Real auth** — Google OAuth (with Dynamic Client Registration for
  ChatGPT/claude.ai connectors) plus a static service token for headless
  callers, coexisting on the same endpoint.

## Architecture

```
Claude Code / Gemini CLI / any MCP client
        │  streamable HTTP, Authorization: Bearer <token>
        ▼
   axiom MCP server  (FastMCP)
        │
        ▼
   PostgreSQL  ──►  markdown export (git mirror), nightly pg_dump
   (hybrid recall: FTS + trigram + pgvector semantic
    similarity via Gemini embeddings, decayed by recency)
```

MCP tools: `remember`, `recall`, `get`, `forget`, `history`, `list_memories`,
`log_event`, `review`, `task`, `document`.
CLI: `axiom serve`, `axiom migrate`, `axiom export`, `axiom embed`,
`axiom review`, `axiom eval`.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
git clone https://github.com/taimoe-ai/axiom-memory && cd axiom-memory
docker compose up -d          # local Postgres (pgvector)
cp .env.example .env
uv run axiom serve            # stdio MCP server (migrations run on first call)
```

Register with Claude Code (local stdio, no auth needed):

```sh
claude mcp add axiom -- uv --directory /path/to/axiom-memory run axiom serve
```

Then paste [instructions/USAGE.md](instructions/USAGE.md) into each client's
system prompt / custom instructions so models know when to call the tools.

## Connecting clients to a deployed server

Point Claude Code at the HTTP server — it detects the OAuth flow and opens a
browser to log in with Google:

```sh
claude mcp add --transport http axiom https://axiom.example.com/mcp
```

ChatGPT: Settings → Connectors → Add custom connector →
`https://axiom.example.com/mcp` (it discovers OAuth and walks through the
Google login). claude.ai: Settings → Connectors → Add custom connector, same
URL.

Headless scripts (curl, CI, cron) skip OAuth with the service token:

```sh
curl -s https://axiom.example.com/mcp \
  -H "Authorization: Bearer $AXIOM_SERVICE_TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Self-hosting

Runs the app and Postgres together on one box over the compose network:

```sh
cp .env.example .env    # fill in the variables below
docker compose --profile full up -d --build
```

The app serves streamable HTTP on `$PORT` (8080) at `/mcp`. Memory access is
stateless, but the OAuth proxy keeps client registrations and encrypted
upstream tokens in a named volume (`oauth_state`) so restarts don't force
ChatGPT/Claude to re-authenticate.

The OAuth flow needs a public HTTPS endpoint (e.g. a Cloudflare Tunnel) that
forwards `/.well-known/*`, `/authorize`, `/token`, `/register` and
`/auth/callback` in addition to `/mcp`.

### Configuration

| Variable | Purpose |
| --- | --- |
| `AXIOM_DATABASE_URL` | Postgres DSN (compose wires this up for you) |
| `AXIOM_BASE_URL` | Public origin the OAuth endpoints are served from |
| `AXIOM_GOOGLE_CLIENT_ID` / `AXIOM_GOOGLE_CLIENT_SECRET` | Google OAuth web app, redirect URI `<base_url>/auth/callback` |
| `AXIOM_ALLOWED_EMAILS` | Comma-separated allowlist for Google login |
| `AXIOM_SERVICE_TOKEN` | Static bearer token for headless callers |
| `AXIOM_GEMINI_API_KEY` | Enables semantic recall (768-dim Gemini embeddings); empty = lexical-only |
| `AXIOM_TIMEZONE` | IANA time zone that decides what "today" is for tasks (default `UTC`) |
| `AXIOM_CF_ACCESS_TEAM_DOMAIN` / `AXIOM_CF_ACCESS_AUD` | Cloudflare Access team domain and application AUD tag guarding `/app` (see below) |
| `POSTGRES_PASSWORD` | Database password (compose) |
| `AXIOM_BACKUP_GCS_URI` / `AXIOM_BACKUP_GCS_CREDENTIALS` | Optional off-machine copy of each backup (see Backups) |

All three OAuth variables must be set together; leave them empty for local
stdio use, which needs no auth at all. After enabling `AXIOM_GEMINI_API_KEY`
on an existing database, run `axiom embed` once to backfill vectors.

Ranking and review thresholds (lexical/semantic weights, decay half-lives,
truncation length, and so on) have defaults in `src/axiom/config.py`; each can
be overridden with an `AXIOM_`-prefixed environment variable.

### The `/app` pages

`<base_url>/app` shows today's tasks, the week, per-project progress, and a
library of memories and documents. It reads every memory, so it refuses all requests unless one of these holds:

- A valid **Cloudflare Access** JWT for an `AXIOM_ALLOWED_EMAILS` address
  (set `AXIOM_CF_ACCESS_TEAM_DOMAIN` and `AXIOM_CF_ACCESS_AUD`). Recommended.
- The **service token**, as a bearer header or entered once on `/app/login`
  (sets an HttpOnly cookie holding an HMAC of the token).

To put Access in front: Zero Trust → Access → Applications → Add → Self-hosted;
domain = your hostname with **path `app`** (scope it to the path — Access on the
whole hostname would block the MCP clients' OAuth flow); add an Allow policy
for your email; copy the application's AUD tag into `AXIOM_CF_ACCESS_AUD`, and
set `AXIOM_CF_ACCESS_TEAM_DOMAIN=https://<team>.cloudflareaccess.com`.

### Backups

Memories live in a named Docker volume, so they survive restarts and
redeploys — but not `docker compose down -v` or a dead disk.
`scripts/backup.sh` dumps the database with `pg_dump` and keeps the last 14
days; run it nightly from root's crontab:

```sh
30 3 * * * /path/to/axiom-memory/scripts/backup.sh >> /var/log/axiom-backup.log 2>&1
```

Set `AXIOM_BACKUP_GCS_URI` (e.g. `gs://your-bucket/axiom/backups`) and
`AXIOM_BACKUP_GCS_CREDENTIALS` (a service-account key file) in `.env` to also
copy each dump to Cloud Storage with `gcloud`.

## Evaluating recall

Ranking is easy to break and hard to judge by eye. `axiom eval` runs a file of
labelled queries against your store and reports hit@k and MRR, without
touching usage counters:

```sh
cp evals/recall_cases.example.toml evals/recall_cases.toml   # then edit
uv run axiom eval
```

Each case is a query plus the memory names a good recall should surface. Real
case files describe your own memories, so `evals/*.toml` is gitignored apart
from the example.

## Development

```sh
docker compose up -d          # local Postgres
uv run pytest                 # tests (uses a separate axiom_test database)
uv run axiom serve --transport http   # http://127.0.0.1:8080/mcp
uv run axiom export           # dump all memories to ./exports as markdown
```

Auth internals are documented in `src/axiom/auth.py`; schema lives in
`src/axiom/migrations/`.

## Status

Personal project, actively used daily by its author. Issues and PRs are
welcome, but no support or stability guarantees yet — the MCP tool surface may
still change.

## License

[MIT](LICENSE)
