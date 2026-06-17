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

DISCOUNT_FACTOR = 0.9985  # минус 0,15%

ADMIN_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip()
}

waiting_for_rate = set()
web_app = Flask(__name__)


def is_private_chat(chat):
    return chat.get("type") == "private"


def is_admin(user_id):
    return user_id in ADMIN_USER_IDS


def get_keyboard(chat, user_id):
    if not is_private_chat(chat):
        return {
            "keyboard": [["📊 Курс"]],
            "resize_keyboard": True
        }

    if is_admin(user_id):
        return {
            "keyboard": [
                ["📊 Курс", "➕ Внести курс"],
                ["📣 Рассылка", "💬 Чаты"],
                ["✅ Статус"]
            ],
            "resize_keyboard": True
        }

    return {
        "keyboard": [["📊 Курс"]],
        "resize_keyboard": True
    }


def telegram_api(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload or {}, timeout=15)
    response.raise_for_status()
    return response.json()


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    telegram_api("sendMessage", payload)


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            usd_rub REAL,
            usd_jpy REAL,
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


def save_rate(usd_rub, jpy_rub):
    usd_jpy = (usd_rub / jpy_rub) * 100

    usd_rub_final = usd_rub * DISCOUNT_FACTOR
    usd_jpy_final = usd_jpy * DISCOUNT_FACTOR
    jpy_rub_final = jpy_rub * DISCOUNT_FACTOR

    now = datetime.now(ZoneInfo(TIMEZONE))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO rates (
            date,
            usd_rub,
            usd_jpy,
            jpy_rub,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        now.strftime("%d.%m.%Y"),
        usd_rub_final,
        usd_jpy_final,
        jpy_rub_final,
        now.isoformat()
    ))

    conn.commit()
    conn.close()


def get_latest_rate():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT date, usd_rub, usd_jpy, jpy_rub
        FROM rates
        ORDER BY id DESC
        LIMIT 1
    """)

    row = cur.fetchone()
    conn.close()
    return row


def has_today_rate():
    rate = get_latest_rate()

    if not rate:
        return False

    rate_date = rate[0]
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y")

    return rate_date == today


def build_message():
    rate = get_latest_rate()

    if not rate:
        return (
            "Курсы еще не внесены.\n\n"
            "Администратор может внести курсы в личном чате с ботом."
        )

    date, usd_rub, usd_jpy, jpy_rub = rate

    return (
        f"📊 Курсы на сегодня {date[:5]}\n\n"
        f"💵 USD/RUB — {usd_rub:.3f}\n"
        f"💴 USD/JPY — {usd_jpy:.2f}\n"
        f"🧮 JPY/RUB — {jpy_rub:.3f}"
    )


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
                print("Проверяю курс перед автоматической рассылкой...")

                if has_today_rate():
                    broadcast()
                    last_sent_date = today
                    print("Автоматическая рассылка выполнена ✅")
                else:
                    print("Курс за сегодня не найден. Рассылка пропущена.")

        time.sleep(30)


def parse_rates_from_text(text):
    clean_text = text.replace(",", ".")

    usd_rub_match = re.search(
        r"(?:USD\s*/?\s*RUB|USD\s*RUB)\D+(\d+(?:\.\d+)?)",
        clean_text,
        re.IGNORECASE
    )

    jpy_rub_match = re.search(
        r"(?:JPY\s*/?\s*RUB|JPY\s*RUB)\D+(\d+(?:\.\d+)?)",
        clean_text,
        re.IGNORECASE
    )

    if not usd_rub_match or not jpy_rub_match:
        return None

    return {
        "usd_rub": float(usd_rub_match.group(1)),
        "jpy_rub": float(jpy_rub_match.group(1))
    }


def handle_message(data):
    message = data.get("message")
    if not message:
        return

    chat = message.get("chat", {})
    user = message.get("from", {})

    chat_id = chat.get("id")
    user_id = user.get("id")
    text = message.get("text", "").strip()
    text_lower = text.lower()

    title = (
        chat.get("title")
        or chat.get("first_name")
        or chat.get("username")
        or "Личный чат"
    )

    save_chat(chat_id, title)

    private_chat = is_private_chat(chat)
    admin = is_admin(user_id)
    reply_markup = get_keyboard(chat, user_id)

    if text_lower == "/chatid":
        send_message(chat_id, f"Chat ID: {chat_id}", reply_markup)
        return

    if chat_id in waiting_for_rate:
        if not private_chat or not admin:
            waiting_for_rate.discard(chat_id)
            send_message(
                chat_id,
                "Внесение курса доступно только администратору в личном чате с ботом.",
                reply_markup
            )
            return

        rates = parse_rates_from_text(text)

        if not rates:
            send_message(
                chat_id,
                "Не удалось распознать курсы.\n\n"
                "Пример:\n"
                "курс\n"
                "USD/RUB 92.50\n"
                "JPY/RUB 58.45",
                reply_markup
            )
            return

        save_rate(
            rates["usd_rub"],
            rates["jpy_rub"]
        )

        waiting_for_rate.discard(chat_id)

        send_message(
            chat_id,
            "Курсы сохранены ✅\n"
            "USD/JPY рассчитан автоматически.\n"
            "От каждого курса отнято 0,15%.\n\n"
            + build_message(),
            reply_markup
        )
        return

    if text_lower in ["/start", "старт"]:
        if private_chat and admin:
            send_message(
                chat_id,
                "Бот запущен ✅\n\nВыбери действие в меню ниже:",
                reply_markup
            )
        else:
            send_message(
                chat_id,
                "Бот запущен ✅\n\nВ группе доступна команда /курс",
                reply_markup
            )

    elif text_lower in ["/kurs", "/курс", "📊 курс", "курс"]:
        send_message(chat_id, build_message(), reply_markup)

    elif text_lower in ["➕ внести курс", "внести курс"]:
        if not private_chat or not admin:
            send_message(
                chat_id,
                "Эта команда доступна только администратору в личном чате с ботом.",
                reply_markup
            )
            return

        waiting_for_rate.add(chat_id)

        send_message(
            chat_id,
            "Введите курсы:\n\n"
            "курс\n"
            "USD/RUB 92.50\n"
            "JPY/RUB 58.45\n\n"
            "Бот рассчитает USD/JPY автоматически и отнимет от всех курсов 0,15%.",
            reply_markup
        )

    elif text_lower.startswith("/addrate"):
        if not private_chat or not admin:
            send_message(
                chat_id,
                "Эта команда доступна только администратору в личном чате с ботом.",
                reply_markup
            )
            return

        rates = parse_rates_from_text(text)

        if not rates:
            send_message(
                chat_id,
                "Неверный формат.\n\n"
                "Используй так:\n"
                "/addrate USD/RUB 92.50 JPY/RUB 58.45",
                reply_markup
            )
            return

        save_rate(
            rates["usd_rub"],
            rates["jpy_rub"]
        )

        send_message(
            chat_id,
            "Курсы сохранены ✅\n"
            "USD/JPY рассчитан автоматически.\n"
            "От каждого курса отнято 0,15%.\n\n"
            + build_message(),
            reply_markup
        )

    elif text_lower in ["/status", "✅ статус", "статус"]:
        if not private_chat or not admin:
            send_message(chat_id, build_message(), reply_markup)
            return

        send_message(chat_id, "Бот работает ✅", reply_markup)

    elif text_lower in ["/chats", "💬 чаты", "чаты"]:
        if not private_chat or not admin:
            send_message(
                chat_id,
                "Эта команда доступна только администратору в личном чате с ботом.",
                reply_markup
            )
            return

        send_message(chat_id, get_chats_message(), reply_markup)

    elif text_lower in ["/broadcast", "📣 рассылка", "рассылка"]:
        if not private_chat or not admin:
            send_message(
                chat_id,
                "Эта команда доступна только администратору в личном чате с ботом.",
                reply_markup
            )
            return

        if has_today_rate():
            broadcast()
            send_message(chat_id, "Рассылка отправлена ✅", reply_markup)
        else:
            send_message(
                chat_id,
                "Рассылка не отправлена: курс за сегодня еще не найден.",
                reply_markup
            )


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
