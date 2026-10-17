"""Command-line entry point: serve (stdio/http), export, migrate, embed, eval, review."""

import argparse
import asyncio
import logging
import os
from pathlib import Path

from axiom.config import get_settings
from axiom.db import create_pool, run_migrations


async def _migrate() -> None:
    pool = await create_pool(get_settings().database_url)
    try:
        await run_migrations(pool)
    finally:
        await pool.close()


async def _export(out_dir: Path) -> None:
    from axiom.export import export_all
    from axiom.server import get_store

    count = await export_all(await get_store(), out_dir)
    print(f"Exported {count} memories to {out_dir}")


async def _embed() -> None:
    from axiom.server import get_store

    embedded, missing = await (await get_store()).backfill_embeddings()
    suffix = f"; {missing} still missing (API errors)" if missing else ""
    print(f"Embedded {embedded} memories{suffix}")


async def _eval(cases_path: Path, k: int) -> None:
    from axiom.evals import format_report, load_cases, run_eval
    from axiom.server import get_store

    report = await run_eval(await get_store(), load_cases(cases_path), k=k)
    print(format_report(report))


async def _review(
    stale_state_days: int,
    zombie_days: int,
    zombie_max_use_count: int,
    event_window_days: int,
    event_min_count: int,
) -> None:
    from axiom.server import get_store

    store = await get_store()
    report = await store.consolidation_candidates(
        stale_state_days=stale_state_days,
        zombie_days=zombie_days,
        zombie_max_use_count=zombie_max_use_count,
        event_window_days=event_window_days,
        event_min_count=event_min_count,
    )

    if report.duplicates:
        print(f"\nNear-duplicates ({len(report.duplicates)}) — consider merging:")
        for d in report.duplicates:
            print(f"  {d.similarity:.2f}  {d.name_a}  ~  {d.name_b}")
    if report.stale_state:
        print(f"\nStale state ({len(report.stale_state)}) — still true?:")
        for s in report.stale_state:
            print(f"  {s.since:%Y-%m-%d}  {s.name}")
    if report.zombies:
        print(f"\nBarely used ({len(report.zombies)}) — worth keeping?:")
        for z in report.zombies:
            print(f"  {z.since:%Y-%m-%d}  {z.name}")
    if report.drifted_overviews:
        print(
            f"\nOverviews behind their details ({len(report.drifted_overviews)}) "
            "— refresh the source of truth?:"
        )
        for o in report.drifted_overviews:
            print(f"  {o.updated_at:%Y-%m-%d}  {o.name}  <  {', '.join(o.newer_related)}")
    if report.orphans:
        print(f"\nOrphaned from an overview ({len(report.orphans)}) — fold in?:")
        for m in report.orphans:
            print(f"  {m.updated_at:%Y-%m-%d}  {m.name}")
    if report.headless:
        print(f"\nLeft behind by a forgotten overview ({len(report.headless)}) — retire?:")
        for m in report.headless:
            print(f"  {m.updated_at:%Y-%m-%d}  {m.name}")
    if report.recurring:
        print(f"\nRecurring events ({len(report.recurring)}) — promote to a memory?:")
        for c in report.recurring:
            print(f"  {c.count}x  {c.first_at:%Y-%m-%d} → {c.last_at:%Y-%m-%d}  {c.samples[0]}")
            for sample in c.samples[1:]:
                print(f"      {sample}")
    if not (
        report.duplicates
        or report.stale_state
        or report.zombies
        or report.drifted_overviews
        or report.orphans
        or report.headless
        or report.recurring
    ):
        print("Nothing to review — memory looks tidy.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = get_settings()

    parser = argparse.ArgumentParser(prog="axiom", description="Personal memory MCP server.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the MCP server.")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default=settings.host)
    # PORT env (Cloud Run / compose) wins over AXIOM_PORT unless --port is given.
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", settings.port)))
    serve.add_argument(
        "--insecure",
        action="store_true",
        help="Allow the HTTP server to start with no auth configured. Do not "
        "use on a networked interface.",
    )

    export = sub.add_parser("export", help="Export all memories to markdown.")
    export.add_argument("--out", type=Path, default=settings.export_dir)

    sub.add_parser("migrate", help="Apply pending database migrations.")

    sub.add_parser("embed", help="Backfill embeddings for memories missing a vector.")

    evaluate = sub.add_parser(
        "eval", help="Measure recall quality against a labelled case file (read-only)."
    )
    evaluate.add_argument("--cases", type=Path, default=Path("evals/recall_cases.toml"))
    evaluate.add_argument("--k", type=int, default=5)

    review = sub.add_parser(
        "review", help="Report consolidation candidates (duplicates, stale, unused)."
    )
    review.add_argument("--stale-state-days", type=int, default=settings.stale_state_days)
    review.add_argument("--zombie-days", type=int, default=settings.zombie_days)
    review.add_argument(
        "--zombie-max-use-count", type=int, default=settings.zombie_max_use_count
    )
    review.add_argument("--event-window-days", type=int, default=settings.event_window_days)
    review.add_argument("--event-min-count", type=int, default=settings.event_min_count)

    args = parser.parse_args()

    if args.command == "migrate":
        asyncio.run(_migrate())
    elif args.command == "embed":
        asyncio.run(_embed())
    elif args.command == "eval":
        asyncio.run(_eval(args.cases, args.k))
    elif args.command == "export":
        asyncio.run(_export(args.out))
    elif args.command == "review":
        asyncio.run(
            _review(
                args.stale_state_days,
                args.zombie_days,
                args.zombie_max_use_count,
                args.event_window_days,
                args.event_min_count,
            )
        )
    elif args.command == "serve":
        if (
            args.transport == "http"
            and not args.insecure
            and not (settings.google_client_id or settings.service_token)
        ):
            parser.error(
                "HTTP transport requires auth: set the AXIOM_GOOGLE_* OAuth "
                "variables and/or AXIOM_SERVICE_TOKEN, or pass --insecure to "
                "run without auth (localhost only)."
            )

        from axiom.server import mcp

        if args.transport == "stdio":
            mcp.run(transport="stdio", show_banner=False)
        else:
            mcp.run(
                transport="http",
                show_banner=False,
                host=args.host,
                port=args.port,
                stateless_http=True,
            )


if __name__ == "__main__":
    main()
