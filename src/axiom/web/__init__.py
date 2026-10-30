"""The /app pages: a phone-first work home over tasks and project overviews.

The home page exists to answer one question — "is everything taken care
of?" — so it leads with a one-line status (how many today, how many already
arranged for later, how many still need a day) and then lists only what
needs a look now. Tasks can be added, edited, finished and undone here;
documents are shown read-only. Memories can be read, traced (who wrote
them, how often recall used them, past versions, what links to them) and
forgotten; rewriting them stays with the AI clients. The review page turns
`review`'s report into an inbox: forget an item, or keep it and stop seeing
it. The "我" page shows the `user-profile` memory, which the user's AI
clients curate on request, plus connected apps, settings and a backup.

Server-rendered HTML with plain forms (POST, then redirect back): no build
step, no client state, works on any phone browser. A manifest and icon let
the page be added to a phone's home screen.
"""

from axiom.web.routes import build_routes

__all__ = ["build_routes"]
