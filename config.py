import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

_raw = os.getenv("TERABOX_COOKIE", "").strip()
# Accept either "ndus=XXXX" or just "XXXX"
TERABOX_COOKIE = "" if not _raw else (_raw if "=" in _raw else f"ndus={_raw}")

LOCAL_API_URL = os.getenv("LOCAL_API_URL", "").strip()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1900" if LOCAL_API_URL else "49"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
MAX_FILES_PER_LINK = int(os.getenv("MAX_FILES_PER_LINK", "10"))
USER_COOLDOWN_SEC = int(os.getenv("USER_COOLDOWN_SEC", "5"))
CACHE_TTL_SEC = 20 * 60  # dlinks expire; keep cache short
DB_PATH = os.getenv("DB_PATH", "bot.db")
