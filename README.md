# TeraBox Telegram Bot

## Setup
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # fill BOT_TOKEN, ADMIN_IDS, TERABOX_COOKIE
python bot.py
```

## Getting the cookie
Log in to terabox.com with a throwaway account -> DevTools -> Application -> Cookies -> copy the `ndus` value into `TERABOX_COOKIE`.
Never commit `.env`. The cookie is only ever sent to TeraBox domains.

## Files over 50 MB
Run a local Telegram Bot API server (https://github.com/tdlib/telegram-bot-api), set `LOCAL_API_URL=http://127.0.0.1:8081`. Upload limit becomes ~2 GB.

## Admin commands
/stats, /ban <id>, /unban <id>, /broadcast <text>

## If it breaks
TeraBox's web endpoints are unofficial. Failures are almost always: expired cookie, changed token location (edit `_TOKEN_PATTERNS` in terabox.py), or a new domain (add to `DOMAINS`).
