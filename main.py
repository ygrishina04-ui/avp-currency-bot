import threading
from flask import Flask
import os
import sqlite3
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Vladivostok")

DB_NAME = "rates.db"
MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Курс", "➕ Внести курс"],
        ["📣 Рассылка", "💬 Чаты"],
        ["✅ Статус"]
    ],
    resize_keyboard=True
)
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "AVP Currency Bot is running ✅"

def run_web():
    port = int(os.getenv("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            usdt_rub REAL,
            usd_jpy_xe REAL,
            usd_jpy_work REAL,
            jpy_rub REAL,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id TEXT PRIMARY KEY,
            title TEXT,
            active INTEGER DEFAULT 1
        )
    """)

    conn.commit()
    conn.close()


def save_chat(chat_id, title):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute(
        "INSERT OR IGNORE INTO chats (chat_id, title, active) VALUES (?, ?, 1)",
        (str(chat_id), title)
    )

    conn.commit()
    conn.close()


def save_rate(usdt_rub, usd_jpy_xe):
    usd_jpy_work = usd_jpy_xe * 0.99
    jpy_rub = usdt_rub / usd_jpy_work

    now = datetime.now(ZoneInfo(TIMEZONE))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO rates (
            date,
            usdt_rub,
            usd_jpy_xe,
            usd_jpy_work,
            jpy_rub,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        now.strftime("%d.%m.%Y"),
        usdt_rub,
        usd_jpy_xe,
        usd_jpy_work,
        jpy_rub,
        now.isoformat()
    ))

    conn.commit()
    conn.close()


def get_latest_rate():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT date, usdt_rub, usd_jpy_xe, usd_jpy_work, jpy_rub
        FROM rates
        ORDER BY id DESC
        LIMIT 1
    """)

    row = cur.fetchone()
    conn.close()

    return row


def build_message():
    rate = get_latest_rate()

    if not rate:
        return (
            "Курсы еще не внесены.\n\n"
            "Чтобы внести курс вручную, отправь:\n"
            "/addrate 76.340 159.42"
        )

    date, usdt_rub, usd_jpy_xe, usd_jpy_work, jpy_rub = rate

    return (
        f"Курсы на сегодня {date[:5]}\n\n"
        f"USDT/RUB — {usdt_rub:.3f}\n"
        f"USD/JPY XE — {usd_jpy_work:.2f}\n"
        f"JPY/RUB расчётный — {jpy_rub:.4f}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    save_chat(
        chat.id,
        chat.title or chat.first_name or "Личный чат"
    )

    await update.message.reply_text(
        "Бот запущен ✅\n\nВыбери действие в меню ниже:",
        reply_markup=MENU_KEYBOARD
    )


async def kurs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    save_chat(
        chat.id,
        chat.title or chat.first_name or "Личный чат"
    )

    await update.message.reply_text(build_message())
    
async def text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()

    if text in ["/курс", "📊 курс", "курс"]:
        await kurs(update, context)

    elif text in ["✅ статус", "статус"]:
        await status(update, context)

    elif text in ["💬 чаты", "чаты"]:
        await chats(update, context)

    elif text in ["📣 рассылка", "рассылка"]:
        await manual_broadcast(update, context)

    elif text in ["➕ внести курс", "внести курс"]:
        await update.message.reply_text(
            "Отправь курс в формате:\n\n"
            "/addrate 76.340 159.42\n\n"
            "где:\n"
            "76.340 — USDT/RUB\n"
            "159.42 — USD/JPY XE"
        )

async def add_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            raise ValueError("Недостаточно аргументов")

        usdt_rub = float(context.args[0].replace(",", "."))
        usd_jpy_xe = float(context.args[1].replace(",", "."))

        save_rate(usdt_rub, usd_jpy_xe)

        await update.message.reply_text(
            "Курс сохранен ✅\n\n" + build_message()
        )

    except Exception:
        await update.message.reply_text(
            "Неверный формат.\n\n"
            "Используй так:\n"
            "/addrate 76.340 159.42"
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает ✅")
async def chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("SELECT chat_id, title, active FROM chats ORDER BY title")
    rows = cur.fetchall()

    conn.close()

    if not rows:
        await update.message.reply_text("Чатов пока нет.")
        return

    text = "Сохраненные чаты:\n\n"

    for chat_id, title, active in rows:
        status_icon = "✅" if active == 1 else "⛔"
        text += f"{status_icon} {title}\nID: {chat_id}\n\n"

    await update.message.reply_text(text)


async def manual_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await broadcast(context.application)
    await update.message.reply_text("Рассылка отправлена ✅")

async def broadcast(app: Application):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("SELECT chat_id FROM chats WHERE active = 1")
    chats = cur.fetchall()

    conn.close()

    message = build_message()

    for chat in chats:
        try:
            await app.bot.send_message(chat_id=chat[0], text=message)
        except Exception as e:
            print(f"Ошибка отправки в {chat[0]}: {e}")


def main():
    init_db()

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("kurs", kurs))
    app.add_handler(CommandHandler("addrate", add_rate))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("chats", chats))
    app.add_handler(CommandHandler("broadcast", manual_broadcast))
    app.add_handler(MessageHandler(filters.TEXT, text_commands))

    print("Бот запускается...")
    threading.Thread(target=run_web, daemon=True).start()
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
