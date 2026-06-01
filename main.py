import os
import re
import time
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request

BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Vladivostok")
DB_NAME = "rates.db"

waiting_for_rate = set()

web_app = Flask(__name__)


def telegram_api(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload or {}, timeout=15)
    response.raise_for_status()
    return response.json()


def send_message(chat_id, text, keyboard=True):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if keyboard:
        payload["reply_markup"] = {
            "keyboard": [
                ["📊 Курс", "➕ Внести курс"],
                ["📣 Рассылка", "💬 Чаты"],
                ["✅ Статус"]
            ],
            "resize_keyboard": True
        }

    telegram_api("sendMessage", payload)


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            usdt_rub REAL,
            usd_jpy_source REAL,
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


def get_usd_jpy_rate():
    url = "https://api.frankfurter.app/latest?from=USD&to=JPY"
    response = requests.get(url, timeout=10)
    response.raise_for_status()

    data = response.json()
    return float(data["rates"]["JPY"])


def save_rate(usdt_rub):
    usd_jpy_source = get_usd_jpy_rate()
    usd_jpy_work = usd_jpy_source * 0.99
    jpy_rub = usdt_rub / usd_jpy_work

    now = datetime.now(ZoneInfo(TIMEZONE))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO rates (
            date,
            usdt_rub,
            usd_jpy_source,
            usd_jpy_work,
            jpy_rub,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        now.strftime("%d.%m.%Y"),
        usdt_rub,
        usd_jpy_source,
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
        SELECT date, usdt_rub, usd_jpy_source, usd_jpy_work, jpy_rub
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
            "Нажми «➕ Внести курс» и отправь USDT/RUB."
        )

    date, usdt_rub, usd_jpy_source, usd_jpy_work, jpy_rub = rate

    return (
        f"📊 Курсы на сегодня {date[:5]}\n\n"
        f"💵 USDT/RUB — {usdt_rub:.3f}\n"
        f"💴 USD/JPY — {usd_jpy_work:.2f}\n"
        f"🧮 JPY/RUB — {jpy_rub:.4f}"
    )


def broadcast():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("SELECT chat_id FROM chats WHERE active = 1")
    chats = cur.fetchall()

    conn.close()

    message = build_message()

    for chat in chats:
        try:
            send_message(chat[0], message)
        except Exception as e:
            print(f"Ошибка отправки в {chat[0]}: {e}")


def auto_broadcast_loop():
    last_sent_date = None

    while True:
        now = datetime.now(ZoneInfo(TIMEZONE))

        if now.hour == 11 and now.minute == 0:
            today = now.strftime("%Y-%m-%d")

            if last_sent_date != today:
                print("Запускаю автоматическую рассылку курсов...")
                broadcast()
                last_sent_date = today
                print("Автоматическая рассылка выполнена ✅")

        time.sleep(30)


def get_chats_message():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("SELECT chat_id, title, active FROM chats ORDER BY title")
    rows = cur.fetchall()

    conn.close()

    if not rows:
        return "Чатов пока нет."

    text = "Сохраненные чаты:\n\n"

    for chat_id, title, active in rows:
        status_icon = "✅" if active == 1 else "⛔"
        text += f"{status_icon} {title}\nID: {chat_id}\n\n"

    return text


def handle_message(data):
    message = data.get("message")
    if not message:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "").strip()

    title = (
        chat.get("title")
        or chat.get("first_name")
        or chat.get("username")
        or "Личный чат"
    )

    save_chat(chat_id, title)

    text_lower = text.lower()

    if chat_id in waiting_for_rate:
        try:
            number_match = re.search(r"\d+[.,]?\d*", text)
            if not number_match:
                raise ValueError("курс не найден")

            usdt_rub = float(number_match.group(0).replace(",", "."))
            save_rate(usdt_rub)

            waiting_for_rate.remove(chat_id)

            send_message(
                chat_id,
                "Курс сохранен ✅\n\n" + build_message()
            )

        except Exception:
            send_message(
                chat_id,
                "Не удалось распознать курс.\n\n"
                "Пример:\n"
                "75.340"
            )

        return

    if text_lower in ["/start", "старт"]:
        send_message(
            chat_id,
            "Бот запущен ✅\n\nВыбери действие в меню ниже:"
        )

    elif text_lower in ["/kurs", "/курс", "📊 курс", "курс"]:
        send_message(chat_id, build_message())

    elif text_lower in ["➕ внести курс", "внести курс"]:
        waiting_for_rate.add(chat_id)
        send_message(
            chat_id,
            "Введите курс USDT/RUB\n\n"
            "Например:\n"
            "75.340"
        )

    elif text_lower.startswith("/addrate"):
        try:
            parts = text.split()
            usdt_rub = float(parts[1].replace(",", "."))
            save_rate(usdt_rub)

            send_message(
                chat_id,
                "Курс сохранен ✅\n\n" + build_message()
            )

        except Exception:
            send_message(
                chat_id,
                "Неверный формат.\n\n"
                "Используй так:\n"
                "/addrate 75.340"
            )

    elif text_lower in ["/status", "✅ статус", "статус"]:
        send_message(chat_id, "Бот работает ✅")

    elif text_lower in ["/chats", "💬 чаты", "чаты"]:
        send_message(chat_id, get_chats_message())

    elif text_lower in ["/broadcast", "📣 рассылка", "рассылка"]:
        broadcast()
        send_message(chat_id, "Рассылка отправлена ✅")


@web_app.route("/", methods=["GET"])
def home():
    return "AVP Currency Bot is running ✅"


@web_app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    handle_message(data)
    return "ok"


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    init_db()

    threading.Thread(
        target=auto_broadcast_loop,
        daemon=True
    ).start()

    port = int(os.getenv("PORT", 10000))

    print("Бот запускается...")

    web_app.run(
        host="0.0.0.0",
        port=port
    )


if __name__ == "__main__":
    main()
