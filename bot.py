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

# -------------------------
# CONFIG
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8205673929:AAFA-XfUvkATrOUtklhCE0A5eB-tcz6W2J8").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@sales_engineerings").strip()  # @sales_engineerings
PORT = int(os.getenv("PORT", "10000"))

WARN_COOLDOWN_SECONDS = int(os.getenv("WARN_COOLDOWN_SECONDS", "60"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty")
if not REQUIRED_CHANNEL:
    raise RuntimeError("REQUIRED_CHANNEL is empty")

logging.basicConfig(level=logging.INFO)

dp = Dispatcher()
_last_warn: Dict[Tuple[int, int], float] = {}  # (chat_id, user_id) -> ts


# -------------------------
# HELPERS
# -------------------------
def channel_url(channel: str) -> str:
    return f"https://t.me/{channel.lstrip('@')}"


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
    return member.status in ("member", "administrator", "creator")


async def warn_once(message: Message, text: str):
    key = (message.chat.id, message.from_user.id)
    now = time.time()

    if now - _last_warn.get(key, 0) < WARN_COOLDOWN_SECONDS:
        return

    _last_warn[key] = now

    try:
        msg = await message.answer(text, disable_web_page_preview=True)
        await asyncio.sleep(15)
        await msg.delete()
    except Exception:
        pass


# -------------------------
# HANDLERS
# -------------------------
@dp.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer(
        "Этот бот удаляет сообщения тех, кто не подписан на канал.\n\n"
        f"Канал: {REQUIRED_CHANNEL}\n"
        f"{channel_url(REQUIRED_CHANNEL)}",
        disable_web_page_preview=True,
    )


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_guard(message: Message, bot: Bot):
    if not message.from_user:
        return

    # не трогаем админов группы
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status in ("administrator", "creator"):
            return
    except Exception:
        pass

    # проверка подписки
    try:
        if await is_subscribed(bot, message.from_user.id):
            return
    except Exception:
        return  # если не можем проверить — не ломаем чат

    # удаляем сообщение
    try:
        await message.delete()
    except Exception:
        pass

    await warn_once(
        message,
        "❗️Чтобы писать в этом чате, нужно быть подписанным на канал:\n"
        f"{REQUIRED_CHANNEL}\n"
        f"{channel_url(REQUIRED_CHANNEL)}"
    )


# -------------------------
# HTTP SERVER (Render)
# -------------------------
async def handle_root(request):
    return web.Response(text="OK")


async def start_http():
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"HTTP server started on {PORT}")


async def start_bot():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await dp.start_polling(bot)


async def main():
    await start_http()
    await start_bot()


if __name__ == "__main__":
    asyncio.run(main())
