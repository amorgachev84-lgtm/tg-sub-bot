import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

# ================= НАСТРОЙКИ (ВСТАВИТЬ ЗНАЧЕНИЯ ТУТ) =================

BOT_TOKEN = "8205673929:AAH1bGrq6elIdHyJ9AEwCHgndUKWifFZtf0"   # ← ВСТАВЬ ТОКЕН БОТА
REQUIRED_CHANNEL = "@sales_engineerings"                     # ← ВСТАВЬ КАНАЛ
OWNER_ID = 1109896805                                        # ← ТВОЙ Telegram ID

# ===================================================================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()


# ================= ПРОВЕРКА ПОДПИСКИ =================

async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


# ================= /start =================

@dp.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.from_user.id

    if not await is_subscribed(user_id):
        await message.answer(
            "❌ <b>Доступ запрещён</b>\n\n"
            "Чтобы пользоваться ботом, подпишись на канал:\n"
            f"👉 {REQUIRED_CHANNEL}\n\n"
            "После подписки нажми /start"
        )
        return

    await message.answer(
        "✅ <b>Доступ разрешён</b>\n\n"
        "Ты подписан на канал и можешь пользоваться ботом."
    )


# ================= ОБРАБОТКА СООБЩЕНИЙ =================

@dp.message(F.text)
async def all_messages(message: Message):
    user_id = message.from_user.id

    if not await is_subscribed(user_id):
        await message.answer(
            "❌ Сначала подпишись на канал:\n"
            f"{REQUIRED_CHANNEL}"
        )
        return

    await message.answer("✉️ Сообщение принято")


# ================= ЗАПУСК =================

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
