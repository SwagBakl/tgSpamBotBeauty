import asyncio
import logging
import re
import json
from pathlib import Path
import os

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# ===== НАСТРОЙКИ =====

BOT_TOKEN = os.environ["BOT_TOKEN"]  # <-- вставь сюда токен от @BotFather

# Файл, где храним чёрный список
BLACKLIST_FILE = "blacklist.json"

# Ключевые фразы, по которым считаем сообщение спамом
BAN_PATTERNS = [
    "ищу помощников для онлайн-работы",
    "занятость: 1–3 часа в день",
    "занятость: 1-3 часа в день",
    "доход: от $",
    "опыт не требуется — всему обучаю",
    "опыт не требуется - всему обучаю",
    "онлайн-работа",
    "работа онлайн",
    "занятость",
]

# Любые ссылки
URL_REGEX = re.compile(r"(https?://\S+|t\.me/\S+|www\.\S+)", re.IGNORECASE)

# Чёрные списки (заполним после загрузки из файла)
BLACKLIST_USER_IDS: set[int] = set()
BLACKLIST_USERNAMES: set[str] = set()

# Сколько предупреждений до бана (если хочешь только мут — можешь поставить большое число)
WARN_LIMIT = 2

# Счётчик нарушений
violations: dict[int, int] = {}

# ===== ЛОГИ =====

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    filename="spam_log.txt",
)
logger = logging.getLogger(__name__)


# ===== РАБОТА С ФАЙЛОМ ЧЁРНОГО СПИСКА =====

def load_blacklist() -> None:
    """Загружаем чёрный список из файла при старте бота."""
    global BLACKLIST_USER_IDS, BLACKLIST_USERNAMES

    path = Path(BLACKLIST_FILE)
    if not path.exists():
        return

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        BLACKLIST_USER_IDS = set(data.get("user_ids", []))
        BLACKLIST_USERNAMES = set(data.get("usernames", []))
        logger.info(
            "Загружен чёрный список: %d id, %d username",
            len(BLACKLIST_USER_IDS),
            len(BLACKLIST_USERNAMES),
        )
    except Exception as e:
        logger.error("Не удалось загрузить чёрный список: %s", e)


def save_blacklist() -> None:
    """Сохраняем чёрный список в файл после любых изменений."""
    data = {
        "user_ids": list(BLACKLIST_USER_IDS),
        "usernames": list(BLACKLIST_USERNAMES),
    }
    try:
        with Path(BLACKLIST_FILE).open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(
            "Сохранён чёрный список: %d id, %d username",
            len(BLACKLIST_USER_IDS),
            len(BLACKLIST_USERNAMES),
        )
    except Exception as e:
        logger.error("Не удалось сохранить чёрный список: %s", e)


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

def is_spam_text(text: str) -> bool:
    """Проверяем текст на спам по фразам и ссылкам."""
    if not text:
        return False

    t = text.lower()
    if any(pattern in t for pattern in BAN_PATTERNS):
        return True

    if URL_REGEX.search(t):
        return True

    return False


async def delete_and_log(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reason: str,
    auto_blacklist: bool = False,
) -> None:
    """Удаляем сообщение, логируем и при необходимости добавляем в чёрный список."""
    message = update.message
    if message is None:
        return

    user = message.from_user
    chat = message.chat
    text = message.text or message.caption or ""

    try:
        await message.delete()
    except Exception as e:
        logger.error("Не смог удалить сообщение: %s", e)

    logger.info(
        "Удалено сообщение | chat='%s' (%s) | user='%s' (%s) | reason='%s' | text='%s'",
        chat.title if chat.title else chat.id,
        chat.id,
        user.username if user.username else user.id,
        user.id,
        reason,
        text.replace("\n", " "),
    )

    # Автоматически добавляем в чёрный список при удалении спама
    if auto_blacklist:
        BLACKLIST_USER_IDS.add(user.id)
        if user.username:
            BLACKLIST_USERNAMES.add(user.username.lower())
        save_blacklist()
        logger.info(
            "Пользователь %s (%s) добавлен в чёрный список (auto).",
            user.username,
            user.id,
        )


async def is_admin(chat, user_id: int) -> bool:
    """Проверяем, админ ли пользователь."""
    try:
        member = await chat.get_member(user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        logger.error("Не смог получить статус участника: %s", e)
        return False


# ===== ОБРАБОТКА СООБЩЕНИЙ =====

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    chat = message.chat
    user = message.from_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if user.is_bot:
        return

    text = message.text or message.caption
    if text is None:
        return

    username_lower = (user.username or "").lower()

    # --- Если уже в чёрном списке — просто удаляем каждое сообщение ---
    if user.id in BLACKLIST_USER_IDS or username_lower in BLACKLIST_USERNAMES:
        await delete_and_log(update, context, reason="blacklist_auto", auto_blacklist=False)
        return

    # --- Проверка на спам ---
    if not is_spam_text(text):
        return

    # --- Не трогаем админов ---
    if await is_admin(chat, user.id):
        return

    user_id = user.id
    current_violations = violations.get(user_id, 0) + 1
    violations[user_id] = current_violations

    # Удаляем сообщение и сразу кладём в чёрный список
    await delete_and_log(update, context, reason="spam_detected", auto_blacklist=True)

    # Можно оставить только предупреждение, но я добавил ещё бан при повторении
    if current_violations <= WARN_LIMIT:
        try:
            warn_text = (
                f"{user.mention_html()}, реклама и ссылки в этом чате запрещены.\n"
                f"Вы добавлены в чёрный список."
            )
            await chat.send_message(warn_text, parse_mode="HTML")
        except Exception as e:
            logger.error("Не смог отправить предупреждение: %s", e)
    else:
        try:
            await context.bot.ban_chat_member(chat_id=chat.id, user_id=user_id)
            ban_text = (
                f"Пользователь {user.mention_html()} заблокирован за повторный спам."
            )
            await chat.send_message(ban_text, parse_mode="HTML")
        except Exception as e:
            logger.error("Не смог забанить пользователя: %s", e)


# ===== КОМАНДЫ ДЛЯ АДМИНОВ =====

async def add_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    caller = update.effective_user

    if not await is_admin(chat, caller.id):
        await message.reply_text("❌ Только администраторы могут использовать эту команду.")
        return

    target_user = None
    target_username = None

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
    else:
        if not context.args:
            await message.reply_text(
                "Использование:\n"
                "/add_blacklist @username\n"
                "или ответь на сообщение и напиши /add_blacklist"
            )
            return
        arg = context.args[0]
        if arg.startswith("@"):
            target_username = arg[1:].lower()
        else:
            try:
                uid = int(arg)
                BLACKLIST_USER_IDS.add(uid)
                save_blacklist()
                await message.reply_text(f"✅ user_id {uid} добавлен в чёрный список.")
                return
            except ValueError:
                target_username = arg.lower()

    if target_user:
        BLACKLIST_USER_IDS.add(target_user.id)
        if target_user.username:
            BLACKLIST_USERNAMES.add(target_user.username.lower())
        save_blacklist()
        await message.reply_html(f"✅ {target_user.mention_html()} добавлен в чёрный список.")
    elif target_username:
        BLACKLIST_USERNAMES.add(target_username)
        save_blacklist()
        await message.reply_text(f"✅ @{target_username} добавлен в чёрный список.")
    else:
        await message.reply_text("Не удалось определить пользователя.")


async def remove_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    caller = update.effective_user

    if not await is_admin(chat, caller.id):
        await message.reply_text("❌ Только администраторы могут использовать эту команду.")
        return

    if not context.args and not message.reply_to_message:
        await message.reply_text(
            "Использование:\n"
            "/remove_blacklist @username\n"
            "или ответь на сообщение и напиши /remove_blacklist"
        )
        return

    changed = False

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        if target_user.id in BLACKLIST_USER_IDS:
            BLACKLIST_USER_IDS.discard(target_user.id)
            changed = True
        if target_user.username and target_user.username.lower() in BLACKLIST_USERNAMES:
            BLACKLIST_USERNAMES.discard(target_user.username.lower())
            changed = True

        if changed:
            save_blacklist()
            await message.reply_html(
                f"✅ {target_user.mention_html()} удалён из чёрного списка."
            )
        else:
            await message.reply_html(
                f"{target_user.mention_html()} не был в чёрном списке."
            )
    else:
        arg = context.args[0]
        if arg.startswith("@"):
            uname = arg[1:].lower()
            if uname in BLACKLIST_USERNAMES:
                BLACKLIST_USERNAMES.discard(uname)
                changed = True
        else:
            try:
                uid = int(arg)
                if uid in BLACKLIST_USER_IDS:
                    BLACKLIST_USER_IDS.discard(uid)
                    changed = True
            except ValueError:
                await message.reply_text("Некорректный аргумент.")
                return

        if changed:
            save_blacklist()
            await message.reply_text("✅ Пользователь удалён из чёрного списка.")
        else:
            await message.reply_text("Пользователь не найден в чёрном списке.")


async def list_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    caller = update.effective_user

    if not await is_admin(chat, caller.id):
        await message.reply_text("❌ Только администраторы могут использовать эту команду.")
        return

    ids_part = ", ".join(str(uid) for uid in sorted(BLACKLIST_USER_IDS)) or "—"
    names_part = ", ".join("@" + n for n in sorted(BLACKLIST_USERNAMES)) or "—"

    text = (
        "<b>📛 Чёрный список</b>\n\n"
        f"<b>ID:</b> {ids_part}\n"
        f"<b>Username:</b> {names_part}"
    )

    await message.reply_html(text)


# ===== ЗАПУСК =====

async def main():
    # Перед запуском бота загружаем чёрный список из файла
    load_blacklist()

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

    await app.run_polling()


if __name__ == "__main__":
    # Загружаем чёрный список
    load_blacklist()

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

    # ВАЖНО: запускаем без asyncio.run()
    app.run_polling(close_loop=False)
