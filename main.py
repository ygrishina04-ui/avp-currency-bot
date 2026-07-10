import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import requests
from flask import Flask, request
from google.oauth2.service_account import Credentials


BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Vladivostok")

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
RATES_WORKSHEET_NAME = "BOT_КУРСЫ"

DB_NAME = "rates.db"
DISCOUNT_FACTOR = 0.9985  # минус 0,15%

ADMIN_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_USER_IDS", "").split(",")
    if value.strip()
}

waiting_for_rate = set()

web_app = Flask(__name__)

_google_worksheet = None
_google_lock = threading.Lock()


# =========================================================
# ДОСТУП И КЛАВИАТУРЫ
# =========================================================

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
        "remove_keyboard": True
    }


# =========================================================
# TELEGRAM API
# =========================================================

def telegram_api(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    response = requests.post(
        url,
        json=payload or {},
        timeout=20
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Ошибка Telegram API {method}: {result}"
        )

    return result


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    return telegram_api("sendMessage", payload)


def edit_message(chat_id, message_id, text):
    return telegram_api(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text
        }
    )


def pin_message(chat_id, message_id):
    return telegram_api(
        "pinChatMessage",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True
        }
    )


# =========================================================
# GOOGLE SHEETS
# =========================================================

def get_rates_worksheet():
    global _google_worksheet

    if _google_worksheet is not None:
        return _google_worksheet

    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError(
            "Переменная GOOGLE_CREDENTIALS_JSON не задана"
        )

    if not GOOGLE_SPREADSHEET_ID:
        raise RuntimeError(
            "Переменная GOOGLE_SPREADSHEET_ID не задана"
        )

    try:
        credentials_info = json.loads(
            GOOGLE_CREDENTIALS_JSON
        )
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_JSON содержит некорректный JSON"
        ) from error

    # На случай, если переносы строк сохранились как символы \n
    private_key = credentials_info.get("private_key")

    if private_key:
        credentials_info["private_key"] = private_key.replace(
            "\\n",
            "\n"
        )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=scopes
    )

    google_client = gspread.authorize(credentials)

    spreadsheet = google_client.open_by_key(
        GOOGLE_SPREADSHEET_ID
    )

    try:
        worksheet = spreadsheet.worksheet(
            RATES_WORKSHEET_NAME
        )
    except gspread.WorksheetNotFound as error:
        raise RuntimeError(
            f"В таблице не найден лист "
            f"«{RATES_WORKSHEET_NAME}»"
        ) from error

    # Если лист совсем пустой — создаём заголовки.
    if not worksheet.row_values(1):
        worksheet.append_row(
            [
                "Дата",
                "USD/RUB",
                "USD/JPY",
                "Создано"
            ],
            value_input_option="USER_ENTERED"
        )

    _google_worksheet = worksheet
    return _google_worksheet


def save_rate(usd_rub_input, jpy_rub_input):
    if usd_rub_input <= 0 or jpy_rub_input <= 0:
        raise ValueError(
            "Курсы должны быть больше нуля"
        )

    # В таблице сохраняем исходный USD/RUB
    # и рассчитанный исходный USD/JPY.
    usd_jpy_input = (
        usd_rub_input / jpy_rub_input
    ) * 100

    now = datetime.now(ZoneInfo(TIMEZONE))

    row = [
        now.strftime("%d.%m.%Y"),
        round(usd_rub_input, 6),
        round(usd_jpy_input, 6),
        now.strftime("%d.%m.%Y %H:%M:%S")
    ]

    with _google_lock:
        worksheet = get_rates_worksheet()
        worksheet.append_row(
            row,
            value_input_option="USER_ENTERED"
        )

    print(
        "Курсы записаны в Google Sheets: "
        f"USD/RUB={usd_rub_input}; "
        f"USD/JPY={usd_jpy_input}"
    )


def parse_sheet_number(value):
    if value is None:
        raise ValueError("Пустое значение курса")

    normalized = (
        str(value)
        .strip()
        .replace(" ", "")
        .replace(",", ".")
    )

    return float(normalized)


def get_latest_rate():
    with _google_lock:
        worksheet = get_rates_worksheet()
        rows = worksheet.get_all_values()

    # Первая строка — заголовки.
    if len(rows) < 2:
        return None

    # Ищем последнюю непустую строку.
    latest_row = None

    for row in reversed(rows[1:]):
        if any(str(cell).strip() for cell in row):
            latest_row = row
            break

    if not latest_row or len(latest_row) < 3:
        return None

    date = latest_row[0].strip()
    usd_rub_input = parse_sheet_number(latest_row[1])
    usd_jpy_input = parse_sheet_number(latest_row[2])

    if usd_rub_input <= 0 or usd_jpy_input <= 0:
        raise ValueError(
            "В последней строке таблицы некорректные курсы"
        )

    # Восстанавливаем исходный JPY/RUB:
    # USD/JPY = (USD/RUB / JPY/RUB) × 100
    # JPY/RUB = (USD/RUB / USD/JPY) × 100
    jpy_rub_input = (
        usd_rub_input / usd_jpy_input
    ) * 100

    usd_rub_final = usd_rub_input * DISCOUNT_FACTOR
    usd_jpy_final = usd_jpy_input * DISCOUNT_FACTOR
    jpy_rub_final = jpy_rub_input * DISCOUNT_FACTOR

    return (
        date,
        usd_rub_final,
        usd_jpy_final,
        jpy_rub_final
    )


def has_today_rate():
    try:
        rate = get_latest_rate()
    except Exception as error:
        print(
            f"Не удалось проверить курс за сегодня: {error}"
        )
        return False

    if not rate:
        return False

    rate_date = rate[0]

    today = datetime.now(
        ZoneInfo(TIMEZONE)
    ).strftime("%d.%m.%Y")

    return rate_date == today


# =========================================================
# SQLITE: ЧАТЫ И ГРУППЫ
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id TEXT PRIMARY KEY,
            title TEXT,
            active INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_groups (
            chat_id TEXT PRIMARY KEY,
            title TEXT,
            created_at TEXT
        )
    """)

    try:
        cur.execute("""
            ALTER TABLE broadcast_groups
            ADD COLUMN mode TEXT DEFAULT 'send'
        """)
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("""
            ALTER TABLE broadcast_groups
            ADD COLUMN message_id INTEGER
        """)
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def save_chat(chat_id, title):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR IGNORE INTO chats (
            chat_id,
            title,
            active
        )
        VALUES (?, ?, 1)
        """,
        (str(chat_id), title)
    )

    conn.commit()
    conn.close()


def add_broadcast_group(
    chat_id,
    title,
    mode="send",
    message_id=None
):
    now = datetime.now(
        ZoneInfo(TIMEZONE)
    ).isoformat()

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR REPLACE INTO broadcast_groups (
            chat_id,
            title,
            created_at,
            mode,
            message_id
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(chat_id),
            title,
            now,
            mode,
            message_id
        )
    )

    conn.commit()
    conn.close()


def update_group_message_id(chat_id, message_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE broadcast_groups
        SET message_id = ?
        WHERE chat_id = ?
        """,
        (
            message_id,
            str(chat_id)
        )
    )

    conn.commit()
    conn.close()


def remove_broadcast_group(chat_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM broadcast_groups
        WHERE chat_id = ?
        """,
        (str(chat_id),)
    )

    deleted = cur.rowcount

    conn.commit()
    conn.close()

    return deleted > 0


def get_broadcast_groups():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT
            chat_id,
            title,
            mode,
            message_id
        FROM broadcast_groups
        ORDER BY title
    """)

    rows = cur.fetchall()

    conn.close()
    return rows


# =========================================================
# ФОРМИРОВАНИЕ СООБЩЕНИЙ
# =========================================================

def build_message():
    try:
        rate = get_latest_rate()
    except Exception as error:
        print(
            f"Ошибка получения курса из Google Sheets: {error}"
        )
        return (
            "Не удалось получить курсы.\n"
            "Проверьте подключение к Google Таблице."
        )

    if not rate:
        return (
            "Курсы еще не внесены.\n\n"
            "Администратор может внести курсы "
            "в личном чате с ботом."
        )

    date, usd_rub, usd_jpy, jpy_rub = rate

    # USD/JPY считается внутри, но пользователям не показывается.
    return (
        f"📊 Курсы на сегодня {date[:5]}\n\n"
        f"💵 USD/RUB — {usd_rub:.3f}\n"
        f"💴 JPY/RUB — {jpy_rub:.3f}"
    )


def build_pin_message():
    try:
        rate = get_latest_rate()
    except Exception as error:
        print(
            f"Ошибка получения курса для закрепа: {error}"
        )
        return "Курсы временно недоступны"

    if not rate:
        return "Курсы не внесены"

    date, usd_rub, usd_jpy, jpy_rub = rate

    return (
        f"💵{usd_rub:.3f} | "
        f"💴{jpy_rub:.3f} | "
        f"{date[:5]}"
    )


def get_chats_message():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT chat_id, title, active
        FROM chats
        ORDER BY title
    """)

    rows = cur.fetchall()
    conn.close()

    if not rows:
        return "Чатов пока нет."

    text = "Сохраненные чаты:\n\n"

    for chat_id, title, active in rows:
        icon = "✅" if active == 1 else "⛔"

        text += (
            f"{icon} {title}\n"
            f"ID: {chat_id}\n\n"
        )

    return text


def get_groups_message():
    rows = get_broadcast_groups()

    if not rows:
        return (
            "Группы рассылки пока не добавлены.\n\n"
            "В группе можно использовать:\n"
            "/addgroup — обычная рассылка\n"
            "/addpin — обновляемый закреп"
        )

    text = "📣 Группы публикации:\n\n"

    for index, (
        chat_id,
        title,
        mode,
        message_id
    ) in enumerate(rows, start=1):

        mode_text = (
            "закреп"
            if mode == "pin"
            else "обычная рассылка"
        )

        text += (
            f"{index}. {title}\n"
            f"Режим: {mode_text}\n"
            f"ID: {chat_id}\n\n"
        )

    return text


# =========================================================
# РАССЫЛКА И ЗАКРЕПЫ
# =========================================================

def broadcast():
    groups = get_broadcast_groups()

    if not groups:
        print("Нет групп для публикации.")
        return 0, 0

    full_message = build_message()
    pin_message_text = build_pin_message()

    success_count = 0
    error_count = 0

    for chat_id, title, mode, message_id in groups:
        try:
            if mode == "pin":
                if message_id:
                    try:
                        edit_message(
                            chat_id,
                            message_id,
                            pin_message_text
                        )

                        print(
                            f"Закреп обновлен: "
                            f"{title} ({chat_id})"
                        )

                    except Exception as edit_error:
                        print(
                            "Не удалось обновить закреп, "
                            f"создаю новый: {edit_error}"
                        )

                        sent = send_message(
                            chat_id,
                            pin_message_text
                        )

                        new_message_id = (
                            sent["result"]["message_id"]
                        )

                        pin_message(
                            chat_id,
                            new_message_id
                        )

                        update_group_message_id(
                            chat_id,
                            new_message_id
                        )

                else:
                    sent = send_message(
                        chat_id,
                        pin_message_text
                    )

                    new_message_id = (
                        sent["result"]["message_id"]
                    )

                    pin_message(
                        chat_id,
                        new_message_id
                    )

                    update_group_message_id(
                        chat_id,
                        new_message_id
                    )

            else:
                send_message(
                    chat_id,
                    full_message
                )

            success_count += 1

        except Exception as error:
            error_count += 1

            print(
                f"Ошибка публикации в "
                f"{title} ({chat_id}): {error}"
            )

    return success_count, error_count


def auto_broadcast_loop():
    last_sent_date = None

    while True:
        now = datetime.now(
            ZoneInfo(TIMEZONE)
        )

        if now.hour == 11 and now.minute == 0:
            today = now.strftime("%Y-%m-%d")

            if last_sent_date != today:
                if has_today_rate():
                    success_count, error_count = broadcast()

                    last_sent_date = today

                    print(
                        "Автоматическая публикация выполнена. "
                        f"Успешно: {success_count}; "
                        f"ошибок: {error_count}"
                    )

                else:
                    print(
                        "Курс за сегодня не найден. "
                        "Автоматическая публикация пропущена."
                    )

        time.sleep(30)


# =========================================================
# РАЗБОР ВВЕДЕННЫХ КУРСОВ
# =========================================================

def parse_rates_from_text(text):
    clean_text = text.replace(",", ".")

    numbers = re.findall(
        r"\d+(?:\.\d+)?",
        clean_text
    )

    if len(numbers) < 2:
        return None

    usd_rub = float(numbers[0])
    jpy_rub = float(numbers[1])

    if usd_rub <= 0 or jpy_rub <= 0:
        return None

    return {
        "usd_rub": usd_rub,
        "jpy_rub": jpy_rub
    }


# =========================================================
# ОБРАБОТКА TELEGRAM-СООБЩЕНИЙ
# =========================================================

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

    # В личный чат с ботом допускаются только администраторы.
    if private_chat and not admin:
        send_message(
            chat_id,
            "Доступ ограничен.",
            reply_markup
        )
        return

    if text_lower == "/chatid":
        send_message(
            chat_id,
            f"Chat ID: {chat_id}",
            reply_markup
        )
        return

    if text_lower == "/addgroup":
        if private_chat:
            send_message(
                chat_id,
                "Эту команду нужно писать в группе.",
                reply_markup
            )
            return

        if not admin:
            send_message(
                chat_id,
                "Добавлять группы может только "
                "администратор бота.",
                reply_markup
            )
            return

        add_broadcast_group(
            chat_id,
            title,
            mode="send"
        )

        send_message(
            chat_id,
            "✅ Группа добавлена "
            "в обычную рассылку.",
            reply_markup
        )
        return

    if text_lower == "/addpin":
        if private_chat:
            send_message(
                chat_id,
                "Эту команду нужно писать в группе.",
                reply_markup
            )
            return

        if not admin:
            send_message(
                chat_id,
                "Добавлять закреп может только "
                "администратор бота.",
                reply_markup
            )
            return

        add_broadcast_group(
            chat_id,
            title,
            mode="pin",
            message_id=None
        )

        try:
            sent = send_message(
                chat_id,
                build_pin_message()
            )

            message_id = sent["result"]["message_id"]

            pin_message(
                chat_id,
                message_id
            )

            update_group_message_id(
                chat_id,
                message_id
            )

            send_message(
                chat_id,
                "✅ Группа добавлена "
                "в режим закрепа.",
                reply_markup
            )

        except Exception as error:
            send_message(
                chat_id,
                "Группа добавлена в режим закрепа, "
                "но закрепить сообщение не удалось.\n\n"
                "Проверьте права бота.\n\n"
                f"Ошибка: {error}",
                reply_markup
            )

        return

    if text_lower == "/updatepin":
        if private_chat:
            send_message(
                chat_id,
                "Эту команду нужно писать в группе.",
                reply_markup
            )
            return

        if not admin:
            send_message(
                chat_id,
                "Обновлять закреп может только "
                "администратор бота.",
                reply_markup
            )
            return

        add_broadcast_group(
            chat_id,
            title,
            mode="pin",
            message_id=None
        )

        try:
            sent = send_message(
                chat_id,
                build_pin_message()
            )

            message_id = sent["result"]["message_id"]

            pin_message(
                chat_id,
                message_id
            )

            update_group_message_id(
                chat_id,
                message_id
            )

            send_message(
                chat_id,
                "✅ Закреп создан заново.",
                reply_markup
            )

        except Exception as error:
            send_message(
                chat_id,
                "Не удалось создать закреп.\n\n"
                f"Ошибка: {error}",
                reply_markup
            )

        return

    if text_lower == "/removegroup":
        if private_chat:
            send_message(
                chat_id,
                "Эту команду нужно писать в группе.",
                reply_markup
            )
            return

        if not admin:
            send_message(
                chat_id,
                "Удалять группы может только "
                "администратор бота.",
                reply_markup
            )
            return

        removed = remove_broadcast_group(chat_id)

        if removed:
            answer = "❌ Группа удалена из публикации."
        else:
            answer = (
                "Этой группы не было "
                "в списке публикации."
            )

        send_message(
            chat_id,
            answer,
            reply_markup
        )
        return

    if text_lower == "/groups":
        if not private_chat or not admin:
            send_message(
                chat_id,
                "Команда доступна только "
                "администратору в личном чате.",
                reply_markup
            )
            return

        send_message(
            chat_id,
            get_groups_message(),
            reply_markup
        )
        return

    if chat_id in waiting_for_rate:
        if not private_chat or not admin:
            waiting_for_rate.discard(chat_id)
            return

        rates = parse_rates_from_text(text)

        if not rates:
            send_message(
                chat_id,
                "Не удалось распознать курсы.\n\n"
                "Отправьте два значения:\n"
                "76,80\n"
                "48,30",
                reply_markup
            )
            return

        try:
            save_rate(
                rates["usd_rub"],
                rates["jpy_rub"]
            )

            waiting_for_rate.discard(chat_id)

            send_message(
                chat_id,
                "✅ Курсы сохранены в Google Таблице.\n\n"
                + build_message(),
                reply_markup
            )

        except Exception as error:
            print(
                f"Ошибка сохранения курса: {error}"
            )

            send_message(
                chat_id,
                "Не удалось сохранить курсы "
                "в Google Таблице.\n\n"
                f"Ошибка: {error}",
                reply_markup
            )

        return

    if text_lower in ["/start", "старт"]:
        if private_chat and admin:
            send_message(
                chat_id,
                "Бот запущен ✅\n\n"
                "Выберите действие:",
                reply_markup
            )
        else:
            send_message(
                chat_id,
                "Доступна команда /курс",
                reply_markup
            )

    elif text_lower in [
        "/kurs",
        "/курс",
        "📊 курс",
        "курс"
    ]:
        send_message(
            chat_id,
            build_message(),
            reply_markup
        )

    elif text_lower in [
        "➕ внести курс",
        "внести курс"
    ]:
        if not private_chat or not admin:
            return

        waiting_for_rate.add(chat_id)

        send_message(
            chat_id,
            "Введите два курса:\n\n"
            "76,80\n"
            "48,30\n\n"
            "1-я строка — USD/RUB\n"
            "2-я строка — JPY/RUB",
            reply_markup
        )

    elif text_lower.startswith("/addrate"):
        if not private_chat or not admin:
            return

        rates = parse_rates_from_text(text)

        if not rates:
            send_message(
                chat_id,
                "Формат:\n"
                "/addrate 76,80 48,30",
                reply_markup
            )
            return

        try:
            save_rate(
                rates["usd_rub"],
                rates["jpy_rub"]
            )

            send_message(
                chat_id,
                "✅ Курсы сохранены в Google Таблице.\n\n"
                + build_message(),
                reply_markup
            )

        except Exception as error:
            send_message(
                chat_id,
                "Не удалось сохранить курсы.\n\n"
                f"Ошибка: {error}",
                reply_markup
            )

    elif text_lower in [
        "/status",
        "✅ статус",
        "статус"
    ]:
        if private_chat and admin:
            send_message(
                chat_id,
                "Бот работает ✅\n"
                "Хранение курсов: Google Sheets",
                reply_markup
            )

    elif text_lower in [
        "/chats",
        "💬 чаты",
        "чаты"
    ]:
        if private_chat and admin:
            send_message(
                chat_id,
                get_chats_message(),
                reply_markup
            )

    elif text_lower in [
        "/broadcast",
        "📣 рассылка",
        "рассылка"
    ]:
        if not private_chat or not admin:
            return

        if not has_today_rate():
            send_message(
                chat_id,
                "Публикация не выполнена: "
                "курс за сегодня еще не внесен.",
                reply_markup
            )
            return

        success_count, error_count = broadcast()

        send_message(
            chat_id,
            "✅ Публикация выполнена.\n\n"
            f"Успешно: {success_count}\n"
            f"Ошибок: {error_count}",
            reply_markup
        )


# =========================================================
# FLASK
# =========================================================

@web_app.route("/", methods=["GET"])
def home():
    return "AVP Currency Bot is running ✅"


@web_app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        handle_message(data)
    except Exception as error:
        print(
            f"Ошибка обработки webhook: {error}"
        )

    return "ok"


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    init_db()

    # Проверяем подключение к таблице при запуске.
    try:
        get_rates_worksheet()
        print(
            "Google Таблица подключена успешно ✅"
        )
    except Exception as error:
        print(
            f"Ошибка подключения Google Sheets: {error}"
        )

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
