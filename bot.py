import os
import asyncio
import logging
import time
from typing import Dict

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8205673929:AAFA-XfUvkATrOUtklhCE0A5eB-tcz6W2J8").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@sales_engineerings").strip()  # например: @sales_engineerings

# Анти-спам предупреждений: раз в N секунд на пользователя в одном чате
WARN_COOLDOWN_SECONDS = int(os.getenv("WARN_COOLDOWN_SECONDS", "60"))

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is empty. Set BOT_TOKEN.")
if not REQUIRED_CHANNEL:
    raise RuntimeError("ENV REQUIRED_CHANNEL is empty. Set REQUIRED_CHANNEL like @channelusername.")

dp = Dispatcher()

# (chat_id, user_id) -> last_warn_ts
_last_warn: Dict[tuple, float] = {}


def channel_url(channel: str) -> str:
    ch = channel.strip()
    if ch.startswith("@"):
        ch = ch[1:]
    return f"https://t.me/{ch}"


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """
    Проверяем подписку на REQUIRED_CHANNEL.
    Для корректной работы:
      - канал должен быть публичным (@username),
      - бота нужно добавить в канал (лучше админом).
    """
    member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
    return member.status in {"member", "administrator", "creator"}


async def try_warn_once(message: Message, text: str) -> None:
    """
    Предупреждаем не чаще, чем WARN_COOLDOWN_SECONDS.
    В ответ на сообщение (reply) — так заметнее, но можно убрать reply_to_message_id.
    """
    key = (message.chat.id, message.from_user.id)
    now = time.time()
    last = _last_warn.get(key, 0.0)
    if now - last < WARN_COOLDOWN_SECONDS:
        return

    _last_warn[key] = now
    try:
        await message.answer(
            text,
            reply_to_message_id=message.message_id,
            disable_web_page_preview=True,
        )
    except Exception:
        # даже если не получилось предупредить — не ломаем работу
        logging.exception("Failed to send warning")


@dp.message(CommandStart())
async def start_cmd(message: Message, bot: Bot):
    # В группе /start просто показывает правила пользователю (без проверки),
    # а в ЛС можно подсказать ссылку.
    await message.answer(
        "Этот бот в группе удаляет сообщения тех, кто не подписан на канал.\n"
        f"Канал: {REQUIRED_CHANNEL}\n"
        f"Ссылка: {channel_url(REQUIRED_CHANNEL)}",
        disable_web_page_preview=True
    )


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_guard(message: Message, bot: Bot):
    # Не трогаем сервисные сообщения
    if not message.from_user:
        return

    # Не трогаем админов/создателя группы (опционально, но обычно удобно)
    try:
        chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if chat_member.status in {"administrator", "creator"}:
            return
    except Exception:
        # если не смогли получить статус — продолжаем как обычно
        pass

    # Проверяем подписку на канал
    try:
        ok = await is_subscribed(bot, message.from_user.id)
    except Exception:
        # Если не можем проверить (например, бот не добавлен в канал / нет прав),
        # лучше НЕ удалять сообщения, чтобы не ломать чат.
        logging.exception("Subscription check failed")
        return

    if ok:
        # Подписан — просто ничего не делаем (НЕ спамим "ты подписан")
        return

    # Не подписан — удаляем сообщение
    try:
        await message.delete()
    except Exception:
        # Если бот не админ в группе или нет права Delete messages — удалить не сможет
        logging.exception("Failed to delete message (check bot rights in group)")
        # если не можем удалять — хотя бы предупредим
        await try_warn_once(
            message,
            "❗️Чтобы писать в этом чате, нужно быть подписанным на канал:\n"
            f"{REQUIRED_CHANNEL}\n"
            f"Подпишись: {channel_url(REQUIRED_CHANNEL)}",
        )
        return

    # Предупреждение (не чаще чем раз в cooldown)
    await try_warn_once(
        message,
        "❗️Чтобы писать в этом чате, нужно быть подписанным на канал:\n"
        f"{REQUIRED_CHANNEL}\n"
        f"Подпишись: {channel_url(REQUIRED_CHANNEL)}",
    )


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
