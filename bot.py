import os
import asyncio
import logging
import time
from typing import Dict, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from aiohttp import web

logging.basicConfig(level=logging.INFO)

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8205673929:AAFA-XfUvkATrOUtklhCE0A5eB-tcz6W2J8").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@sales_engineerings").strip()  # например: @sales_engineerings
PORT = int(os.getenv("PORT", "10000"))  # Render выставляет PORT сам
WARN_COOLDOWN_SECONDS = int(os.getenv("WARN_COOLDOWN_SECONDS", "60"))
WARN_AUTO_DELETE_SECONDS = int(os.getenv("WARN_AUTO_DELETE_SECONDS", "15"))  # 0 = не удалять предупреждение

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is empty. Set BOT_TOKEN.")
if not REQUIRED_CHANNEL:
    raise RuntimeError("ENV REQUIRED_CHANNEL is empty. Set REQUIRED_CHANNEL like @channelusername.")

dp = Dispatcher()
_last_warn: Dict[Tuple[int, int], float] = {}  # (chat_id, user_id) -> last_warn_ts


def channel_url(channel: str) -> str:
    ch = channel.strip()
    if ch.startswith("@"):
        ch = ch[1:]
    return f"https://t.me/{ch}"


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """
    Проверяем подписку на REQUIRED_CHANNEL.
    Лучше, чтобы бот был добавлен в канал (желательно админом),
    иначе get_chat_member может работать нестабильно.
    """
    member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
    return member.status in {"member", "administrator", "creator"}


async def warn_with_cooldown(message: Message, text: str) -> None:
    """
    Предупреждаем пользователя не чаще, чем WARN_COOLDOWN_SECONDS.
    ВАЖНО: не делаем reply_to_message_id, потому что сообщение может быть удалено.
    """
    key = (message.chat.id, message.from_user.id)
    now = time.time()
    last = _last_warn.get(key, 0.0)
    if now - last < WARN_COOLDOWN_SECONDS:
        return

    _last_warn[key] = now
    try:
        sent = await message.answer(text, disable_web_page_preview=True)
        if WARN_AUTO_DELETE_SECONDS > 0:
            await asyncio.sleep(WARN_AUTO_DELETE_SECONDS)
            await sent.delete()
    except Exception:
        logging.exception("Failed to warn user / delete warning message")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Бот удаляет сообщения тех, кто не подписан на канал.\n"
        f"Канал: {REQUIRED_CHANNEL}\n"
        f"Ссылка: {channel_url(REQUIRED_CHANNEL)}",
        disable_web_page_preview=True,
    )


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_guard(message: Message, bot: Bot):
    if not message.from_user:
        return

    # Не трогаем админов/создателя группы (удобно, чтобы не мешать модерации)
    try:
        cm = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if cm.status in {"administrator", "creator"}:
            return
    except Exception:
        pass

    # Проверка подписки
    try:
        ok = await is_subscribed(bot, message.from_user.id)
    except Exception:
        # Если бот не может проверить (нет прав в канале и т.п.) — не ломаем чат
        logging.exception("Subscription check failed (check bot rights in channel)")
        return

    if ok:
        return  # подписан — молча пропускаем

    # Не подписан — удаляем сообщение
    try:
        await message.delete()
    except Exception:
        logging.exception("Failed to delete message (check bot rights in group)")

    # Предупреждение (не спамим — cooldown)
    await warn_with_cooldown(
        message,
        "❗️Чтобы писать в этом чате, нужно быть подписанным на канал:\n"
        f"{REQUIRED_CHANNEL}\n"
        f"Подпишись: {channel_url(REQUIRED_CHANNEL)}",
    )


# =========================
# AIOHTTP server for Render Web Service (port binding)
# =========================
async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_http_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_root)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logging.info(f"HTTP server started on 0.0.0.0:{PORT}")


async def start_bot_polling() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await dp.start_polling(bot)


async def main() -> None:
    # Запускаем HTTP сервер (для Render) и polling бота параллельно
    await start_http_server()
    await start_bot_polling()


if __name__ == "__main__":
    asyncio.run(main())
