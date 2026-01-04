import asyncio
import re
import sqlite3
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "8205673929:AAEIeyCFZ8L3mMEx9o0ImsZF7FvnME9wfz4"      # <-- ВСТАВЬ СЮДА СВОЙ ТОКЕН
THANKS_CHANNEL = "@sales_engineerings"   # канал благодарности
DB_PATH = "bot.db"
PARSE_MODE = "HTML"

dp = Dispatcher()
WAITING = {}  # user_id -> "channel" | "reward"

# ================= БАЗА =================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS owners (
            owner_id INTEGER PRIMARY KEY,
            channel TEXT NOT NULL,
            reward TEXT
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_channel ON owners(channel)")
    conn.commit()
    return conn

def set_channel(owner_id, channel):
    c = db()
    c.execute("""
        INSERT INTO owners(owner_id, channel) VALUES(?, ?)
        ON CONFLICT(owner_id) DO UPDATE SET channel=excluded.channel
    """, (owner_id, channel))
    c.commit()
    c.close()

def set_reward(owner_id, reward):
    c = db()
    c.execute("UPDATE owners SET reward=? WHERE owner_id=?", (reward, owner_id))
    c.commit()
    c.close()

def get_owner(owner_id):
    c = db()
    r = c.execute("SELECT channel, reward FROM owners WHERE owner_id=?", (owner_id,)).fetchone()
    c.close()
    return {"channel": r[0], "reward": r[1]} if r else None

def get_reward_by_channel(channel):
    c = db()
    r = c.execute("SELECT reward FROM owners WHERE channel=?", (channel,)).fetchone()
    c.close()
    return r[0] if r else None

# ================= УТИЛИТЫ =================
def norm_channel(text):
    t = text.replace("https://", "").replace("http://", "").replace("t.me/", "").replace("@", "").strip()
    return f"@{t}" if re.fullmatch(r"[A-Za-z0-9_]{5,32}", t) else None

def norm_link(text):
    t = text.strip()
    if t.startswith("http://"):
        t = "https://" + t[7:]
    if t.startswith("https://t.me/") or t.startswith("t.me/"):
        return t if t.startswith("https://") else "https://" + t
    return None

async def is_sub(bot, channel, uid):
    try:
        m = await bot.get_chat_member(channel, uid)
        return m.status in ("member", "administrator", "creator")
    except:
        return False

# ================= КНОПКИ =================
def kb_sub():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Подписаться", url=f"https://t.me/{THANKS_CHANNEL[1:]}")],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_thanks")]
    ])

def kb_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧩 Мастер-настройка", callback_data="wizard")],
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="link")],
    ])

def kb_wizard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Указать канал", callback_data="set_channel")],
        [InlineKeyboardButton(text="2️⃣ Ссылка на чат", callback_data="set_reward")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="back")]
    ])

def kb_gate(owner_channel):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Канал автора", url=f"https://t.me/{owner_channel[1:]}")],
        [InlineKeyboardButton(text=f"📌 {THANKS_CHANNEL}", url=f"https://t.me/{THANKS_CHANNEL[1:]}")],
        [InlineKeyboardButton(text="✅ Проверить", callback_data=f"gate:{owner_channel}")]
    ])

def kb_reward(link):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚪 Войти в закрытый чат", url=link)]
    ])

# ================= START =================
@dp.message(Command("start"))
async def start(m: Message, bot: Bot):
    if len(m.text.split()) > 1 and m.text.split()[1].startswith("gate_"):
        ch = norm_channel(m.text.split()[1][5:])
        await m.answer(
            f"Подпишитесь на <b>{ch}</b> и <b>{THANKS_CHANNEL}</b>",
            reply_markup=kb_gate(ch),
            parse_mode=PARSE_MODE
        )
        return

    if not await is_sub(bot, THANKS_CHANNEL, m.from_user.id):
        await m.answer(
            f"Подпишитесь на <b>{THANKS_CHANNEL}</b> для использования бота",
            reply_markup=kb_sub(),
            parse_mode=PARSE_MODE
        )
        return

    await m.answer("Меню владельца:", reply_markup=kb_menu())

# ================= CALLBACKS =================
@dp.callback_query(F.data == "check_thanks")
async def check_thanks(c: CallbackQuery, bot: Bot):
    if await is_sub(bot, THANKS_CHANNEL, c.from_user.id):
        await c.message.edit_text("Меню владельца:", reply_markup=kb_menu())
    else:
        await c.answer("Подписка не найдена", show_alert=True)

@dp.callback_query(F.data == "wizard")
async def wizard(c: CallbackQuery):
    await c.message.edit_text("Мастер-настройка:", reply_markup=kb_wizard())

@dp.callback_query(F.data == "set_channel")
async def set_ch(c: CallbackQuery):
    WAITING[c.from_user.id] = "channel"
    await c.message.edit_text("Отправь @username своего канала")

@dp.callback_query(F.data == "set_reward")
async def set_rw(c: CallbackQuery):
    WAITING[c.from_user.id] = "reward"
    await c.message.edit_text("Отправь инвайт в закрытый чат")

@dp.callback_query(F.data == "back")
async def back(c: CallbackQuery):
    await c.message.edit_text("Меню владельца:", reply_markup=kb_menu())

@dp.callback_query(F.data.startswith("gate:"))
async def gate(c: CallbackQuery, bot: Bot):
    ch = c.data.split(":")[1]
    if await is_sub(bot, THANKS_CHANNEL, c.from_user.id) and await is_sub(bot, ch, c.from_user.id):
        link = get_reward_by_channel(ch)
        await c.message.edit_text("Доступ открыт:", reply_markup=kb_reward(link))
    else:
        await c.answer("Подписка не подтверждена", show_alert=True)

# ================= ВВОД =================
@dp.message(F.text)
async def input_text(m: Message):
    step = WAITING.get(m.from_user.id)
    if not step:
        return

    if step == "channel":
        ch = norm_channel(m.text)
        if not ch:
            await m.answer("Неверный канал")
            return
        set_channel(m.from_user.id, ch)
        WAITING.pop(m.from_user.id)
        await m.answer("Канал сохранён", reply_markup=kb_menu())

    if step == "reward":
        link = norm_link(m.text)
        if not link:
            await m.answer("Неверная ссылка")
            return
        set_reward(m.from_user.id, link)
        WAITING.pop(m.from_user.id)
        await m.answer("Ссылка сохранена", reply_markup=kb_menu())

# ================= MAIN =================
async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=PARSE_MODE))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
