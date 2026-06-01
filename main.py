import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Vladivostok")

DB_NAME = "rates.db"


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
        INSERT INTO rates (date, usdt_rub, usd_jpy_xe, usd_jpy_work, jpy_rub, created_at)
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
        return "Курсы еще не внесены. Используй команду:\n\n/addrate 76.340 159.42"

    date, usdt_rub, usd_jpy_xe, usd_jpy_work, jpy_rub = rate

    return (
        f"Курсы на сегодня {date[:5]}\n\n"
        f"USDT/RUB — {usdt_rub:.3f}\n"
        f"USD/JPY XE -1% — {usd_jpy_work:.2f}\n"
        f"JPY/RUB расчётный — {jpy_rub:.4f}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    save_chat(chat.id, chat.title or chat.first_name or "Личный чат")

    await update.message.reply_text(
        "Бот запущен. Команды:\n\n"
        "/курс — показать курс\n"
        "/addrate 76.340 159.42 — внести курс вручную\n"
        "/status — статус"
    )


async def kurs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    save_chat(chat.id, chat.title or chat.first_name or "Личный чат")

    await update.message.reply_text(build_message())


async def add_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        usdt_rub = float(context.args[0].replace(",", "."))
        usd_jpy_xe = float(context.args[1].replace(",", "."))

        save_rate(usdt_rub, usd_jpy_xe)

        await update.message.reply_text("Курс сохранен ✅\n\n" + build_message())

    except Exception:
        await update.message.reply_text(
            "Неверный формат. Используй так:\n\n"
            "/addrate 76.340 159.42"
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает ✅")


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

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("курс", kurs))
    app.add_handler(CommandHandler("kurs", kurs))
    app.add_handler(CommandHandler("addrate", add_rate))
    app.add_handler(CommandHandler("status", status))

    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        lambda: app.create_task(broadcast(app)),
        "cron",
        hour=10,
        minute=0
    )
    scheduler.start()

    app.run_polling()


if __name__ == "__main__":
    main()
