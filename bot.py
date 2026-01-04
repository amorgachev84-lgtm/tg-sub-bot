import os
import asyncio
import logging
import time
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from aiogram.exceptions import TelegramBadRequest

from aiohttp import web


# =========================
# CONFIG (через ENV)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8205673929:AAH1bGrq6elIdHyJ9AEwCHgndUKWifFZtf0").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@sales_engineerings").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "1109896805").strip()

# Render (Web Service) даёт PORT
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is empty. Set BOT_TOKEN in environment variables.")

if not REQUIRED_CHANNEL:
    raise RuntimeError("ENV REQUIRED_CHANNEL is empty. Example: @sales_engineerings")

if not REQUIRED_CHANNEL.startswith("@"):
    REQUIRED_CHANNEL = "@" + REQUIRED_CHANNEL

OWNER_ID: Optional[int] = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None

PARSE_MODE = ParseMode.HTML

TEXT_NEED_SUB = (
    "❗️Для использования бота нужно быть подписанным на канал:\n"
    f"{REQUIRED_CHANNEL}\n\n"
    "Подпишись и нажми /start ещё раз."
)

# Чтобы не спамить одним и тем же сообщением в группе:
# хранит время последнего предупреждения для user_id
WARN_COOLDOWN_SECONDS = 60
_last_warn_at: Dict[int, float] = {}


# =========================
# HELPERS
# =========================
def _should_warn(user_id: int) -> bool:
    """Антиспам: предупреждать пользователя не чаще, чем раз в N секунд."""
    now = time.time()
    last = _last_warn_at.get(user_id, 0.0)
    if now - last >= WARN_COOLDOWN_SECONDS:
        _last_warn_at[user_id] = now
        return True
    return False


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """
    Проверяем подписку пользователя на REQUIRED_CHANNEL.
    Важно: бот должен быть админом канала, чтобы getChatMember работал стабильно.
    """
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        # allowed statuses: creator, administrator, member
        return member.status in ("creator", "administrator", "member")
    except TelegramBadRequest as e:
        # Частые причины:
        # - bot is not an administrator in the channel
        # - chat not found / wrong username
        # - user not found
        logging.warning("get_chat_member failed: %s", e)
        return False
    except Exception as e:
        logging.exception("Unexpected error in is_subscribed: %s", e)
        return False


# =========================
# AIoHTTP (порт для Render)
# =========================
async def health(request: web.Request) -> web.Response:
    # Отвечаем и на GET и на HEAD
    return web.Response(text="OK")


async def start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_head("/", health)
    app.router.add_get("/health", health)
    app.router.add_head("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logging.info("Web server started on 0.0.0.0:%s", PORT)


# =========================
# BOT HANDLERS
# =========================
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    # Проверяем подписку и показываем сообщение ТОЛЬКО если не подписан
    subscribed = await is_subscribed(bot, message.from_user.id)
    if not subscribed:
        await message.answer(TEXT_NEED_SUB)
        return

    # Если подписан — без лишнего спама, можно коротко
    await message.answer("✅ Доступ открыт. Пиши в чат/группу, где установлен бот.")


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_gatekeeper(message: Message, bot: Bot) -> None:
    """
    Логика для групп:
    - если не подписан: удаляем сообщение и (иногда) предупреждаем
    - если подписан: молча пропускаем (ничего не отвечаем!)
    """
    user_id = message.from_user.id

    # Можно не ограничивать OWNER_ID — но если хочешь, чтобы владелец всегда проходил:
    if OWNER_ID and user_id == OWNER_ID:
        return

    subscribed = await is_subscribed(bot, user_id)
    if subscribed:
        return  # никаких "👌 ты подписан" не пишем

    # Не подписан: пытаемся удалить сообщение (бот должен быть админом группы и иметь право delete)
    try:
        await message.delete()
    except Exception as e:
        logging.warning("Cannot delete message (need admin rights?): %s", e)

    # Предупреждать не чаще, чем раз в минуту
    if _should_warn(user_id):
        try:
            # лучше отвечать в группе как reply (но сообщение уже удалено)
            await message.answer(TEXT_NEED_SUB)
        except Exception:
            # если нельзя писать в группе — пробуем в личку
            try:
                await bot.send_message(user_id, TEXT_NEED_SUB)
            except Exception as e:
                logging.warning("Cannot send warning to user: %s", e)


# (опционально) В личке любые сообщения от НЕ подписанного — мягко направляем на подписку
@dp.message(F.chat.type == "private")
async def private_any(message: Message, bot: Bot) -> None:
    # Не мешаем /start (он уже обработан выше)
    if message.text and message.text.startswith("/"):
        return

    subscribed = await is_subscribed(bot, message.from_user.id)
    if not subscribed:
        await message.answer(TEXT_NEED_SUB)
        return

    # Если подписан — можешь дальше развивать функционал.
    # Сейчас молчим, чтобы не засорять.
    return


# =========================
# MAIN
# =========================
async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=PARSE_MODE),
    )

    # Важно: для Render Web Service нужен открытый порт
    await start_web_server()

    # Стартуем polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
