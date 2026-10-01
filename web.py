"""Tiny web server: /play/<token> (HTML5 player) and /stream/<token> (Range-aware proxy)."""
import asyncio
import html
import logging
import mimetypes
import secrets
import time

import aiohttp
from aiohttp import web

import config
from terabox import FileItem, TeraBoxClient, TeraError

log = logging.getLogger("web")
_streams: dict[str, dict] = {}
_slots = asyncio.Semaphore(config.MAX_STREAMS)
DLINK_MAX_AGE = 10 * 60  # re-resolve links older than this (they expire)


def create_stream(item: FileItem) -> str:
    now = time.time()
    for k in [k for k, v in _streams.items() if v["exp"] < now]:
        _streams.pop(k, None)
    token = secrets.token_urlsafe(16)
    _streams[token] = {"item": item, "exp": now + config.STREAM_TTL_SEC, "fetched": now}
    return token


def play_url(token: str) -> str:
    return f"{config.PUBLIC_BASE_URL}/play/{token}"


def _get(request: web.Request) -> dict:
    entry = _streams.get(request.match_info["token"])
    if not entry or entry["exp"] < time.time():
        raise web.HTTPNotFound(text="Link expired. Send the TeraBox link to the bot again.")
    return entry


async def _refresh(tb: TeraBoxClient, entry: dict) -> None:
    item: FileItem = entry["item"]
    files = await tb.resolve(item.source_url)
    for f in files:
        if f.fs_id == item.fs_id and f.dlink:
            item.dlink = f.dlink
            entry["fetched"] = time.time()
            return
    raise TeraError("Could not refresh the video link.")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title}</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>html,body{{margin:0;height:100%;background:#000;color:#ddd;font-family:system-ui}}
video{{width:100%;height:100%;background:#000}}#e{{display:none;padding:24px}}</style></head>
<body><video id="v" src="/stream/{token}" controls autoplay playsinline preload="metadata"></video>
<div id="e">This video format can't be played in the browser (often MKV/HEVC).</div>
<script>try{{Telegram.WebApp.ready();Telegram.WebApp.expand()}}catch(e){{}}
document.getElementById('v').addEventListener('error',function(){{this.style.display='none';
document.getElementById('e').style.display='block'}});</script></body></html>"""


async def play(request: web.Request) -> web.Response:
    entry = _get(request)
    page = PAGE.format(title=html.escape(entry["item"].name), token=request.match_info["token"])
    return web.Response(text=page, content_type="text/html")


async def stream(request: web.Request) -> web.StreamResponse:
    entry = _get(request)
    tb: TeraBoxClient = request.app["tb"]
    item: FileItem = entry["item"]
    rng = request.headers.get("Range")
    async with _slots:
        upstream = None
        try:
            for attempt in range(2):
                if time.time() - entry["fetched"] > DLINK_MAX_AGE or attempt == 1:
                    await _refresh(tb, entry)
                upstream = await tb.open_stream(item.dlink, rng)
                if upstream.status < 400:
                    break
                upstream.release()
                upstream = None
            if upstream is None:
                raise web.HTTPBadGateway(text="Upstream refused the request.")

            ctype = upstream.headers.get("Content-Type", "")
            if not ctype.startswith("video/"):
                ctype = mimetypes.guess_type(item.name)[0] or "video/mp4"
            headers = {"Content-Type": ctype, "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
            for h in ("Content-Length", "Content-Range"):
                if h in upstream.headers:
                    headers[h] = upstream.headers[h]
            resp = web.StreamResponse(status=upstream.status, headers=headers)
            await resp.prepare(request)
            try:
                async for chunk in upstream.content.iter_chunked(256 * 1024):
                    await resp.write(chunk)
            except (ConnectionResetError, asyncio.TimeoutError, aiohttp.ClientError):
                pass  # viewer seeked/closed
            return resp
        except (TeraError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise web.HTTPBadGateway(text=str(e) or 'Upstream error')
        finally:
            if upstream is not None:
                upstream.release()


def make_app(tb: TeraBoxClient) -> web.Application:
    app = web.Application()
    app["tb"] = tb
    app.add_routes([web.get("/play/{token}", play), web.get("/stream/{token}", stream),
                    web.get("/health", lambda r: web.Response(text="ok"))])
    return app
