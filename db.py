import time
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, first_seen INTEGER, banned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS downloads(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, size INTEGER, ok INTEGER, ts INTEGER);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def init(self):
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def touch_user(self, uid: int, username: str | None):
        await self.conn.execute(
            "INSERT INTO users(id,username,first_seen) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username",
            (uid, username, int(time.time())),
        )
        await self.conn.commit()

    async def is_banned(self, uid: int) -> bool:
        async with self.conn.execute("SELECT banned FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
        return bool(row and row[0])

    async def set_ban(self, uid: int, banned: bool):
        await self.conn.execute(
            "INSERT INTO users(id,first_seen,banned) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET banned=excluded.banned",
            (uid, int(time.time()), int(banned)),
        )
        await self.conn.commit()

    async def log_download(self, uid: int, name: str, size: int, ok: bool):
        await self.conn.execute(
            "INSERT INTO downloads(user_id,name,size,ok,ts) VALUES(?,?,?,?,?)",
            (uid, name, size, int(ok), int(time.time())),
        )
        await self.conn.commit()

    async def all_user_ids(self) -> list[int]:
        async with self.conn.execute("SELECT id FROM users WHERE banned=0") as c:
            return [r[0] for r in await c.fetchall()]

    async def stats(self) -> dict:
        out = {}
        for key, q in {
            "users": "SELECT COUNT(*) FROM users",
            "downloads_ok": "SELECT COUNT(*) FROM downloads WHERE ok=1",
            "downloads_failed": "SELECT COUNT(*) FROM downloads WHERE ok=0",
        }.items():
            async with self.conn.execute(q) as c:
                out[key] = (await c.fetchone())[0]
        return out
      
