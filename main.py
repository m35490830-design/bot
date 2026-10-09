import asyncio
import datetime
import html
import logging
import math
import os
import sqlite3
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ================= SOZLAMALAR (Railway Variables'dan olinadi) =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi! Railway > Variables ga BOT_TOKEN qo'shing.")

ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "isakov")
ADMIN_PAROL = os.getenv("ADMIN_PAROL", "sardor")

# Railway serveri UTC da ishlaydi, shuning uchun vaqt zonasini aniq beramiz
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))

# Railway Volume ulangan bo'lsa: DB_FILE=/data/bot_data.db
DB_FILE = os.getenv("DB_FILE", "bot_data.db")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TZ)
router = Router()

authenticated_admins = set()


def now_local() -> datetime.datetime:
    return datetime.datetime.now(TZ)


# ================= DATABASE (SQLite) =================
def db_connect():
    folder = os.path.dirname(DB_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    return sqlite3.connect(DB_FILE)


def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            last_active TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            remind_time TEXT
        )
        """
    )
    # Eski bazaga "sent" ustunini qo'shish (yuborilgan/yuborilmagan)
    cur.execute("PRAGMA table_info(reminders)")
    columns = [row[1] for row in cur.fetchall()]
    if "sent" not in columns:
        cur.execute("ALTER TABLE reminders ADD COLUMN sent INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


def update_user_activity(user_id: int):
    conn = db_connect()
    today = now_local().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO users (user_id, last_active) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET last_active=?",
        (user_id, today, today),
    )
    conn.commit()
    conn.close()


def save_reminder(user_id: int, text: str, remind_time_str: str) -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reminders (user_id, text, remind_time, sent) VALUES (?, ?, ?, 0)",
        (user_id, text, remind_time_str),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def mark_sent(reminder_id: int):
    conn = db_connect()
    conn.execute("UPDATE reminders SET sent=1 WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()


def get_pending_reminders():
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, user_id, text, remind_time FROM reminders WHERE COALESCE(sent, 0) = 0"
    ).fetchall()
    conn.close()
    return rows


def get_stats():
    conn = db_connect()
    cur = conn.cursor()
    today = now_local().strftime("%Y-%m-%d")
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE last_active = ?", (today,))
    today_users = cur.fetchone()[0]
    conn.close()
    return total_users, today_users


# ================= FSM STATES =================
class ReminderState(StatesGroup):
    waiting_for_text = State()
    waiting_for_date = State()
    waiting_for_hour = State()
    waiting_for_minute = State()
    waiting_for_confirm = State()


class AdminState(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()


# ================= KEYBOARDS =================
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Reja qo'shish")],
            [KeyboardButton(text="⚙️ Admin panel")],
        ],
        resize_keyboard=True,
    )


def date_keyboard():
    today = now_local().date()
    days = [
        ("Bugun", today),
        ("Ertaga", today + datetime.timedelta(days=1)),
        ("Indinga", today + datetime.timedelta(days=2)),
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{name} ({d.strftime('%d.%m')})",
                    callback_data=f"date_{d.strftime('%Y-%m-%d')}",
                )
            ]
            for name, d in days
        ]
    )


def hour_keyboard():
    buttons, row = [], []
    for h in range(24):
        hour_str = f"{h:02d}"
        row.append(
            InlineKeyboardButton(text=f"{hour_str}:00", callback_data=f"hour_{hour_str}")
        )
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def minute_keyboard():
    """00 dan 59 gacha — har bir daqiqa tanlanadi (6 tadan qator)."""
    buttons, row = [], []
    for m in range(60):
        m_str = f"{m:02d}"
        row.append(InlineKeyboardButton(text=m_str, callback_data=f"minute_{m_str}"))
        if len(row) == 6:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="confirm_yes"),
                InlineKeyboardButton(text="❌ Bekor qilish", callback_data="confirm_no"),
            ]
        ]
    )


# ================= ESLATMA YUBORISH =================
def schedule_reminder(reminder_id: int, user_id: int, text: str, remind_dt: datetime.datetime):
    """Vaqtdan 5 daqiqa oldin ishga tushadi. Agar 5 daqiqadan kam qolgan bo'lsa — darhol."""
    run_at = max(
        remind_dt - datetime.timedelta(minutes=5),
        now_local() + datetime.timedelta(seconds=3),
    )
    scheduler.add_job(
        send_reminder_notification,
        "date",
        run_date=run_at,
        args=[reminder_id, user_id, text, remind_dt.strftime("%Y-%m-%d %H:%M")],
        id=f"rem_{reminder_id}",
        replace_existing=True,
        misfire_grace_time=3600,
    )


async def send_reminder_notification(reminder_id: int, user_id: int, text: str, time_str: str):
    try:
        remind_dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        left = math.ceil((remind_dt - now_local()).total_seconds() / 60)
        if left > 0:
            head = f"Rejangiz boshlanishiga {left} daqiqa qoldi!"
        else:
            head = "Rejangiz vaqti keldi!"

        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⏰ <b>ESLATMA!</b>\n\n{head}\n"
                f"📌 <b>Reja:</b> {html.escape(text)}\n"
                f"📅 <b>Belgilangan vaqt:</b> {time_str}"
            ),
        )
    except Exception as e:
        logging.error(f"Eslatma yuborishda xatolik: {e}")
    finally:
        mark_sent(reminder_id)


def restore_reminders():
    """Bot qayta ishga tushganda yuborilmagan eslatmalarni qayta jadvalga qo'shadi."""
    restored = 0
    for rid, user_id, text, remind_time in get_pending_reminders():
        try:
            remind_dt = datetime.datetime.strptime(remind_time, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except Exception:
            mark_sent(rid)
            continue
        if remind_dt <= now_local():
            mark_sent(rid)
            continue
        schedule_reminder(rid, user_id, text, remind_dt)
        restored += 1
    logging.info(f"Qayta tiklangan eslatmalar: {restored}")


# ================= HANDLERS =================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    update_user_activity(message.from_user.id)
    await message.answer(
        "Xush kelibsiz! Eslatma botidan foydalanish uchun quyidagi tugmalardan birini tanlang:",
        reply_markup=main_keyboard(),
    )


# --- REJA QO'SHISH ---
@router.message(F.text == "➕ Reja qo'shish")
async def add_plan_start(message: Message, state: FSMContext):
    update_user_activity(message.from_user.id)
    await state.set_state(ReminderState.waiting_for_text)
    await message.answer(
        "Nimani eslatay? Rejangiz mazmunini yozing:",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(ReminderState.waiting_for_text, F.text)
async def process_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await state.set_state(ReminderState.waiting_for_date)
    await message.answer("📅 Qaysi kunga eslatishni tanlang:", reply_markup=date_keyboard())


@router.callback_query(ReminderState.waiting_for_date, F.data.startswith("date_"))
async def process_date(callback: CallbackQuery, state: FSMContext):
    selected_date = callback.data.split("_")[1]
    await state.update_data(selected_date=selected_date)
    await state.set_state(ReminderState.waiting_for_hour)
    await callback.message.edit_text(
        f"📅 Tanlangan sana: <b>{selected_date}</b>\n\n🕒 Endi soatni tanlang:",
        reply_markup=hour_keyboard(),
    )
    await callback.answer()


@router.callback_query(ReminderState.waiting_for_hour, F.data.startswith("hour_"))
async def process_hour(callback: CallbackQuery, state: FSMContext):
    selected_hour = callback.data.split("_")[1]
    await state.update_data(selected_hour=selected_hour)
    await state.set_state(ReminderState.waiting_for_minute)
    await callback.message.edit_text(
        f"🕒 Tanlangan soat: <b>{selected_hour}:XX</b>\n\n⏱ Endi aniq daqiqani tanlang (00–59):",
        reply_markup=minute_keyboard(),
    )
    await callback.answer()


@router.callback_query(ReminderState.waiting_for_minute, F.data.startswith("minute_"))
async def process_minute(callback: CallbackQuery, state: FSMContext):
    selected_minute = callback.data.split("_")[1]
    data = await state.get_data()

    remind_time_str = f"{data['selected_date']} {data['selected_hour']}:{selected_minute}"

    try:
        remind_dt = datetime.datetime.strptime(remind_time_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        now = now_local()

        if remind_dt <= now:
            await callback.answer(
                "❌ Bu vaqt o'tib ketgan! Qaytadan kelajakdagi vaqtni tanlang.",
                show_alert=True,
            )
            await state.set_state(ReminderState.waiting_for_date)
            await callback.message.edit_text(
                "📅 Qaysi kunga eslatishni tanlang:", reply_markup=date_keyboard()
            )
            return

        notify_dt = max(remind_dt - datetime.timedelta(minutes=5), now)
        await state.update_data(remind_time=remind_time_str)
        await state.set_state(ReminderState.waiting_for_confirm)
        await callback.message.edit_text(
            f"📌 <b>Reja:</b> {html.escape(data['text'])}\n"
            f"⏰ <b>Vaqt:</b> {remind_time_str}\n"
            f"🔔 <b>Eslatma yuborilishi:</b> {notify_dt.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"Ma'lumotlarni tasdiqlaysizmi?",
            reply_markup=confirm_keyboard(),
        )
        await callback.answer()
    except Exception as e:
        logging.error(f"process_minute xatolik: {e}")
        await callback.message.edit_text("❌ Xatolik yuz berdi. Qaytadan urinib ko'ring.")
        await state.clear()
        await callback.message.answer("Asosiy menyu:", reply_markup=main_keyboard())


@router.callback_query(
    ReminderState.waiting_for_confirm, F.data.in_(["confirm_yes", "confirm_no"])
)
async def process_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "confirm_yes":
        data = await state.get_data()
        user_id = callback.from_user.id
        text = data["text"]
        remind_time_str = data["remind_time"]
        remind_dt = datetime.datetime.strptime(remind_time_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)

        reminder_id = save_reminder(user_id, text, remind_time_str)
        schedule_reminder(reminder_id, user_id, text, remind_dt)

        await callback.message.edit_text(
            "✅ Rejangiz bot xotirasiga saqlandi! Belgilangan vaqtdan 5 daqiqa oldin eslatma yuboriladi."
        )
    else:
        await callback.message.edit_text("❌ Reja saqlash bekor qilindi.")

    await callback.message.answer("Asosiy menyu:", reply_markup=main_keyboard())
    await state.clear()
    await callback.answer()


# --- ADMIN PANEL ---
def stats_text(prefix: str = "") -> str:
    total, today = get_stats()
    return (
        f"{prefix}📊 <b>BOT STATISTIKASI</b>\n\n"
        f"👥 Jami foydalanuvchilar: {total} ta\n"
        f"🔥 Bugun kirganlar (Online): {today} ta"
    )


@router.message(F.text == "⚙️ Admin panel")
async def admin_start(message: Message, state: FSMContext):
    update_user_activity(message.from_user.id)

    if message.from_user.id in authenticated_admins:
        await message.answer(stats_text(), reply_markup=main_keyboard())
        return

    await state.set_state(AdminState.waiting_for_login)
    await message.answer(
        "🔐 Admin panelga kirish uchun Loginni kiriting:",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AdminState.waiting_for_login, F.text)
async def process_login(message: Message, state: FSMContext):
    if message.text == ADMIN_LOGIN:
        await state.set_state(AdminState.waiting_for_password)
        await message.answer("Parolni kiriting:")
    else:
        await state.clear()
        await message.answer("❌ Login noto'g'ri!", reply_markup=main_keyboard())


@router.message(AdminState.waiting_for_password, F.text)
async def process_password(message: Message, state: FSMContext):
    ok = message.text == ADMIN_PAROL
    # Parol chatda ko'rinib qolmasligi uchun xabarni o'chirishga urinamiz
    try:
        await message.delete()
    except Exception:
        pass

    await state.clear()
    if ok:
        authenticated_admins.add(message.from_user.id)
        await message.answer(
            stats_text("✅ <b>Muvaffaqiyatli kirdingiz!</b>\n\n"),
            reply_markup=main_keyboard(),
        )
    else:
        await message.answer("❌ Parol noto'g'ri!", reply_markup=main_keyboard())


# ================= ISHGA TUSHIRISH =================
async def main():
    init_db()
    dp.include_router(router)
    scheduler.start()
    restore_reminders()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
