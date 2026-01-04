import os
import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ChatMemberStatus
from aiogram.client.default import DefaultBotProperties

# ================== CONFIG ==================
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8205673929:AAH1bGrq6elIdHyJ9AEwCHgndUKWifFZtf0").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@sales_engineerings").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "1109896805").strip()

# Render задаёт PORT сам
PORT = int(os.getenv("PORT", "10000"))

PARSE_MODE = "HTML"

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is empty. Set BOT_TOKEN in Render Environment Variables.")

if not REQUIRED_CHANNEL:
    raise RuntimeError("ENV REQUIRED_CHANNEL is empty. Example: @sales_engineerings")

if not REQUIRED_CHANNEL.startswith("@"):
    REQUIRED_CHANNEL = "@" + REQUIRED_CHANNEL

OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None


# ================== HELPERS ==================
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """
    Проверяем подписку через getChatMember.
    Важно:
    - Канал должен быть публичным (@username) или бот должен быть добавлен в приватный.
    - Часто лучше дать боту права админа в канале (как минимум доступ к участникам).
    """
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
    except Exception as e:
        logging.warning(f"Subscription check failed: {e}")
        return False


def sub_text() -> str:
    return (
        "❗️Для использования бота нужно быть подписанным на канал:\n"
        f"{REQUIRED_CHANNEL}\n\n"
        "Подпишись и нажми /start ещё раз."
    )


# ================== BOT HANDLERS ==================
dp = Dispatcher()

@dp.message(F.text == "/start")
async def cmd_start(message: Message, bot: Bot):
    user_id = message.from_user.id

    # Владелец всегда проходит (по желанию)
    if OWNER_ID and user_id == OWNER_ID:
        await message.answer("✅ Привет! Ты владелец — доступ разрешён.")
        return

    ok = await is_subscribed(bot, user_id)
    if not ok:
        await message.answer(sub_text())
        return

    await message.answer("✅ Доступ разрешён! Напиши любое сообщение — отвечу.")


@dp.message()
async def any_message(message: Message, bot: Bot):
    user_id = message.from_user.id

    if OWNER_ID and user_id == OWNER_ID:
        await message.answer("👋 Принято (владелец).")
        return

    ok = await is_subscribed(bot, user_id)
    if not ok:
        await message.answer(sub_text())
        return

    await message.answer("👌 Ты подписан, сообщение принято.")


# ================== WEB SERVER (для Render порта) ==================
async def handle_root(request):
    return web.Response(text="OK")  # Render увидит порт и успокоится


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logging.info(f"Web server started on 0.0.0.0:{PORT}")


# ================== MAIN ==================
async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=PARSE_MODE))

    # запускаем веб-сервер и бота параллельно
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
