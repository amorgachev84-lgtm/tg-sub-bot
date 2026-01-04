# bot.py
# Telegram subscription-gate bot (Owner channel + optional 1 customer channel per group)
# Works well on Render with WEBHOOK.
#
# ENV required:
#   BOT_TOKEN=...
#   OWNER_ID=123456789
#   OWNER_CHANNEL=@sales_engineerings
#   WEBHOOK_URL=https://<your-render-service>.onrender.com/webhook
# Optional:
#   PORT=10000
#   DATABASE_PATH=/data/bot.db          (Render persistent disk recommended)
#   REQUIRED_MESSAGE_TEXT=...          (custom text for "not subscribed to owner channel")
#   CHECK_TTL_SECONDS=60               (cache TTL for subscription checks)
#   WARN_COOLDOWN_SECONDS=60           (anti-spam cooldown for warning messages per user per chat)

import os
import time
import asyncio
import logging
from typing import Optional, Tuple, Dict

import aiosqlite
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8205673929:AAGkTwUkLUHnapi7Ay1cbI1c1VE9LIsa1fw").strip()
OWNER_CHANNEL = os.getenv("OWNER_CHANNEL", "@sales_engineerings").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "1109896805").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://...onrender.com/webhook").strip()

PORT = int(os.getenv("PORT", "10000"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/bot.db").strip()

CHECK_TTL_SECONDS = int(os.getenv("CHECK_TTL_SECONDS", "60"))
WARN_COOLDOWN_SECONDS = int(os.getenv("WARN_COOLDOWN_SECONDS", "60"))

DEFAULT_REQUIRED_TEXT = (
    "❗️Для использования бота нужно быть подписанным на канал:\n"
    f"{OWNER_CHANNEL}\n\n"
    "Подпишись и нажми /start ещё раз."
)
REQUIRED_MESSAGE_TEXT = os.getenv("REQUIRED_MESSAGE_TEXT", DEFAULT_REQUIRED_TEXT).strip()

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is empty.")
if not OWNER_ID_RAW.isdigit():
    raise RuntimeError("ENV OWNER_ID must be numeric Telegram user id.")
OWNER_ID = int(OWNER_ID_RAW)

if not WEBHOOK_URL:
    raise RuntimeError("ENV WEBHOOK_URL is empty (required for Render webhook mode).")

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("subgate-bot")

# =========================
# BOT / DISPATCHER
# =========================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# =========================
# SIMPLE CACHES (in-memory)
# =========================
# (channel, user_id) -> (is_subscribed, expires_at)
_sub_cache: Dict[Tuple[str, int], Tuple[bool, float]] = {}

# (chat_id, user_id) -> last_warn_ts
_warn_cooldown: Dict[Tuple[int, int], float] = {}


# =========================
# DB
# =========================
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS groups (
    group_id INTEGER PRIMARY KEY,
    title TEXT,
    added_by_user_id INTEGER,
    customer_channel TEXT,
    is_customer_channel_required INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_groups_updated_at ON groups(updated_at);
"""

DB_LOCK = asyncio.Lock()


async def db_init() -> None:
    # Ensure folder exists (especially if DATABASE_PATH includes directories)
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()
    log.info("DB initialized at %s", DATABASE_PATH)


async def upsert_group(group_id: int, title: str, added_by_user_id: int) -> None:
    now = int(time.time())
    async with DB_LOCK:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """
                INSERT INTO groups (group_id, title, added_by_user_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    title=excluded.title,
                    updated_at=excluded.updated_at
                """,
                (group_id, title, added_by_user_id, now, now),
            )
            await db.commit()


async def set_customer_channel(group_id: int, channel: Optional[str], required: bool) -> None:
    now = int(time.time())
    async with DB_LOCK:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """
                UPDATE groups
                SET customer_channel = ?,
                    is_customer_channel_required = ?,
                    updated_at = ?
                WHERE group_id = ?
                """,
                (channel, 1 if required else 0, now, group_id),
            )
            await db.commit()


async def get_group_config(group_id: int) -> Tuple[Optional[str], bool]:
    async with DB_LOCK:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cur = await db.execute(
                "SELECT customer_channel, is_customer_channel_required FROM groups WHERE group_id=?",
                (group_id,),
            )
            row = await cur.fetchone()
            await cur.close()
    if not row:
        return None, False
    ch, req = row[0], bool(row[1])
    return ch, req


async def list_groups_for_user(user_id: int):
    async with DB_LOCK:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cur = await db.execute(
                """
                SELECT group_id, title, customer_channel, is_customer_channel_required, updated_at
                FROM groups
                WHERE added_by_user_id=?
                ORDER BY updated_at DESC
                """,
                (user_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
    return rows


# =========================
# SUBSCRIPTION CHECK
# =========================
def _is_member_status(status: str) -> bool:
    return status in ("member", "administrator", "creator")


async def is_subscribed(channel: str, user_id: int) -> bool:
    """
    Returns True if user is member/admin/creator of the channel.
    Uses a small TTL cache to reduce Telegram API calls.
    """
    channel = channel.strip()
    if not channel:
        return False

    key = (channel, user_id)
    now = time.time()
    cached = _sub_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]

    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        ok = _is_member_status(getattr(member, "status", ""))
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        # If bot cannot access the channel, we cannot verify.
        # For OWNER channel, it must be accessible or the whole bot makes no sense.
        log.warning("Cannot check subscription: channel=%s user=%s err=%r", channel, user_id, e)
        ok = False

    _sub_cache[key] = (ok, now + CHECK_TTL_SECONDS)
    return ok


async def can_warn(chat_id: int, user_id: int) -> bool:
    now = time.time()
    key = (chat_id, user_id)
    last = _warn_cooldown.get(key, 0.0)
    if now - last < WARN_COOLDOWN_SECONDS:
        return False
    _warn_cooldown[key] = now
    return True


# =========================
# TEXT BUILDERS
# =========================
def build_group_requirements_text(owner_channel: str, customer_channel: Optional[str], customer_required: bool) -> str:
    if customer_required and customer_channel:
        return (
            "❗️Чтобы писать в этом чате, нужно быть подписанным на каналы:\n"
            f"1) {owner_channel}\n"
            f"2) {customer_channel}\n\n"
            "Подпишись и нажми /check."
        )
    return (
        "❗️Чтобы писать в этом чате, нужно быть подписанным на канал:\n"
        f"{owner_channel}\n\n"
        "Подпишись и нажми /check."
    )


def normalize_channel_username(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("https://t.me/"):
        raw = raw.replace("https://t.me/", "").strip("/")
    if not raw.startswith("@"):
        raw = "@" + raw
    return raw


# =========================
# HANDLERS: PRIVATE
# =========================
@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_private(message: Message):
    user_id = message.from_user.id

    if not await is_subscribed(OWNER_CHANNEL, user_id):
        await message.answer(REQUIRED_MESSAGE_TEXT)
        return

    text = (
        "✅ Доступ подтверждён.\n\n"
        "<b>Как подключить бота в группу:</b>\n"
        "1) Добавь бота в свою группу.\n"
        "2) Выдай боту права администратора (минимум: <i>удаление сообщений</i>).\n"
        "3) В группе напиши команду:\n"
        "<code>/setchannel @ваш_канал</code> — чтобы требовать подписку на 1 ваш канал\n"
        "или <code>/disablechannel</code> — чтобы отключить требование на ваш канал.\n\n"
        "<b>Важно:</b> требование подписки на канал Владельца действует всегда."
    )
    await message.answer(text)


@router.message(Command("mygroups"), F.chat.type == ChatType.PRIVATE)
async def my_groups(message: Message):
    user_id = message.from_user.id

    if not await is_subscribed(OWNER_CHANNEL, user_id):
        await message.answer(REQUIRED_MESSAGE_TEXT)
        return

    rows = await list_groups_for_user(user_id)
    if not rows:
        await message.answer("Пока нет групп, где ты подключил бота как админа.")
        return

    lines = ["<b>Твои группы:</b>"]
    for group_id, title, ch, req, upd in rows[:30]:
        status = "требуется" if req else "не требуется"
        ch_show = ch or "—"
        lines.append(f"• <b>{title}</b> (<code>{group_id}</code>)\n  канал: {ch_show} | проверка: {status}")
    await message.answer("\n".join(lines))


# =========================
# HANDLERS: GROUP ADMIN COMMANDS
# =========================
async def is_admin_in_chat(chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return getattr(m, "status", "") in ("administrator", "creator")
    except Exception:
        return False


@router.message(Command("setchannel"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def setchannel_group(message: Message):
    # Gate: must be subscribed to Owner channel to use bot at all
    if not await is_subscribed(OWNER_CHANNEL, message.from_user.id):
        # in group we keep it short to avoid spam
        if await can_warn(message.chat.id, message.from_user.id):
            await message.reply(REQUIRED_MESSAGE_TEXT)
        return

    if not await is_admin_in_chat(message.chat.id, message.from_user.id):
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: <code>/setchannel @ваш_канал</code>")
        return

    customer_channel = normalize_channel_username(args[1])
    await upsert_group(message.chat.id, message.chat.title or str(message.chat.id), message.from_user.id)

    # Try to validate that bot can access the channel (optional, but useful)
    accessible = True
    try:
        await bot.get_chat(customer_channel)
    except Exception as e:
        accessible = False
        log.warning("Customer channel not accessible now: %s err=%r", customer_channel, e)

    await set_customer_channel(message.chat.id, customer_channel, True)

    if accessible:
        await message.reply(
            "✅ Готово.\n"
            f"Теперь в этом чате требуется подписка на:\n"
            f"1) {OWNER_CHANNEL}\n"
            f"2) {customer_channel}\n\n"
            "Проверка выполняется автоматически при сообщениях и по команде /check."
        )
    else:
        await message.reply(
            "✅ Настройка сохранена, но я не смог проверить доступ к вашему каналу.\n\n"
            "Чтобы проверка подписки работала корректно, добавьте этого бота в ваш канал "
            "(лучше как администратора) и повторите /setchannel.\n\n"
            f"Текущий канал: {customer_channel}"
        )


@router.message(Command("disablechannel"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def disablechannel_group(message: Message):
    if not await is_subscribed(OWNER_CHANNEL, message.from_user.id):
        if await can_warn(message.chat.id, message.from_user.id):
            await message.reply(REQUIRED_MESSAGE_TEXT)
        return

    if not await is_admin_in_chat(message.chat.id, message.from_user.id):
        return

    await upsert_group(message.chat.id, message.chat.title or str(message.chat.id), message.from_user.id)
    await set_customer_channel(message.chat.id, None, False)
    await message.reply(
        "✅ Готово.\n"
        f"Теперь в этом чате требуется только подписка на канал Владельца: {OWNER_CHANNEL}"
    )


@router.message(Command("status"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def status_group(message: Message):
    if not await is_subscribed(OWNER_CHANNEL, message.from_user.id):
        if await can_warn(message.chat.id, message.from_user.id):
            await message.reply(REQUIRED_MESSAGE_TEXT)
        return

    if not await is_admin_in_chat(message.chat.id, message.from_user.id):
        return

    ch, req = await get_group_config(message.chat.id)
    if req and ch:
        await message.reply(
            "📌 <b>Статус проверки</b>\n"
            f"Owner канал: {OWNER_CHANNEL}\n"
            f"Канал группы: {ch}\n"
            "Режим: требуется подписка на оба канала"
        )
    else:
        await message.reply(
            "📌 <b>Статус проверки</b>\n"
            f"Owner канал: {OWNER_CHANNEL}\n"
            "Канал группы: —\n"
            "Режим: требуется подписка только на Owner канал"
        )


# =========================
# HANDLERS: GROUP CHECK + CALLBACK
# =========================
@router.message(Command("check"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def check_group(message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    customer_channel, customer_required = await get_group_config(chat_id)

    ok_owner = await is_subscribed(OWNER_CHANNEL, user_id)
    ok_customer = True
    if customer_required and customer_channel:
        # If bot cannot access customer channel, check will become False.
        ok_customer = await is_subscribed(customer_channel, user_id)

    if ok_owner and ok_customer:
        await message.reply("✅ Доступ подтверждён.")
    else:
        text = build_group_requirements_text(OWNER_CHANNEL, customer_channel, customer_required)
        await message.reply(text)


@router.callback_query(F.data.startswith("recheck:"))
async def recheck_callback(call: CallbackQuery):
    try:
        _, chat_id_s = call.data.split(":", 1)
        chat_id = int(chat_id_s)
    except Exception:
        await call.answer("Ошибка данных.", show_alert=True)
        return

    user_id = call.from_user.id
    customer_channel, customer_required = await get_group_config(chat_id)

    ok_owner = await is_subscribed(OWNER_CHANNEL, user_id)
    ok_customer = True
    if customer_required and customer_channel:
        ok_customer = await is_subscribed(customer_channel, user_id)

    if ok_owner and ok_customer:
        await call.answer("✅ Теперь всё ок!", show_alert=True)
    else:
        await call.answer("Пока нет подписки на все каналы.", show_alert=True)


# =========================
# MAIN ENFORCEMENT: any message in group
# =========================
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def enforce_on_message(message: Message):
    # Ignore messages from bots
    if not message.from_user or message.from_user.is_bot:
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    # Ensure we store group if bot is used there (best-effort)
    try:
        await upsert_group(chat_id, message.chat.title or str(chat_id), message.from_user.id)
    except Exception as e:
        log.warning("Upsert group failed: %r", e)

    customer_channel, customer_required = await get_group_config(chat_id)

    ok_owner = await is_subscribed(OWNER_CHANNEL, user_id)
    ok_customer = True
    if customer_required and customer_channel:
        ok_customer = await is_subscribed(customer_channel, user_id)

    if ok_owner and ok_customer:
        return  # allow silently (no spam)

    # If fail: delete the message (no extra "accepted" spam)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as e:
        # If bot cannot delete (missing rights), just warn rarely
        log.warning("Cannot delete message in chat=%s: %r", chat_id, e)

    if await can_warn(chat_id, user_id):
        text = build_group_requirements_text(OWNER_CHANNEL, customer_channel, customer_required)
        # Keep it simple; do not spam chat
        try:
            await message.answer(text)
        except Exception:
            pass


# =========================
# WEBHOOK APP (aiohttp)
# =========================
async def on_startup(app: web.Application):
    await db_init()
    # Set webhook
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    log.info("Webhook set to %s", WEBHOOK_URL)


async def on_shutdown(app: web.Application):
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    await bot.session.close()
    log.info("Shutdown complete")


async def handle_webhook(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad Request")

    # Feed update into aiogram dispatcher
    await dp.feed_webhook_update(bot, data)
    return web.Response(text="OK")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


if __name__ == "__main__":
    app = build_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
