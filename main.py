import base64
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


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Vladivostok")
DB_NAME = os.getenv("DB_NAME", "rates.db")

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
JAPAN_SPREADSHEET_ID = os.getenv("JAPAN_SPREADSHEET_ID")
RATES_SHEET_NAME = os.getenv("RATES_SHEET_NAME", "BOT_КУРСЫ")

CLIENTS_SHEET_NAME = os.getenv("JAPAN_CLIENTS_SHEET", "Клиенты")
LOGISTICS_SHEET_NAME = os.getenv("JAPAN_LOGISTICS_SHEET", "Сверка 2.0")

WATCH_INTERVAL_SECONDS = int(os.getenv("JAPAN_WATCH_INTERVAL_SECONDS", "60"))
DISCOUNT_FACTOR = 0.9985  # минус 0,15%

ADMIN_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip()
}

# Точные заголовки листа «Клиенты»
CLIENT_COLUMN = "Клиент"
TELEGRAM_ID_COLUMN = "Telegram ID чата"

# Точные заголовки листа «Логистика»
CAR_MODEL_COLUMN = "Модель и марка ТС"
BODY_NUMBER_COLUMN = "Номер кузова"

YARD_PLAN_COLUMN = "ПЛАН дата доставки на ярд"
YARD_FACT_COLUMN = "ФАКТ дата доставки на ярд"
JAPAN_EXIT_PLAN_COLUMN = "ПЛАН выхода из Японии"
JAPAN_EXIT_FACT_COLUMN = "ФАКТ выхода из Японии"
CHINA_KOREA_ARRIVAL_COLUMN = "Дата прибытия в Китай/Корею"
CHINA_EXIT_PLAN_COLUMN = "ПЛАН выхода из Китай"
CHINA_EXIT_FACT_COLUMN = "ФАКТ выхода из Китай"
RUSSIA_ARRIVAL_PLAN_COLUMN = "ПЛАН прибытия в РФ"
RUSSIA_ARRIVAL_FACT_COLUMN = "ФАКТ прибытия в РФ"
RELEASE_DATE_COLUMN = "ВЫПУСК ДАТА"

# Все даты, изменения которых отслеживаются.
# Удаление/очистка значения клиенту не отправляется.
TRACKED_COLUMNS = {
    YARD_PLAN_COLUMN: ("Плановая дата доставки на ярд", "plan"),
    YARD_FACT_COLUMN: ("Автомобиль доставлен на ярд", "fact"),
    JAPAN_EXIT_PLAN_COLUMN: ("Плановая дата выхода из Японии", "plan"),
    JAPAN_EXIT_FACT_COLUMN: ("Автомобиль вышел из Японии", "fact"),
    CHINA_KOREA_ARRIVAL_COLUMN: ("Дата прибытия в Китай/Корею", "fact"),
    CHINA_EXIT_PLAN_COLUMN: ("Плановая дата выхода из Китая", "plan"),
    CHINA_EXIT_FACT_COLUMN: ("Автомобиль вышел из Китая", "fact"),
    RUSSIA_ARRIVAL_PLAN_COLUMN: ("Плановая дата прибытия в Россию", "plan"),
    RUSSIA_ARRIVAL_FACT_COLUMN: ("Автомобиль прибыл в Россию", "fact"),
    RELEASE_DATE_COLUMN: ("Автомобиль выпущен", "fact"),
}

# Этапы для ответа по кнопке «Уточнить место дислокации груза».
STAGES = [
    {
        "name": "Ожидается доставка автомобиля на ярд",
        "plan": YARD_PLAN_COLUMN,
        "fact": YARD_FACT_COLUMN,
        "date_label": "Плановая дата доставки на ярд",
    },
    {
        "name": "Ожидается выход из Японии",
        "plan": JAPAN_EXIT_PLAN_COLUMN,
        "fact": JAPAN_EXIT_FACT_COLUMN,
        "date_label": "Плановая дата выхода из Японии",
    },
    {
        "name": "Ожидается выход из Китая",
        "plan": CHINA_EXIT_PLAN_COLUMN,
        "fact": CHINA_EXIT_FACT_COLUMN,
        "date_label": "Плановая дата выхода из Китая",
    },
    {
        "name": "Автомобиль следует в Россию",
        "plan": RUSSIA_ARRIVAL_PLAN_COLUMN,
        "fact": RUSSIA_ARRIVAL_FACT_COLUMN,
        "date_label": "Плановая дата прибытия в РФ",
    },
]

waiting_for_rate = set()
web_app = Flask(__name__)

_google_client = None
_google_worksheet = None
_google_lock = threading.Lock()
_rates_lock = threading.Lock()


# ============================================================
# ОБЩИЕ ФУНКЦИИ TELEGRAM
# ============================================================

def is_private_chat(chat):
    return chat.get("type") == "private"


def is_admin(user_id):
    return user_id in ADMIN_USER_IDS


def get_keyboard(chat, user_id):
    if not is_private_chat(chat):
        return {
            "keyboard": [
                ["🚗 Уточнить место дислокации груза"],
                ["📊 Курс"],
            ],
            "resize_keyboard": True,
        }

    if is_admin(user_id):
        return {
            "keyboard": [
                ["📊 Курс", "➕ Внести курс"],
                ["🚗 Уточнить место дислокации груза"],
                ["📣 Рассылка", "💬 Чаты"],
                ["✅ Статус"],
            ],
            "resize_keyboard": True,
        }

    return {
        "keyboard": [
            ["🚗 Уточнить место дислокации груза"],
            ["📊 Курс"],
        ],
        "resize_keyboard": True,
    }


def telegram_api(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload or {}, timeout=20)
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

    return result


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    return telegram_api("sendMessage", payload)


def answer_callback_query(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}

    if text:
        payload["text"] = text

    return telegram_api("answerCallbackQuery", payload)


def edit_message(chat_id, message_id, text):
    return telegram_api(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        },
    )


def pin_message(chat_id, message_id):
    return telegram_api(
        "pinChatMessage",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True,
        },
    )


# ============================================================
# SQLITE
# ============================================================

def db_connect():
    return sqlite3.connect(DB_NAME, timeout=30)


def init_db():
    conn = db_connect()
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_groups (
            chat_id TEXT PRIMARY KEY,
            title TEXT,
            created_at TEXT,
            mode TEXT DEFAULT 'send',
            message_id INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS logistics_snapshot (
            car_key TEXT NOT NULL,
            column_name TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (car_key, column_name)
        )
    """)

    conn.commit()
    conn.close()


def save_chat(chat_id, title):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO chats (chat_id, title, active) VALUES (?, ?, 1)",
        (str(chat_id), title),
    )
    conn.commit()
    conn.close()


def get_snapshot_value(car_key, column_name):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT value
        FROM logistics_snapshot
        WHERE car_key = ? AND column_name = ?
        """,
        (car_key, column_name),
    )
    row = cur.fetchone()
    conn.close()
    return None if row is None else row[0]


def save_snapshot_value(car_key, column_name, value):
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO logistics_snapshot (car_key, column_name, value, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(car_key, column_name)
        DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (car_key, column_name, value, now),
    )
    conn.commit()
    conn.close()


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_google_client():
    global _google_client

    if _google_client is not None:
        return _google_client

    with _google_lock:
        if _google_client is not None:
            return _google_client

        if not GOOGLE_CREDENTIALS_JSON:
            raise RuntimeError("GOOGLE_CREDENTIALS_JSON не задан")

        try:
            credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON содержит некорректный JSON"
            ) from exc

        private_key = credentials_info.get("private_key")
        if private_key:
            credentials_info["private_key"] = private_key.replace(
                "\\n",
                "\n",
            )

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=scopes,
        )

        _google_client = gspread.authorize(credentials)
        return _google_client


def get_japan_spreadsheet():
    if not JAPAN_SPREADSHEET_ID:
        raise RuntimeError("JAPAN_SPREADSHEET_ID не задан")

    return get_google_client().open_by_key(JAPAN_SPREADSHEET_ID)


def get_rates_worksheet():
    global _google_worksheet

    if _google_worksheet is not None:
        return _google_worksheet

    with _rates_lock:
        if _google_worksheet is not None:
            return _google_worksheet

        if not GOOGLE_SPREADSHEET_ID:
            raise RuntimeError("GOOGLE_SPREADSHEET_ID не задан")

        spreadsheet = get_google_client().open_by_key(
            GOOGLE_SPREADSHEET_ID
        )

        try:
            worksheet = spreadsheet.worksheet(RATES_SHEET_NAME)
        except gspread.WorksheetNotFound as exc:
            raise RuntimeError(
                f"В таблице не найден лист «{RATES_SHEET_NAME}»"
            ) from exc

        if not worksheet.row_values(1):
            worksheet.append_row(
                ["Дата", "USD/RUB", "USD/JPY", "Создано"],
                value_input_option="USER_ENTERED",
            )

        _google_worksheet = worksheet
        return _google_worksheet


def normalize_header(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def worksheet_records(sheet_name):
    worksheet = get_japan_spreadsheet().worksheet(sheet_name)
    values = worksheet.get_all_values()

    if not values:
        return []

    # Ищем строку заголовков среди первых 20 строк.
    # Это позволяет работать, даже если над таблицей есть название или пустые строки.
    normalized_rows = [
        [normalize_header(cell) for cell in row]
        for row in values[:20]
    ]

    if sheet_name == CLIENTS_SHEET_NAME:
        required_headers = {CLIENT_COLUMN, TELEGRAM_ID_COLUMN}
    else:
        required_headers = {
            CLIENT_COLUMN,
            CAR_MODEL_COLUMN,
            BODY_NUMBER_COLUMN,
            RELEASE_DATE_COLUMN,
        }

    header_row_index = None
    for index, row in enumerate(normalized_rows):
        if required_headers.issubset(set(row)):
            header_row_index = index
            break

    if header_row_index is None:
        raise RuntimeError(
            f"На листе «{sheet_name}» не найдена строка заголовков. "
            f"Ожидались колонки: {', '.join(sorted(required_headers))}"
        )

    headers = normalized_rows[header_row_index]
    records = []

    for sheet_row_number, raw_row in enumerate(
        values[header_row_index + 1:],
        start=header_row_index + 2,
    ):
        padded = raw_row + [""] * (len(headers) - len(raw_row))
        row = {
            headers[index]: str(padded[index]).strip()
            for index in range(len(headers))
            if headers[index]
        }
        row["_sheet_row"] = sheet_row_number
        records.append(row)

    return records


def get_clients_rows():
    return worksheet_records(CLIENTS_SHEET_NAME)


def get_logistics_rows():
    return worksheet_records(LOGISTICS_SHEET_NAME)


# ============================================================
# ЛОГИКА КЛИЕНТОВ И АВТОМОБИЛЕЙ
# ============================================================

def normalize_telegram_id(value):
    text = str(value or "").strip()
    return re.sub(r"\.0$", "", text)


def normalize_client_name(value):
    text = str(value or "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    text = text.replace("«", "").replace("»", "").replace('"', "")
    return text


def get_client_by_telegram_id(telegram_id, clients_rows=None):
    rows = clients_rows if clients_rows is not None else get_clients_rows()
    target_id = normalize_telegram_id(telegram_id)

    for row in rows:
        row_id = normalize_telegram_id(row.get(TELEGRAM_ID_COLUMN))
        if row_id == target_id:
            client_name = str(row.get(CLIENT_COLUMN, "")).strip()
            if client_name:
                return client_name

    return None


def get_telegram_ids_by_client(client_name, clients_rows=None):
    rows = clients_rows if clients_rows is not None else get_clients_rows()
    result = []

    for row in rows:
        row_client = str(row.get(CLIENT_COLUMN, "")).strip()
        telegram_id = normalize_telegram_id(row.get(TELEGRAM_ID_COLUMN))

        if normalize_client_name(row_client) == normalize_client_name(client_name) and telegram_id:
            try:
                result.append(int(telegram_id))
            except ValueError:
                print(
                    f"Некорректный Telegram ID у клиента {client_name}: {telegram_id}",
                    flush=True,
                )

    return list(dict.fromkeys(result))


def is_nonempty(value):
    return bool(str(value or "").strip())


def is_car_active(row):
    # Автомобиль показывается до заполнения даты выпуска.
    return not is_nonempty(row.get(RELEASE_DATE_COLUMN))


def get_active_cars_for_client(client_name, logistics_rows=None):
    rows = logistics_rows if logistics_rows is not None else get_logistics_rows()

    cars = []
    for row in rows:
        if normalize_client_name(row.get(CLIENT_COLUMN, "")) != normalize_client_name(client_name):
            continue

        if not is_car_active(row):
            continue

        body_number = str(row.get(BODY_NUMBER_COLUMN, "")).strip()
        if not body_number:
            continue

        cars.append(row)

    return cars


def format_date(value):
    text = str(value or "").strip()

    if not text:
        return "уточняется"

    # Google Sheets обычно возвращает уже отформатированную строку.
    # Дополнительно поддерживаем ISO-дату.
    for pattern in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).strftime("%d.%m.%Y")
        except ValueError:
            pass

    return text


def get_current_stage(row):
    for stage in STAGES:
        fact_value = str(row.get(stage["fact"], "")).strip()

        if not fact_value:
            return {
                "name": stage["name"],
                "date_label": stage["date_label"],
                "date": format_date(row.get(stage["plan"])),
                "completed": False,
            }

    if not is_nonempty(row.get(RELEASE_DATE_COLUMN)):
        return {
            "name": "Автомобиль прибыл в Россию и ожидает выпуска",
            "date_label": "Дата выпуска",
            "date": "уточняется",
            "completed": False,
        }

    return {
        "name": "Автомобиль выпущен",
        "date_label": "Дата выпуска",
        "date": format_date(row.get(RELEASE_DATE_COLUMN)),
        "completed": True,
    }


def format_car_status(row):
    model = str(row.get(CAR_MODEL_COLUMN, "")).strip() or "Автомобиль"
    body_number = str(row.get(BODY_NUMBER_COLUMN, "")).strip() or "не указан"
    stage = get_current_stage(row)

    return (
        f"🚗 {model}\n"
        f"🔢 Номер кузова: {body_number}\n\n"
        f"📍 Текущий этап: {stage['name']}\n"
        f"📅 {stage['date_label']}: {stage['date']}"
    )


def encode_car_row(row_number):
    return f"car:{row_number}"


def build_cars_keyboard(cars):
    buttons = []

    for car in cars:
        model = str(car.get(CAR_MODEL_COLUMN, "")).strip() or "Автомобиль"
        body = str(car.get(BODY_NUMBER_COLUMN, "")).strip()
        text = f"{model} / {body}"

        if len(text) > 60:
            text = f"{model[:28]}… / {body[-24:]}"

        buttons.append(
            [
                {
                    "text": text,
                    "callback_data": encode_car_row(car["_sheet_row"]),
                }
            ]
        )

    return {"inline_keyboard": buttons}


def show_client_cars(chat_id, telegram_id):
    clients_rows = get_clients_rows()
    client_name = get_client_by_telegram_id(telegram_id, clients_rows)

    if not client_name:
        send_message(
            chat_id,
            "Ваш аккаунт пока не привязан к дилеру.\n"
            "Обратитесь к менеджеру для подключения доступа.",
        )
        return

    logistics_rows = get_logistics_rows()
    cars = get_active_cars_for_client(client_name, logistics_rows)

    if not cars:
        send_message(
            chat_id,
            "Сейчас у вас нет автомобилей в активной перевозке.",
        )
        return

    send_message(
        chat_id,
        "Выберите автомобиль:",
        reply_markup=build_cars_keyboard(cars),
    )


def handle_car_callback(callback_query):
    callback_id = callback_query.get("id")
    data = callback_query.get("data", "")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    telegram_id = callback_query.get("from", {}).get("id")
    chat_type = message.get("chat", {}).get("type")
    access_id = telegram_id if chat_type == "private" else chat_id
    
    if not callback_id or not chat_id or not data.startswith("car:"):
        return

    answer_callback_query(callback_id)

    try:
        row_number = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        send_message(chat_id, "Не удалось определить автомобиль.")
        return

    clients_rows = get_clients_rows()
    client_name = get_client_by_telegram_id(access_id, clients_rows)

    if not client_name:
        send_message(chat_id, "Ваш аккаунт не привязан к дилеру.")
        return

    logistics_rows = get_logistics_rows()
    selected_car = next(
        (
            row
            for row in logistics_rows
            if row.get("_sheet_row") == row_number
        ),
        None,
    )

    if not selected_car:
        send_message(
            chat_id,
            "Автомобиль не найден. Обновите список и попробуйте ещё раз.",
        )
        return

    # Защита: клиент не сможет запросить чужую строку вручную.
    if normalize_client_name(selected_car.get(CLIENT_COLUMN, "")) != normalize_client_name(client_name):
        send_message(chat_id, "У вас нет доступа к этому автомобилю.")
        return

    send_message(chat_id, format_car_status(selected_car))


def build_debug_cars_message(telegram_id):
    clients_rows = get_clients_rows()
    logistics_rows = get_logistics_rows()
    client_name = get_client_by_telegram_id(telegram_id, clients_rows)

    if not client_name:
        return (
            "DEBUG\n"
            f"Telegram ID: {telegram_id}\n"
            "Клиент по Telegram ID не найден."
        )

    same_client = [
        row for row in logistics_rows
        if normalize_client_name(row.get(CLIENT_COLUMN, ""))
        == normalize_client_name(client_name)
    ]

    with_body = [
        row for row in same_client
        if is_nonempty(row.get(BODY_NUMBER_COLUMN))
    ]

    active = [
        row for row in with_body
        if is_car_active(row)
    ]

    sample_clients = []
    for row in logistics_rows:
        value = str(row.get(CLIENT_COLUMN, "")).strip()
        if value and value not in sample_clients:
            sample_clients.append(value)
        if len(sample_clients) >= 10:
            break

    examples = []
    for row in same_client[:5]:
        examples.append(
            f"• {row.get(CAR_MODEL_COLUMN, '')} / "
            f"{row.get(BODY_NUMBER_COLUMN, '')} / "
            f"ВЫПУСК ДАТА: {row.get(RELEASE_DATE_COLUMN, '') or 'ПУСТО'}"
        )

    return (
        "DEBUG АВТОМОБИЛЕЙ\n\n"
        f"Telegram ID: {telegram_id}\n"
        f"Найденный клиент: {client_name}\n"
        f"Всего строк в «{LOGISTICS_SHEET_NAME}»: {len(logistics_rows)}\n"
        f"Строк этого клиента: {len(same_client)}\n"
        f"С заполненным номером кузова: {len(with_body)}\n"
        f"Активных автомобилей: {len(active)}\n\n"
        f"Примеры строк клиента:\n"
        + ("\n".join(examples) if examples else "нет")
        + "\n\nПервые клиенты на листе:\n"
        + ("\n".join(f"• {x}" for x in sample_clients) if sample_clients else "нет")
    )


# ============================================================
# УВЕДОМЛЕНИЯ ОБ ИЗМЕНЕНИИ ДАТ
# ============================================================

def make_car_key(row):
    client = str(row.get(CLIENT_COLUMN, "")).strip()
    body = str(row.get(BODY_NUMBER_COLUMN, "")).strip()
    return f"{client}|{body}"


def build_date_notification(row, column_name, old_value, new_value):
    model = str(row.get(CAR_MODEL_COLUMN, "")).strip() or "Автомобиль"
    body = str(row.get(BODY_NUMBER_COLUMN, "")).strip() or "не указан"
    event_title, value_type = TRACKED_COLUMNS[column_name]

    if not old_value:
        if value_type == "plan":
            heading = f"📅 Добавлена дата: {event_title}"
        else:
            heading = f"✅ Обновление: {event_title}"

        return (
            f"🚗 {model}\n"
            f"🔢 Номер кузова: {body}\n\n"
            f"{heading}\n"
            f"Дата: {format_date(new_value)}"
        )

    if value_type == "plan":
        heading = f"⚠️ Изменена дата: {event_title}"
    else:
        heading = f"⚠️ Уточнена дата: {event_title}"

    return (
        f"🚗 {model}\n"
        f"🔢 Номер кузова: {body}\n\n"
        f"{heading}\n"
        f"Было: {format_date(old_value)}\n"
        f"Стало: {format_date(new_value)}"
    )


def initialize_logistics_snapshot(logistics_rows):
    saved = 0

    for row in logistics_rows:
        car_key = make_car_key(row)

        if car_key == "|":
            continue

        for column_name in TRACKED_COLUMNS:
            new_value = str(row.get(column_name, "")).strip()
            previous = get_snapshot_value(car_key, column_name)

            if previous is None:
                save_snapshot_value(car_key, column_name, new_value)
                saved += 1

    return saved


def check_logistics_updates():
    clients_rows = get_clients_rows()
    logistics_rows = get_logistics_rows()

    # Первый запуск по каждому автомобилю/полю только сохраняет значения.
    initialize_logistics_snapshot(logistics_rows)

    for row in logistics_rows:
        client_name = str(row.get(CLIENT_COLUMN, "")).strip()
        body = str(row.get(BODY_NUMBER_COLUMN, "")).strip()

        if not client_name or not body:
            continue

        car_key = make_car_key(row)
        telegram_ids = None

        for column_name in TRACKED_COLUMNS:
            new_value = str(row.get(column_name, "")).strip()
            old_value = get_snapshot_value(car_key, column_name)

            if old_value is None:
                save_snapshot_value(car_key, column_name, new_value)
                continue

            if new_value == old_value:
                continue

            # Снимок обновляем всегда, включая очистку значения.
            save_snapshot_value(car_key, column_name, new_value)

            # Очистку/удаление даты клиенту не показываем.
            if not new_value:
                print(
                    f"Дата очищена без уведомления: {car_key} / {column_name}",
                    flush=True,
                )
                continue

            if telegram_ids is None:
                telegram_ids = get_telegram_ids_by_client(
                    client_name,
                    clients_rows,
                )

            if not telegram_ids:
                print(
                    f"Нет Telegram ID для клиента {client_name}",
                    flush=True,
                )
                continue

            notification = build_date_notification(
                row,
                column_name,
                old_value,
                new_value,
            )

            for telegram_id in telegram_ids:
                try:
                    send_message(telegram_id, notification)
                except Exception as exc:
                    print(
                        f"Не удалось отправить уведомление {telegram_id}: {exc}",
                        flush=True,
                    )


def logistics_watch_loop():
    while True:
        try:
            check_logistics_updates()
        except Exception as exc:
            print(f"Ошибка проверки таблицы логистики: {exc}", flush=True)

        time.sleep(WATCH_INTERVAL_SECONDS)


# ============================================================
# СОХРАНЁННАЯ ЛОГИКА ВАЛЮТНОГО БОТА
# ============================================================

def add_broadcast_group(chat_id, title, mode="send", message_id=None):
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO broadcast_groups
        (chat_id, title, created_at, mode, message_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(chat_id), title, now, mode, message_id),
    )
    conn.commit()
    conn.close()


def update_group_message_id(chat_id, message_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE broadcast_groups SET message_id = ? WHERE chat_id = ?",
        (message_id, str(chat_id)),
    )
    conn.commit()
    conn.close()


def remove_broadcast_group(chat_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM broadcast_groups WHERE chat_id = ?",
        (str(chat_id),),
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted > 0


def get_broadcast_groups():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT chat_id, title, mode, message_id
        FROM broadcast_groups
        ORDER BY title
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def parse_sheet_number(value):
    normalized = (
        str(value or "")
        .strip()
        .replace("\xa0", "")
        .replace(" ", "")
        .replace(",", ".")
    )

    if not normalized:
        raise ValueError("Пустое значение курса")

    return float(normalized)


def save_rate(usd_rub, jpy_rub):
    if usd_rub <= 0 or jpy_rub <= 0:
        raise ValueError("Курсы должны быть больше нуля")

    usd_jpy = (usd_rub / jpy_rub) * 100
    now = datetime.now(ZoneInfo(TIMEZONE))

    worksheet = get_rates_worksheet()
    worksheet.append_row(
        [
            now.strftime("%d.%m.%Y"),
            round(usd_rub, 6),
            round(usd_jpy, 6),
            now.strftime("%d.%m.%Y %H:%M:%S"),
        ],
        value_input_option="USER_ENTERED",
    )

    print(
        "Курсы сохранены в Google Sheets: "
        f"USD/RUB={usd_rub}; USD/JPY={usd_jpy}",
        flush=True,
    )


def get_latest_rate():
    worksheet = get_rates_worksheet()
    rows = worksheet.get_all_values()

    if len(rows) < 2:
        return None

    latest_row = None
    for row in reversed(rows[1:]):
        if any(str(cell).strip() for cell in row):
            latest_row = row
            break

    if not latest_row or len(latest_row) < 3:
        return None

    date = str(latest_row[0]).strip()
    usd_rub_input = parse_sheet_number(latest_row[1])
    usd_jpy_input = parse_sheet_number(latest_row[2])

    if usd_rub_input <= 0 or usd_jpy_input <= 0:
        raise ValueError("В последней строке BOT_КУРСЫ некорректные значения")

    jpy_rub_input = (usd_rub_input / usd_jpy_input) * 100

    return (
        date,
        usd_rub_input * DISCOUNT_FACTOR,
        usd_jpy_input * DISCOUNT_FACTOR,
        jpy_rub_input * DISCOUNT_FACTOR,
    )


def has_today_rate():
    rate = get_latest_rate()
    if not rate:
        return False

    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y")
    return rate[0] == today


def build_message():
    rate = get_latest_rate()

    if not rate:
        return (
            "Курсы еще не внесены.\n\n"
            "Администратор может внести курсы в личном чате с ботом."
        )

    date, usd_rub, _usd_jpy, jpy_rub = rate
    return (
        f"📊 Курсы на сегодня {date[:5]}\n\n"
        f"💵 USD/RUB — {usd_rub:.3f}\n"
        f"🧮 JPY/RUB — {jpy_rub:.3f}"
    )


def build_pin_message():
    rate = get_latest_rate()

    if not rate:
        return "Курсы не внесены"

    date, usd_rub, _usd_jpy, jpy_rub = rate
    return f"📊 {date[:5]} | 💵{usd_rub:.3f} | 🧮{jpy_rub:.3f}"


def get_chats_message():
    conn = db_connect()
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


def get_groups_message():
    rows = get_broadcast_groups()

    if not rows:
        return (
            "Группы рассылки пока не добавлены.\n\n"
            "В группе можно использовать:\n"
            "/addgroup — обычная рассылка\n"
            "/addpin — закрепленное обновляемое сообщение"
        )

    text = "📣 Группы рассылки:\n\n"
    for index, (chat_id, title, mode, message_id) in enumerate(rows, start=1):
        mode_text = "закреп" if mode == "pin" else "обычная рассылка"
        pin_text = f"\nMessage ID: {message_id}" if message_id else ""
        text += (
            f"{index}. {title}\n"
            f"Режим: {mode_text}\n"
            f"ID: {chat_id}{pin_text}\n\n"
        )

    return text


def broadcast():
    groups = get_broadcast_groups()

    for chat_id, title, mode, message_id in groups:
        try:
            if mode == "pin":
                pin_text = build_pin_message()

                if message_id:
                    try:
                        edit_message(chat_id, message_id, pin_text)
                    except Exception:
                        sent = send_message(chat_id, pin_text)
                        new_message_id = sent["result"]["message_id"]
                        pin_message(chat_id, new_message_id)
                        update_group_message_id(chat_id, new_message_id)
                else:
                    sent = send_message(chat_id, pin_text)
                    new_message_id = sent["result"]["message_id"]
                    pin_message(chat_id, new_message_id)
                    update_group_message_id(chat_id, new_message_id)
            else:
                send_message(chat_id, build_message())

            print(f"Рассылка выполнена: {title}", flush=True)
        except Exception as exc:
            print(f"Ошибка отправки в {title}: {exc}", flush=True)


def auto_broadcast_loop():
    last_sent_date = None

    while True:
        now = datetime.now(ZoneInfo(TIMEZONE))

        if now.hour == 11 and now.minute == 0:
            today = now.strftime("%Y-%m-%d")

            if last_sent_date != today and has_today_rate():
                broadcast()
                last_sent_date = today

        time.sleep(30)


def parse_rates_from_text(text):
    clean_text = text.replace(",", ".")
    numbers = re.findall(r"\d+(?:\.\d+)?", clean_text)

    if len(numbers) < 2:
        return None

    return {
        "usd_rub": float(numbers[0]),
        "jpy_rub": float(numbers[1]),
    }


# ============================================================
# ОБРАБОТКА ОБНОВЛЕНИЙ TELEGRAM
# ============================================================

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

    if text_lower in ["/debugcars", "/debugавто"]:
        if not private_chat or not admin:
            send_message(chat_id, "Команда доступна только администратору.", reply_markup)
            return

        try:
            send_message(chat_id, build_debug_cars_message(user_id), reply_markup)
        except Exception as exc:
            send_message(chat_id, f"DEBUG ERROR: {exc}", reply_markup)
        return

    if text_lower in [
        "🚗 уточнить место дислокации груза",
        "уточнить место дислокации груза",
        "/cars",
        "/авто",
    ]:

        try:
            access_id = user_id if private_chat else chat_id
            show_client_cars(chat_id, access_id)
        except Exception as exc:
            print(f"Ошибка получения автомобилей: {exc}", flush=True)
            send_message(
                chat_id,
                "Не удалось получить данные по автомобилям. "
                "Попробуйте повторить запрос немного позже.",
                reply_markup,
            )
        return

    if text_lower == "/addgroup":
        if private_chat or not admin:
            send_message(
                chat_id,
                "Команда доступна администратору в группе.",
                reply_markup,
            )
            return

        add_broadcast_group(chat_id, title, mode="send")
        send_message(chat_id, "✅ Группа добавлена в рассылку.", reply_markup)
        return

    if text_lower == "/addpin":
        if private_chat or not admin:
            send_message(
                chat_id,
                "Команда доступна администратору в группе.",
                reply_markup,
            )
            return

        add_broadcast_group(chat_id, title, mode="pin", message_id=None)
        sent = send_message(chat_id, build_message())
        message_id = sent["result"]["message_id"]
        pin_message(chat_id, message_id)
        update_group_message_id(chat_id, message_id)
        return

    if text_lower == "/removegroup":
        if private_chat or not admin:
            send_message(
                chat_id,
                "Команда доступна администратору в группе.",
                reply_markup,
            )
            return

        removed = remove_broadcast_group(chat_id)
        send_message(
            chat_id,
            "❌ Группа удалена из рассылки."
            if removed
            else "Этой группы не было в списке.",
            reply_markup,
        )
        return

    if text_lower == "/groups":
        if not private_chat or not admin:
            send_message(
                chat_id,
                "Команда доступна только администратору.",
                reply_markup,
            )
            return

        send_message(chat_id, get_groups_message(), reply_markup)
        return

    if chat_id in waiting_for_rate:
        if not private_chat or not admin:
            waiting_for_rate.discard(chat_id)
            return

        rates = parse_rates_from_text(text)

        if not rates:
            send_message(
                chat_id,
                "Не удалось распознать курсы.\n\nПример:\n76,80\n48,30",
                reply_markup,
            )
            return

        save_rate(rates["usd_rub"], rates["jpy_rub"])
        waiting_for_rate.discard(chat_id)
        send_message(
            chat_id,
            "Курсы сохранены ✅\n\n" + build_message(),
            reply_markup,
        )
        return

    if text_lower in ["/start", "старт"]:
        if private_chat:
            send_message(
                chat_id,
                "Бот запущен ✅\n\nВыберите действие в меню:",
                reply_markup,
            )
        else:
            send_message(
                chat_id,
                "Бот запущен ✅\n\nВ группе доступна команда /курс",
                reply_markup,
            )

    elif text_lower in ["/kurs", "/курс", "📊 курс", "курс"]:
        send_message(chat_id, build_message(), reply_markup)

    elif text_lower in ["➕ внести курс", "внести курс"]:
        if not private_chat or not admin:
            send_message(
                chat_id,
                "Команда доступна только администратору.",
                reply_markup,
            )
            return

        waiting_for_rate.add(chat_id)
        send_message(
            chat_id,
            "Введите два курса:\n\n76,80\n48,30\n\n"
            "1-я строка — USD/RUB\n2-я строка — JPY/RUB",
            reply_markup,
        )

    elif text_lower.startswith("/addrate"):
        if not private_chat or not admin:
            send_message(chat_id, "Нет доступа.", reply_markup)
            return

        rates = parse_rates_from_text(text)

        if not rates:
            send_message(
                chat_id,
                "Используйте: /addrate 76,80 48,30",
                reply_markup,
            )
            return

        save_rate(rates["usd_rub"], rates["jpy_rub"])
        send_message(chat_id, "Курсы сохранены ✅\n\n" + build_message())

    elif text_lower in ["/status", "✅ статус", "статус"]:
        if admin:
            try:
                get_japan_spreadsheet()
                sheets_status = "Google Таблица подключена ✅"
            except Exception as exc:
                sheets_status = f"Ошибка Google Таблицы: {exc}"

            send_message(
                chat_id,
                f"Бот работает ✅\n{sheets_status}",
                reply_markup,
            )
        else:
            send_message(chat_id, "Бот работает ✅", reply_markup)

    elif text_lower in ["/chats", "💬 чаты", "чаты"]:
        if not private_chat or not admin:
            send_message(chat_id, "Нет доступа.", reply_markup)
            return

        send_message(chat_id, get_chats_message(), reply_markup)

    elif text_lower in ["/broadcast", "📣 рассылка", "рассылка"]:
        if not private_chat or not admin:
            send_message(chat_id, "Нет доступа.", reply_markup)
            return

        if has_today_rate():
            broadcast()
            send_message(chat_id, "Рассылка выполнена ✅", reply_markup)
        else:
            send_message(
                chat_id,
                "Курс за сегодня еще не внесен.",
                reply_markup,
            )


def handle_update(data):
    if data.get("callback_query"):
        try:
            handle_car_callback(data["callback_query"])
        except Exception as exc:
            print(f"Ошибка callback_query: {exc}", flush=True)
        return

    handle_message(data)


# ============================================================
# FLASK / RENDER
# ============================================================

@web_app.route("/", methods=["GET"])
def home():
    return "AVP Bot with Japan Logistics is running ✅"


@web_app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        handle_update(data)
    except Exception as exc:
        print(f"Ошибка обработки webhook: {exc}", flush=True)

    return "ok"


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    init_db()

    try:
        get_rates_worksheet()
        print("Таблица курсов подключена ✅", flush=True)
    except Exception as exc:
        print(f"Ошибка подключения таблицы курсов: {exc}", flush=True)

    try:
        get_japan_spreadsheet()
        print("Таблица логистики подключена ✅", flush=True)
    except Exception as exc:
        print(f"Ошибка подключения таблицы логистики: {exc}", flush=True)

    threading.Thread(
        target=auto_broadcast_loop,
        daemon=True,
    ).start()

    threading.Thread(
        target=logistics_watch_loop,
        daemon=True,
    ).start()

    port = int(os.getenv("PORT", "10000"))
    print("Бот запускается...", flush=True)

    web_app.run(
        host="0.0.0.0",
        port=port,
    )


if __name__ == "__main__":
    main()
