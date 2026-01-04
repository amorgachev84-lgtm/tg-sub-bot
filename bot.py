import os
import asyncio
import logging
import time
from typing import Optional, Dict, Tuple

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


# =========================
# CONFIG (ENV)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8205673929:AAH1bGrq6elIdHyJ9AEwCHgndUKWifFZtf0").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@sales_engineerings").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "1109896805").strip()

# Render Web Service provides PORT
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


# =========================
# Anti-spam предупреждений
# =========================
WARN_COOLDOWN_SECONDS = 60
_last_warn_at: Dict[int, float] = {}


def _should_warn(user_id: int) -> bool:
    now = time.time()
    last = _last_warn_at.get(user_id, 0.0)
    if now - last >= WARN_COOLDOWN_SECONDS:
        _last_warn_at[user_id] = now
        return True
    return False


# =========================
# Кэш подписки (чтобы не долбить API)
# =========================
SUB_CACHE_TTL_SECONDS = 60
_sub_cache: Dict[int, Tuple[bool, float]] = {}


def _sub_cache_get(user_id: int) -> Optional[bool]:
    rec = _sub_cache.get(user_id)
    if not rec:
        return None
    ok, exp = rec
    if time.time() >= exp:
        _sub_cache.pop(user_id, None)
        return None
    return ok


def _sub_cache_set(user_id: int, ok: bool) -> None:
    _sub_cache[user_id] = (ok, time.time() + SUB_CACHE_TTL_SECONDS)


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """
    Проверка подписки через getChatMember.
    Важно: бот ДОЛЖЕН быть админом канала, иначе часто будут ошибки доступа.
    """
    cached = _sub_cache_get(user_id)
    if cached is not None:
        return cached

    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        ok = member.status in ("creator", "administrator", "member")
        _sub_cache_set(user_id, ok)
        return ok
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        # TelegramBadRequest: chat not found / user not found / etc.
        # TelegramForbiddenError: bot has no rights to check members (not admin in channel)
        logging.warning("get_chat_member failed (user_id=%s): %s", user_id, e)
        _sub_cache_set(user_id, False)
        return False
    except Exception as e:
        logging.exception("Unexpected error in is_subscribed (user_id=%s): %s", user_id, e)
        _sub_cache_set(user_id, False)
        return False


# =========================
# AIOHTTP web server (для Render healthcheck)
# =========================
async def health(_: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_web_server() -> web.AppRunner:
    """
    ВАЖНО:
    add_get() по умолчанию САМ добавляет HEAD для того же пути.
    Поэтому add_head() на те же URL вызывал RuntimeError "method HEAD is already registered".
    """
    app = web.Application()
    app.router.add_get("/", health)        # HEAD будет добавлен автоматически
    app.router.add_get("/health", health)  # HEAD будет добавлен автоматически

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    logging.info("Web server started on 0.0.0.0:%s", PORT)
    return runner


# =========================
# BOT HANDLERS
# =========================
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id

    # OWNER bypass (если задан)
    if OWNER_ID and user_id == OWNER_ID:
        await message.answer("✅ Доступ открыт (OWNER).")
        return

    if not await is_subscribed(bot, user_id):
        await message.answer(TEXT_NEED_SUB)
        return

    # Для подписанного — без спама, одно стартовое сообщение
    await message.answer("✅ Доступ открыт. Можешь пользоваться ботом.")


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_gatekeeper(message: Message, bot: Bot) -> None:
    """
    В группе:
    - подписан -> молча пропускаем (ничего не отвечаем)
    - не подписан -> пытаемся удалить сообщение, и иногда выдаём инструкцию
    """
    user_id = message.from_user.id

    if OWNER_ID and user_id == OWNER_ID:
        return

    if await is_subscribed(bot, user_id):
        return  # молча

    # Пытаемся удалить (нужны права админа у бота в группе)
    try:
        await message.delete()
    except Exception as e:
        logging.warning("Cannot delete message (need admin rights in group?): %s", e)

    # Предупреждение не чаще, чем раз в минуту
    if _should_warn(user_id):
        # Сначала пробуем написать в группе
        try:
            await message.answer(TEXT_NEED_SUB)
        except Exception:
            # Если нельзя писать в группе — пишем в личку
            try:
                await bot.send_message(user_id, TEXT_NEED_SUB)
            except Exception as e:
                logging.warning("Cannot send warning to user in DM: %s", e)


@dp.message(F.chat.type == "private")
async def private_any(message: Message, bot: Bot) -> None:
    """
    В личке:
    - команды пропускаем (их обработают другие хендлеры)
    - если не подписан -> показываем инструкцию
    - если подписан -> молчим (чтобы не засорять)
    """
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id

    if OWNER_ID and user_id == OWNER_ID:
        return

    if not await is_subscribed(bot, user_id):
        await message.answer(TEXT_NEED_SUB)
        return

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

    # Render: поднимаем web server на PORT
    runner = await start_web_server()

    try:
        await dp.start_polling(bot)
    finally:
        # аккуратное завершение web server
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
