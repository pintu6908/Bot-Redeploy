import asyncio
import html
import logging
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from db import DB
from terabox import VIDEO_EXT, FileItem, TeraBoxClient, TeraError, extract_links

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

db = DB(config.DB_PATH)
dp = Dispatcher()
SEM = asyncio.Semaphore(config.MAX_CONCURRENT)
tb: TeraBoxClient  # set in main()
_last_msg: dict[int, float] = {}
_active: dict[int, int] = {}
_batches: dict[str, tuple[float, int, list[FileItem]]] = {}  # token -> (ts, owner, files)


def human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)[:120] or "file"


def gc_batches():
    now = time.time()
    for k in [k for k, v in _batches.items() if now - v[0] > config.CACHE_TTL_SEC]:
        _batches.pop(k, None)


async def safe_edit(msg: Message, text: str, **kw):
    try:
        await msg.edit_text(text, **kw)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
    except TelegramBadRequest:
        pass  # "message is not modified" etc.


@dp.message(CommandStart())
async def start(m: Message):
    await db.touch_user(m.from_user.id, m.from_user.username)
    await m.answer("👋 Send me a TeraBox link and I'll fetch the file(s) for you.\n"
                   f"Upload limit: <b>{config.MAX_UPLOAD_MB} MB</b> (bigger files get a direct link).")


@dp.message(Command("help"))
async def help_(m: Message):
    await m.answer("Paste one or more TeraBox share links (terabox.com, 1024terabox.com, "
                   "teraboxapp.com, terabox.app, …). Folders are supported.")


def admin_only(m: Message) -> bool:
    return m.from_user.id in config.ADMIN_IDS


@dp.message(Command("stats"), admin_only)
async def stats(m: Message):
    s = await db.stats()
    await m.answer("\n".join(f"{k}: <b>{v}</b>" for k, v in s.items()))


@dp.message(Command("ban", "unban"), admin_only)
async def ban(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Usage: /ban &lt;user_id&gt;")
    await db.set_ban(int(command.args), command.command == "ban")
    await m.answer("Done.")


@dp.message(Command("broadcast"), admin_only)
async def broadcast(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Usage: /broadcast &lt;text&gt;")
    ok = 0
    for uid in await db.all_user_ids():
        try:
            await m.bot.send_message(uid, command.args)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception:
            pass
        await asyncio.sleep(0.05)  # stay under ~20 msg/s
    await m.answer(f"Sent to {ok} users.")


@dp.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    await db.touch_user(uid, m.from_user.username)
    if await db.is_banned(uid):
        return
    links = extract_links(m.text)
    if not links:
        return await m.answer("Please send a valid TeraBox share link.")
    now = time.time()
    if now - _last_msg.get(uid, 0) < config.USER_COOLDOWN_SEC:
        return await m.answer("⏳ Slow down a little.")
    _last_msg[uid] = now
    if _active.get(uid, 0) >= 2:
        return await m.answer("You already have downloads running. Wait for them to finish.")

    for link in links[:3]:
        status = await m.answer("🔎 Resolving link…")
        try:
            files = await tb.resolve(link)
        except TeraError as e:
            await safe_edit(status, f"❌ {html.escape(str(e))}")
            continue
        except Exception:
            log.exception("resolve failed for %s", link)
            await safe_edit(status, "❌ Unexpected error while reading the link.")
            continue
        if not files:
            await safe_edit(status, "❌ No files found in this share.")
            continue
        if len(files) == 1:
            asyncio.create_task(process(m, files[0], status))
            continue
        gc_batches()
        token = secrets.token_urlsafe(6)
        _batches[token] = (time.time(), uid, files)
        kb = InlineKeyboardBuilder()
        for i, f in enumerate(files[:config.MAX_FILES_PER_LINK]):
            kb.button(text=f"{f.name[:40]} ({human(f.size)})", callback_data=f"dl:{token}:{i}")
        kb.button(text=f"⬇️ First {min(len(files), config.MAX_FILES_PER_LINK)} files", callback_data=f"dl:{token}:all")
        kb.adjust(1)
        extra = f"\n(+{len(files) - config.MAX_FILES_PER_LINK} more not shown)" if len(files) > config.MAX_FILES_PER_LINK else ""
        await safe_edit(status, f"📁 Found <b>{len(files)}</b> files. Pick one:{extra}", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("dl:"))
async def on_pick(c: CallbackQuery):
    _, token, which = c.data.split(":")
    entry = _batches.get(token)
    if not entry or time.time() - entry[0] > config.CACHE_TTL_SEC:
        return await c.answer("Expired — please send the link again.", show_alert=True)
    if entry[1] != c.from_user.id:
        return await c.answer("Not your request.", show_alert=True)
    files = entry[2][:config.MAX_FILES_PER_LINK]
    chosen = files if which == "all" else [files[int(which)]]
    await c.answer("Queued")
    for f in chosen:
        status = await c.message.answer(f"⏳ Queued: {html.escape(f.name)}")
        asyncio.create_task(process(c.message, f, status, user_id=c.from_user.id))


async def process(m: Message, item: FileItem, status: Message, user_id: int | None = None):
    uid = user_id or m.from_user.id
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    name = html.escape(item.name)
    if not item.dlink or item.size > limit:
        why = "too large for Telegram upload" if item.size > limit else "ready"
        text = f"📄 <b>{name}</b> ({human(item.size)}) — {why}."
        if item.dlink:
            text += f"\n\n<a href=\"{html.escape(item.dlink, quote=True)}\">Direct download link</a> (expires in a few hours)"
        else:
            text += "\nNo direct link available (TeraBox may require a logged-in cookie)."
        return await safe_edit(status, text, disable_web_page_preview=True)

    _active[uid] = _active.get(uid, 0) + 1
    tmp = Path(tempfile.mkdtemp(prefix="tb_"))
    ok = False
    try:
        async with SEM:
            path = tmp / safe_name(item.name)
            last = 0.0

            async def prog(done: int, total: int):
                nonlocal last
                if time.time() - last < 3:
                    return
                last = time.time()
                pct = f"{done * 100 // total}%" if total else human(done)
                await safe_edit(status, f"⬇️ Downloading <b>{name}</b>… {pct}")

            await tb.download(item, path, prog)
            await safe_edit(status, f"⬆️ Uploading <b>{name}</b>…")
            caption = f"{name}\n{human(item.size)}"
            file = FSInputFile(path, filename=item.name)
            sent = False
            if path.suffix.lower() in VIDEO_EXT:
                try:
                    await m.answer_video(file, caption=caption, supports_streaming=True)
                    sent = True
                except TelegramBadRequest:
                    log.info("send_video rejected, falling back to document")
            if not sent:
                await m.answer_document(file, caption=caption)
            ok = True
            await status.delete()
    except TeraError as e:
        await safe_edit(status, f"❌ {html.escape(str(e))}")
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await safe_edit(status, "❌ Telegram rate limit hit. Please retry.")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        await safe_edit(status, "❌ Network error during download. Please retry.")
    except Exception:
        log.exception("process failed")
        await safe_edit(status, "❌ Unexpected error.")
    finally:
        _active[uid] = max(0, _active.get(uid, 1) - 1)
        shutil.rmtree(tmp, ignore_errors=True)
        await db.log_download(uid, item.name, item.size, ok)


async def main():
    global tb
    await db.init()
    api = TelegramAPIServer.from_base(config.LOCAL_API_URL) if config.LOCAL_API_URL else None
    session = AiohttpSession(api=api) if api else AiohttpSession()
    bot = Bot(config.BOT_TOKEN, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=50)) as http:
        tb = TeraBoxClient(http, config.TERABOX_COOKIE)
        try:
            await dp.start_polling(bot)
        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
  
