"""The page shell every /app page shares, and the static files it links.

The stylesheet and script are read once at import and served with a content
hash in their URL, so a phone keeps them cached until they actually change.
"""

import hashlib
import json
from html import escape
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

_STATIC = Path(__file__).parent / "static"
_FONTS = "https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400..700&display=swap"
_SKELETON = '<div class="skel" aria-hidden="true"><i class="h"></i>' + "<i></i>" * 4 + "</div>"
_TABS = [
    ("today", "/app", "今天"),
    ("week", "/app/week", "本週"),
    ("projects", "/app/projects", "專案"),
    ("library", "/app/library", "資料"),
    ("me", "/app/me", "我"),
]
_MANIFEST = json.dumps(
    {
        "name": "Axiom",
        "short_name": "Axiom",
        "start_url": "/app",
        "scope": "/app",
        "display": "standalone",
        "background_color": "#FFFFFF",
        "theme_color": "#FFFFFF",
        "icons": [
            {"src": "/app/icon-180.png", "sizes": "180x180", "type": "image/png"},
            {
                "src": "/app/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
)


class _Asset:
    def __init__(self, filename: str, media_type: str) -> None:
        self.body = (_STATIC / filename).read_bytes()
        self.media_type = media_type
        self.path = f"/app/{filename}"
        self.url = f"{self.path}?v={hashlib.sha256(self.body).hexdigest()[:12]}"


_CSS = _Asset("app.css", "text/css")
_JS = _Asset("app.js", "text/javascript")
_ICONS = [_Asset("icon-180.png", "image/png"), _Asset("icon-512.png", "image/png")]


def page(title: str, active: str, body: str) -> HTMLResponse:
    nav = "".join(
        f'<a href="{href}"{" class=on aria-current=page" if key == active else ""}>{label}</a>'
        for key, href, label in _TABS
    )
    return HTMLResponse(
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        "<meta name='robots' content='noindex'>"
        "<meta name='theme-color' content='#FFFFFF'>"
        "<meta name='mobile-web-app-capable' content='yes'>"
        "<meta name='apple-mobile-web-app-capable' content='yes'>"
        "<meta name='apple-mobile-web-app-title' content='Axiom'>"
        "<link rel='manifest' href='/app/manifest.webmanifest' crossorigin='use-credentials'>"
        "<link rel='icon' href='/app/icon-180.png'>"
        "<link rel='apple-touch-icon' href='/app/icon-180.png'>"
        f"<title>{escape(title)} · Axiom</title>"
        f"<link rel='stylesheet' href='{_FONTS}'><link rel='stylesheet' href='{_CSS.url}'>"
        "</head>"
        f"<body><header><div class='bar'><span class='brand'>Axiom</span><nav>{nav}</nav>"
        f"</div><div class='progress'></div></header><main>{body}{_SKELETON}</main>"
        f"<script src='{_JS.url}'></script></body></html>"
    )


def asset_routes() -> list[Route]:
    """The stylesheet, script, manifest and icons. They hold nothing private
    and the browser's install flow may fetch them without the session, so
    they skip the sign-in check."""

    def serve(asset: _Asset, cache: str) -> Route:
        async def endpoint(request: Request) -> Response:
            return Response(
                asset.body, media_type=asset.media_type, headers={"cache-control": cache}
            )

        return Route(asset.path, endpoint, methods=["GET"])

    async def manifest(request: Request) -> Response:
        return Response(_MANIFEST, media_type="application/manifest+json")

    # The CSS and JS URLs change with their content, so they can be cached for good.
    return [
        Route("/app/manifest.webmanifest", manifest, methods=["GET"]),
        *(serve(icon, "max-age=86400") for icon in _ICONS),
        *(serve(code, "max-age=31536000, immutable") for code in (_CSS, _JS)),
    ]
