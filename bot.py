# ===== HTTP SERVER для Render =====
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

def run_http_server():
    port = int(os.environ.get("PORT", 8000))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running")

    print(f"[Render] HTTP server running on port {port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ===== ДАЛЬШЕ — ТВОЙ БОТ =====

import asyncio
import logging
import re
import json
from pathlib import Path

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# Токен берем из переменной окружения
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Файл чёрного списка
BLACKLIST_FILE = "blacklist.json"

# Ключевые фразы
BAN_PATTERNS = [
    "ищу помощников для онлайн-работы",
    "занятость: 1–3 часа в день",
    "занятость: 1-3 часа в день",
    "доход: от $",
    "опыт не требуется — всему обучаю",
    "опыт не требуется - всему обучаю",
    "онлайн-работа",
    "работа онлайн",
]

URL_REGEX = re.compile(r"(https?://\S+|t\.me/\S+|www\.\S+)", re.IGNORECASE)

BLACKLIST_USER_IDS = set()
BLACKLIST_USERNAMES = set()

WARN_LIMIT = 2
violations = {}

# Логи
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==== ЧТЕНИЕ / ЗАПИСЬ ЧЁРНОГО СПИСКА ====

def load_blacklist():
    global BLACKLIST_USER_IDS, BLACKLIST_USERNAMES

    try:
        if Path(BLACKLIST_FILE).exists():
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            BLACKLIST_USER_IDS = set(data.get("user_ids", []))
            BLACKLIST_USERNAMES = set(data.get("usernames", []))
            print("[BL] Чёрный список загружен")
    except Exception as e:
        print("[BL] Ошибка загрузки:", e)


def save_blacklist():
    try:
        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "user_ids": list(BLACKLIST_USER_IDS),
                    "usernames": list(BLACKLIST_USERNAMES),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print("[BL] Чёрный список сохранён")
    except Exception as e:
        print("[BL] Ошибка сохранения:", e)


# ==== СПАМ ФИЛЬТР ====

def is_spam_text(text):
    if not text:
        return False
    t = text.lower()
    return URL_REGEX.search(t) or any(p in t for p in BAN_PATTERNS)


async def delete_and_log(update, context, reason, auto_blacklist=False):
    msg = update.message
    user = msg.from_user
    text = msg.text or msg.caption or ""

    try:
        await msg.delete()
    except:
        pass

    print(f"[DEL] {user.id} | {reason} | {text}")

    if auto_blacklist:
        BLACKLIST_USER_IDS.add(user.id)
        if user.username:
            BLACKLIST_USERNAMES.add(user.username.lower())
        save_blacklist()


async def is_admin(chat, uid):
    try:
        member = await chat.get_member(uid)
        return member.status in ("administrator", "creator")
    except:
        return False


# ==== ЛОГИКА БОТА ====

async def handle_message(update, context):
    msg = update.message
    chat = msg.chat
    user = msg.from_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if user.is_bot:
        return

    text = msg.text or msg.caption or ""
    uname = (user.username or "").lower()

    # Чёрный список
    if user.id in BLACKLIST_USER_IDS or uname in BLACKLIST_USERNAMES:
        await delete_and_log(update, context, "blacklisted", auto_blacklist=False)
        return

    # Не спам → игнор
    if not is_spam_text(text):
        return

    # Пропускаем админов
    if await is_admin(chat, user.id):
        return

    # Нарушение
    violations[user.id] = violations.get(user.id, 0) + 1

    # Удаление + автодобавление в чёрный список
    await delete_and_log(update, context, "spam_detected", auto_blacklist=True)

    if violations[user.id] <= WARN_LIMIT:
        await chat.send_message(
            f"{user.mention_html()}, это спам. Вы добавлены в чёрный список.",
            parse_mode="HTML",
        )
    else:
        await context.bot.ban_chat_member(chat.id, user.id)
        await chat.send_message(
            f"{user.mention_html()} заблокирован.", parse_mode="HTML"
        )


# ==== КОМАНДЫ ====

async def add_blacklist(update, context):
    msg = update.message
    chat = msg.chat
    user = msg.from_user

    if not await is_admin(chat, user.id):
        return await msg.reply_text("Только администратор.")

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        BLACKLIST_USER_IDS.add(target.id)
        if target.username:
            BLACKLIST_USERNAMES.add(target.username.lower())
        save_blacklist()
        return await msg.reply_text("Добавлен.")

    return await msg.reply_text("Нужно ответить на сообщение юзера.")


async def remove_blacklist(update, context):
    msg = update.message
    chat = msg.chat
    user = msg.from_user

    if not await is_admin(chat, user.id):
        return await msg.reply_text("Только администратор.")

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
        BLACKLIST_USER_IDS.discard(target.id)
        if target.username:
            BLACKLIST_USERNAMES.discard(target.username.lower())
        save_blacklist()
        return await msg.reply_text("Удалён.")

    return await msg.reply_text("Нужно ответить на сообщение юзера.")


async def list_blacklist(update, context):
    ids = ", ".join(str(i) for i in BLACKLIST_USER_IDS) or "—"
    names = ", ".join("@" + u for u in BLACKLIST_USERNAMES) or "—"
    await update.message.reply_text(f"ID: {ids}\nUsername: {names}")


# ==== ЗАПУСК ====

if __name__ == "__main__":
    load_blacklist()

    # Запуск HTTP-сервера для Render
    threading.Thread(target=run_http_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("add_blacklist", add_blacklist))
    app.add_handler(CommandHandler("remove_blacklist", remove_blacklist))
    app.add_handler(CommandHandler("blacklist", list_blacklist))

    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Caption()) & ~filters.COMMAND,
            handle_message,
        )
    )

    app.run_polling(close_loop=False)
