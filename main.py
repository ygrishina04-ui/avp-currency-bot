import os
import re
import time
import sqlite3
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request


BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Vladivostok")
DB_NAME = "rates.db"

ADMIN_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip()
}

TTB_CHAT_ID = os.getenv("TTB_CHAT_ID")

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


def get_cbr_usd_rub():
    url = "https://www.cbr.ru/scripts/XML_daily.asp"

    response = requests.get(url, timeout=10)
    response.raise_for_status()

    root = ET.fromstring(response.content)

    for valute in root.findall("Valute"):
        char_code = valute.find("CharCode").text

        if char_code == "USD":
            value = valute.find("Value").text.replace(",", ".")
            return float(value)

    raise ValueError("USD не найден в курсах ЦБ РФ")


def save_rate(usd_jpy):
    cbr_usd_rub = get_cbr_usd_rub()
    usdt_rub = cbr_usd_rub * 1.032

    jpy_rub = (usdt_rub / usd_jpy) + 0.02

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
        usd_jpy,
        usd_jpy,
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
            "Ожидаю курс USD/JPY из группы ТТБ."
        )

    date, usdt_rub, usd_jpy_source, usd_jpy_work, jpy_rub = rate

    return (
        f"📊 Курсы на сегодня {date[:5]}\n\n"
        f"💵 USDT/RUB — {usdt_rub:.3f}\n"
        f"💴 USD/JPY — {usd_jpy_work:.2f}\n"
        f"🧮 JPY/RUB — {jpy_rub:.4f}"
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

        if now.hour == 12 and now.minute == 30:
            today = now.strftime("%Y-%m-%d")

            if last_sent_date != today:
                print("Проверяю курс перед автоматической рассылкой...")

                if has_today_rate():
                    print("Курс за сегодня найден. Запускаю рассылку...")
                    broadcast()
                    last_sent_date = today
                    print("Автоматическая рассылка выполнена ✅")
                else:
                    print("Курс за сегодня не найден. Рассылка пропущена.")

        time.sleep(30)


def extract_rate_from_text(text):
    text = text.lower().strip()

    if not text.startswith("курс"):
        return None

    match = re.search(r"\d+[.,]?\d*", text)

    if not match:
        return None

    return float(match.group(0).replace(",", "."))


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
        send_message(
            chat_id,
            f"Chat ID: {chat_id}",
            reply_markup
        )
        return

    # Автозабор USD/JPY из группы ТТБ.
    # В группе должен писать человек, не бот.
    if TTB_CHAT_ID and str(chat_id) == str(TTB_CHAT_ID):
        usd_jpy = extract_rate_from_text(text)

        if usd_jpy:
            save_rate(usd_jpy)
            print(f"Курс из группы ТТБ сохранен: USD/JPY = {usd_jpy}")

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

        try:
            usd_jpy = extract_rate_from_text(text)

            if not usd_jpy:
                raise ValueError("курс не найден")

            save_rate(usd_jpy)
            waiting_for_rate.discard(chat_id)

            send_message(
                chat_id,
                "Курс сохранен ✅\n\n" + build_message(),
                reply_markup
            )

        except Exception:
            send_message(
                chat_id,
                "Не удалось распознать курс.\n\n"
                "Пример:\n"
                "157.83",
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
            "Введите курс USD/JPY\n\n"
            "Например:\n"
            "157.83",
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

        try:
            parts = text.split()
            usd_jpy = float(parts[1].replace(",", "."))
            save_rate(usd_jpy)

            send_message(
                chat_id,
                "Курс сохранен ✅\n\n" + build_message(),
                reply_markup
            )

        except Exception:
            send_message(
                chat_id,
                "Неверный формат.\n\n"
                "Используй так:\n"
                "/addrate 157.83",
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
