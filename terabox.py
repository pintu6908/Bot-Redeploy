"""TeraBox share-link resolver + downloader.

Uses the same web endpoints the TeraBox site uses (unofficial, may change):
  1. GET  /sharing/link?surl=...   -> HTML containing a jsToken
  2. GET  /share/list?app_id=250528&shorturl=...&jsToken=...  -> file list incl. `dlink`
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import aiofiles
import aiohttp
from yarl import URL

log = logging.getLogger("terabox")

# Domains seen in public TeraBox clients. Extend freely.
DOMAINS = (
    "terabox.com", "terabox.app", "teraboxapp.com", "teraboxshare.com",
    "1024terabox.com", "1024tera.com", "freeterabox.com", "mirrobox.com",
    "nephobox.com", "4funbox.com", "momerybox.com", "tibibox.com",
    "terafileshare.com", "teraboxlink.com", "terasharelink.com", "terabox.fun",
)
API_HOSTS = ("www.terabox.com", "www.terabox.app", "www.1024terabox.com", "www.teraboxapp.com")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_TOKEN_PATTERNS = (
    re.compile(r"fn%28%22([^%\"]+)%22%29"),
    re.compile(r"fn\(\"([0-9A-Fa-f]+)\"\)"),
    re.compile(r"jsToken\"?\s*[:=]\s*\"([0-9A-Fa-f]+)\""),
)


class TeraError(Exception):
    """User-presentable error."""


@dataclass
class FileItem:
    name: str
    size: int
    path: str
    fs_id: str
    dlink: str | None
    is_dir: bool = False


def _is_tb_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in DOMAINS)


def extract_links(text: str) -> list[str]:
    seen, out = set(), []
    for u in _URL_RE.findall(text or ""):
        u = u.rstrip(".,;)")
        if _is_tb_host(urlparse(u).netloc) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_surl(url: str) -> str | None:
    """Return the share id in '/s/1xxxx' form (leading '1' included)."""
    u = urlparse(url)
    m = re.search(r"/s/([\w-]+)", u.path)
    if m:
        return m.group(1)
    qs = parse_qs(u.query)
    if qs.get("surl"):
        return "1" + qs["surl"][0]  # ?surl= form omits the leading "1"
    return None


def surl_candidates(full: str) -> list[str]:
    # Different endpoints accept the id with or without the leading "1"; try both.
    return [full, full[1:]] if full.startswith("1") and len(full) > 1 else [full]


class TeraBoxClient:
    def __init__(self, session: aiohttp.ClientSession, cookie: str = ""):
        self.s = session
        self.cookie = cookie

    def _headers(self, referer: str | None = None) -> dict:
        h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
        if self.cookie:
            h["Cookie"] = self.cookie
        if referer:
            h["Referer"] = referer
        return h

    async def _page_token(self, host: str, surl_q: str) -> tuple[str, str]:
        url = f"https://{host}/sharing/link?surl={surl_q}"
        async with self.s.get(url, headers=self._headers(), allow_redirects=True,
                              timeout=aiohttp.ClientTimeout(total=25)) as r:
            html = await r.text()
            base = f"{r.url.scheme}://{r.url.host}"
        for pat in _TOKEN_PATTERNS:
            m = pat.search(html)
            if m:
                return m.group(1), base
        raise TeraError("Could not read page token (link invalid, deleted, or TeraBox changed).")

    async def _list(self, base: str, token: str, shorturl: str, directory: str | None) -> list[dict]:
        items, page = [], 1
        while True:
            params = {
                "app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0",
                "jsToken": token, "page": str(page), "num": "100", "by": "name",
                "order": "asc", "site_referer": "", "shorturl": shorturl,
                "root": "0" if directory else "1",
            }
            if directory:
                params["dir"] = directory
            async with self.s.get(f"{base}/share/list", params=params,
                                  headers=self._headers(f"{base}/sharing/link?surl={shorturl}"),
                                  timeout=aiohttp.ClientTimeout(total=25)) as r:
                data = await r.json(content_type=None)
            if data.get("errno") not in (0, None):
                raise TeraError(f"TeraBox error code {data.get('errno')} "
                                f"({data.get('errmsg') or data.get('show_msg') or 'no message'}).")
            chunk = data.get("list") or []
            items += chunk
            if len(chunk) < 100:
                return items
            page += 1

    async def resolve(self, url: str, max_files: int = 200) -> list[FileItem]:
        full = parse_surl(url)
        if not full:
            raise TeraError("Unsupported TeraBox link format.")
        last: Exception | None = None
        for host in dict.fromkeys([urlparse(url).netloc, *API_HOSTS]):
            for attempt in range(2):
                try:
                    surl_q = full[1:] if full.startswith("1") else full
                    token, base = await self._page_token(host, surl_q)
                    for sh in surl_candidates(full):
                        try:
                            raw = await self._list(base, token, sh, None)
                        except TeraError as e:
                            last = e
                            continue
                        if raw:
                            return await self._expand(base, token, sh, raw, max_files)
                    break  # token worked but no list; try next host
                except TeraError as e:
                    last = e
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last = e
                    await asyncio.sleep(1.5 * (attempt + 1))
        if isinstance(last, TeraError):
            raise last
        raise TeraError("TeraBox is unreachable right now. Try again later.")

    async def _expand(self, base, token, sh, raw, max_files, depth=0) -> list[FileItem]:
        files: list[FileItem] = []
        for it in raw:
            if len(files) >= max_files:
                break
            if str(it.get("isdir")) == "1":
                if depth < 3:
                    sub = await self._list(base, token, sh, it.get("path"))
                    files += await self._expand(base, token, sh, sub, max_files - len(files), depth + 1)
                continue
            files.append(FileItem(
                name=it.get("server_filename") or "file", size=int(it.get("size") or 0),
                path=it.get("path", ""), fs_id=str(it.get("fs_id", "")), dlink=it.get("dlink"),
            ))
        return files

    async def download(self, item: FileItem, dest: Path,
                       on_progress: Callable[[int, int], Awaitable[None]] | None = None) -> None:
        if not item.dlink:
            raise TeraError("No download link for this file (account cookie may be required).")
        url = item.dlink
        for _ in range(6):  # manual redirects so the cookie is never sent to non-TeraBox hosts
            host = urlparse(url).netloc
            headers = {"User-Agent": UA, "Referer": "https://www.terabox.com/"}
            if self.cookie and _is_tb_host(host):
                headers["Cookie"] = self.cookie
            async with self.s.get(url, headers=headers, allow_redirects=False,
                                  timeout=aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)) as r:
                if r.status in (301, 302, 303, 307, 308):
                    url = str(r.url.join(URL(r.headers["Location"])))
                    continue
                if r.status >= 400:
                    raise TeraError(f"Download server returned HTTP {r.status} (link may have expired).")
                total = int(r.headers.get("Content-Length") or item.size or 0)
                done = 0
                async with aiofiles.open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        await f.write(chunk)
                        done += len(chunk)
                        if on_progress:
                            await on_progress(done, total)
                if total and done < total:
                    raise TeraError("Download was cut short. Please retry.")
                return
        raise TeraError("Too many redirects.")
