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
BROADCAST_GROUPS_SHEET_NAME = os.getenv("BROADCAST_GROUPS_SHEET_NAME", "BOT_РАССЫЛКА")
LOGISTICS_SNAPSHOT_SHEET_NAME = os.getenv("LOGISTICS_SNAPSHOT_SHEET_NAME", "BOT_СНИМКИ")
TEST_BROADCAST_CHAT_ID = os.getenv("TEST_BROADCAST_CHAT_ID")

CLIENTS_SHEET_NAME = os.getenv("JAPAN_CLIENTS_SHEET", "Клиенты")
LOGISTICS_SHEET_NAME = os.getenv("JAPAN_LOGISTICS_SHEET", "Сверка 2.0")

WATCH_INTERVAL_SECONDS = int(os.getenv("JAPAN_WATCH_INTERVAL_SECONDS", "300"))
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
CONTAINER_LOADING_FACT_COLUMN = "ФАКТ дата погрузки"
JAPAN_EXIT_PLAN_COLUMN = "ПЛАН выхода из Япония"
JAPAN_EXIT_FACT_COLUMN = "ФАКТ выхода из Японии"
CHINA_KOREA_ARRIVAL_COLUMN = "Дата прибытия в порт перегруза"
CHINA_EXIT_PLAN_COLUMN = "ПЛАН выхода из порта перегруза"
CHINA_EXIT_FACT_COLUMN = "ФАКТ выхода из порта перегруза"
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
    CHINA_KOREA_ARRIVAL_COLUMN: ("Дата прибытия в порт перегруза", "fact"),
    CHINA_EXIT_PLAN_COLUMN: ("Плановая дата выхода из порта перегруза", "plan"),
    CHINA_EXIT_FACT_COLUMN: ("Автомобиль вышел из порта перегруза", "fact"),
    RUSSIA_ARRIVAL_PLAN_COLUMN: ("Плановая дата прибытия в Россию", "plan"),
    RUSSIA_ARRIVAL_FACT_COLUMN: ("Автомобиль прибыл в Россию", "fact"),
    RELEASE_DATE_COLUMN: ("Автомобиль выпущен", "fact"),
    CONTAINER_LOADING_FACT_COLUMN: ("Автомобиль погружен","fact"),
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
        "name": "Ожидается выход из порта перегруза",
        "plan": CHINA_EXIT_PLAN_COLUMN,
        "fact": CHINA_EXIT_FACT_COLUMN,
        "date_label": "Плановая дата выхода из порта перегруза",
    },
    {
        "name": "Автомобиль следует в Россию",
        "plan": RUSSIA_ARRIVAL_PLAN_COLUMN,
        "fact": RUSSIA_ARRIVAL_FACT_COLUMN,
        "date_label": "Плановая дата прибытия в РФ",
    },
]

waiting_for_rate = set()
waiting_for_custom_broadcast = set()
pending_custom_broadcast = {}
web_app = Flask(__name__)

_google_client = None
_google_worksheet = None
_broadcast_groups_worksheet = None
_snapshot_worksheet = None
_google_lock = threading.Lock()
_rates_lock = threading.Lock()
_storage_lock = threading.Lock()


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


def get_storage_spreadsheet():
    """Постоянное техническое хранилище бота в Google Sheets."""
    if not GOOGLE_SPREADSHEET_ID:
        raise RuntimeError("GOOGLE_SPREADSHEET_ID не задан")

    return get_google_client().open_by_key(GOOGLE_SPREADSHEET_ID)


def _get_or_create_worksheet(title, headers, rows=1000, cols=10):
    spreadsheet = get_storage_spreadsheet()

    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=title,
            rows=rows,
            cols=cols,
        )

    current_headers = worksheet.row_values(1)

    if not current_headers:
        worksheet.update(
            values=[headers],
            range_name=f"A1:{chr(64 + len(headers))}1",
        )

    return worksheet


def get_broadcast_groups_worksheet():
    """
    Реестр рассылки хранится в JAPAN_SPREADSHEET_ID на листе BOT_РАССЫЛКА.

    Колонки:
    Клиент | Chat ID | Активен | Режим | Message ID | Обновлено
    """
    global _broadcast_groups_worksheet

    if _broadcast_groups_worksheet is not None:
        return _broadcast_groups_worksheet

    with _storage_lock:
        if _broadcast_groups_worksheet is None:
            spreadsheet = get_japan_spreadsheet()

            try:
                worksheet = spreadsheet.worksheet(
                    BROADCAST_GROUPS_SHEET_NAME
                )
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(
                    title=BROADCAST_GROUPS_SHEET_NAME,
                    rows=1000,
                    cols=6,
                )

            headers = [
                "Клиент",
                "Chat ID",
                "Активен",
                "Режим",
                "Message ID",
                "Обновлено",
            ]

            current_headers = worksheet.row_values(1)

            if not current_headers:
                worksheet.update(
                    values=[headers],
                    range_name="A1:F1",
                )
            elif current_headers[:6] != headers:
                # Не перезаписываем существующие данные автоматически.
                # Если лист уже есть в другом формате — создаём правильные заголовки
                # только если ниже нет данных.
                if len(worksheet.get_all_values()) <= 1:
                    worksheet.update(
                        values=[headers],
                        range_name="A1:F1",
                    )

            _broadcast_groups_worksheet = worksheet

    return _broadcast_groups_worksheet


def get_snapshot_worksheet():
    global _snapshot_worksheet

    if _snapshot_worksheet is not None:
        return _snapshot_worksheet

    with _storage_lock:
        if _snapshot_worksheet is None:
            _snapshot_worksheet = _get_or_create_worksheet(
                LOGISTICS_SNAPSHOT_SHEET_NAME,
                [
                    "Car Key",
                    "Колонка",
                    "Значение",
                    "Обновлено",
                ],
                rows=5000,
                cols=4,
            )

    return _snapshot_worksheet


def load_snapshot_state():
    """Читает постоянный снимок дат из Google Sheets."""
    worksheet = get_snapshot_worksheet()
    values = worksheet.get_all_values()

    snapshot = {}
    row_numbers = {}

    for row_number, row in enumerate(values[1:], start=2):
        if len(row) < 2:
            continue

        car_key = str(row[0]).strip()
        column_name = str(row[1]).strip()

        if not car_key or not column_name:
            continue

        value = str(row[2]).strip() if len(row) > 2 else ""
        key = (car_key, column_name)

        snapshot[key] = value
        row_numbers[key] = row_number

    return snapshot, row_numbers


def initialize_snapshot_in_google(logistics_rows):
    """Первичная инициализация без рассылки старых изменений."""
    worksheet = get_snapshot_worksheet()
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

    rows = [[
        "Car Key",
        "Колонка",
        "Значение",
        "Обновлено",
    ]]

    for row in logistics_rows:
        car_key = make_car_key(row)

        if car_key == "|":
            continue

        for column_name in TRACKED_COLUMNS:
            rows.append([
                car_key,
                column_name,
                str(row.get(column_name, "")).strip(),
                now,
            ])

    worksheet.clear()

    if rows:
        worksheet.update(
            values=rows,
            range_name=f"A1:D{len(rows)}",
        )

    print(
        f"BOT_СНИМКИ инициализирован: {max(len(rows) - 1, 0)} значений. "
        "Старые изменения не рассылаются.",
        flush=True,
    )


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


def get_cars_for_client(client_name, logistics_rows=None):
    """Возвращает все автомобили клиента с заполненным номером кузова.

    Активные автомобили идут первыми, завершённые — после них.
    """
    rows = logistics_rows if logistics_rows is not None else get_logistics_rows()

    cars = []
    for row in rows:
        if normalize_client_name(row.get(CLIENT_COLUMN, "")) != normalize_client_name(client_name):
            continue

        body_number = str(row.get(BODY_NUMBER_COLUMN, "")).strip()
        if not body_number:
            continue

        cars.append(row)

    cars.sort(
        key=lambda row: (
            1 if is_nonempty(row.get(RELEASE_DATE_COLUMN)) else 0,
            str(row.get(CAR_MODEL_COLUMN, "")).casefold(),
            str(row.get(BODY_NUMBER_COLUMN, "")).casefold(),
        )
    )
    return cars


def get_active_cars_for_client(client_name, logistics_rows=None):
    """Оставлено для совместимости с debug-функциями."""
    return [
        row
        for row in get_cars_for_client(client_name, logistics_rows)
        if is_car_active(row)
    ]


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
    """Определяет фактический статус автомобиля.

    Плановые даты не меняют текущий статус.
    """

    if is_nonempty(row.get(RELEASE_DATE_COLUMN)):
        return {
            "code": "released",
            "name": "Автомобиль выпущен",
            "completed": True,
        }

    if is_nonempty(row.get(RUSSIA_ARRIVAL_FACT_COLUMN)):
        return {
            "code": "russia_arrived",
            "name": "Автомобиль прибыл в Россию и ожидает выпуска",
            "completed": False,
        }

    if is_nonempty(row.get(CHINA_EXIT_FACT_COLUMN)):
        return {
            "code": "left_china",
            "name": "Автомобиль следует в Россию",
            "completed": False,
        }

    if is_nonempty(row.get(CHINA_KOREA_ARRIVAL_COLUMN)):
        return {
            "code": "china_arrived",
            "name": "Автомобиль находится в порту перегруза и ожидает отправку",
            "completed": False,
        }

    if is_nonempty(row.get(JAPAN_EXIT_FACT_COLUMN)):
        return {
            "code": "left_japan",
            "name": "Автомобиль следует в порт перегруза",
            "completed": False,
        }

    if is_nonempty(row.get(CONTAINER_LOADING_FACT_COLUMN)):
        return {
            "code": "loaded",
            "name": "Автомобиль погружен, ожидает отправку из Японии",
            "completed": False,
        }

    if is_nonempty(row.get(YARD_FACT_COLUMN)):
        return {
            "code": "on_yard",
            "name": "Автомобиль на ярде, ожидает погрузку",
            "completed": False,
        }

    return {
        "code": "before_yard",
        "name": "Автомобиль ожидает доставку на ярд",
        "completed": False,
    }

def get_stage_plan_lines(row, stage_code):
    """Возвращает только планы, актуальные для текущего этапа."""

    plans_by_stage = {
        "before_yard": [
            ("План доставки на ярд", YARD_PLAN_COLUMN),
            ("План выхода из Японии", JAPAN_EXIT_PLAN_COLUMN),
            ("План прибытия в РФ", RUSSIA_ARRIVAL_PLAN_COLUMN),
        ],
        "on_yard": [
            ("План выхода из Японии", JAPAN_EXIT_PLAN_COLUMN),
            ("План прибытия в РФ", RUSSIA_ARRIVAL_PLAN_COLUMN),
        ],
        "left_japan": [
            ("План выхода из порта перегруза", CHINA_EXIT_PLAN_COLUMN),
            ("План прибытия в РФ", RUSSIA_ARRIVAL_PLAN_COLUMN),
        ],
        "china_arrived": [
            ("План выхода из порта перегруза", CHINA_EXIT_PLAN_COLUMN),
            ("План прибытия в РФ", RUSSIA_ARRIVAL_PLAN_COLUMN),
        ],
        "left_china": [
            ("План прибытия в РФ", RUSSIA_ARRIVAL_PLAN_COLUMN),
        ],
        "russia_arrived": [],
        "released": [],
    }

    lines = []

    for label, column in plans_by_stage.get(stage_code, []):
        value = row.get(column)

        if is_nonempty(value):
            lines.append(
                f"📅 {label}: {format_date(value)}"
            )

    return lines

def build_car_history(row):
    """Формирует хронологию по заполненным фактическим датам."""
    events = [
        ("Доставлен на ярд", YARD_FACT_COLUMN),
        ("Вышел из Японии", JAPAN_EXIT_FACT_COLUMN),
        ("Прибыл в порт перегруза", CHINA_KOREA_ARRIVAL_COLUMN),
        ("Вышел из порта перегруза", CHINA_EXIT_FACT_COLUMN),
        ("Прибыл в Россию", RUSSIA_ARRIVAL_FACT_COLUMN),
        ("Выпущен", RELEASE_DATE_COLUMN),
    ]

    lines = []
    for label, column in events:
        value = row.get(column)
        if is_nonempty(value):
            lines.append(f"• {format_date(value)} — {label}")

    return lines


def format_car_status(row):
    model = str(row.get(CAR_MODEL_COLUMN, "")).strip() or "Автомобиль"
    body_number = str(row.get(BODY_NUMBER_COLUMN, "")).strip() or "не указан"
    stage = get_current_stage(row)

    text = (
        f"🚗 {model}\n"
        f"🔢 Номер кузова: {body_number}\n\n"
        f"📍 Текущий статус: {stage['name']}"
    )

    if stage["code"] == "loaded":
        japan_exit_plan = format_date(
            row.get(JAPAN_EXIT_PLAN_COLUMN)
        )

        text += (
            f"\n\n"
            f"📅 Планируемая дата выхода из Японии: "
            f"{japan_exit_plan}"
        )

    return text

def normalize_body_number(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def encode_car_body(body_number):
    return f"car:{normalize_body_number(body_number)}"


def build_cars_keyboard(cars):
    buttons = []

    for car in cars:
        model = str(car.get(CAR_MODEL_COLUMN, "")).strip() or "Автомобиль"
        body = str(car.get(BODY_NUMBER_COLUMN, "")).strip()
        completed = is_nonempty(car.get(RELEASE_DATE_COLUMN))
        suffix = " ✅" if completed else ""
        text = f"{model} / {body}{suffix}"

        if len(text) > 60:
            text = f"{model[:28]}… / {body[-22:]}{suffix}"

        buttons.append(
            [
                {
                    "text": text,
                    "callback_data": encode_car_body(body),
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
    cars = get_cars_for_client(client_name, logistics_rows)

    if not cars:
        send_message(
            chat_id,
            "Автомобили по вашему аккаунту не найдены.",
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
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    user_id = callback_query.get("from", {}).get("id")
    access_id = user_id if chat_type == "private" else chat_id

    if not callback_id or not chat_id or not data.startswith("car:"):
        return

    answer_callback_query(callback_id)
    requested_body = normalize_body_number(data.split(":", 1)[1])

    if not requested_body:
        send_message(chat_id, "Не удалось определить номер кузова.")
        return

    clients_rows = get_clients_rows()
    client_name = get_client_by_telegram_id(access_id, clients_rows)

    if not client_name:
        send_message(chat_id, "Этот аккаунт или чат не привязан к дилеру.")
        return

    logistics_rows = get_logistics_rows()
    selected_car = next(
        (
            row
            for row in logistics_rows
            if normalize_body_number(row.get(BODY_NUMBER_COLUMN)) == requested_body
            and normalize_client_name(row.get(CLIENT_COLUMN, ""))
            == normalize_client_name(client_name)
        ),
        None,
    )

    if not selected_car:
        send_message(
            chat_id,
            "Автомобиль не найден. Обновите список и попробуйте ещё раз.",
        )
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


def check_logistics_updates():
    clients_rows = get_clients_rows()
    logistics_rows = get_logistics_rows()

    snapshot, row_numbers = load_snapshot_state()

    # Первый запуск после перехода на Google-хранилище:
    # фиксируем текущее состояние и ничего старого не рассылаем.
    if not snapshot:
        initialize_snapshot_in_google(logistics_rows)
        return

    worksheet = get_snapshot_worksheet()
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

    rows_to_append = []
    batch_updates = []

    for row in logistics_rows:
        client_name = str(row.get(CLIENT_COLUMN, "")).strip()
        body = str(row.get(BODY_NUMBER_COLUMN, "")).strip()

        if not client_name or not body:
            continue

        car_key = make_car_key(row)
        telegram_ids = None

        for column_name in TRACKED_COLUMNS:
            key = (car_key, column_name)
            new_value = str(row.get(column_name, "")).strip()

            # Новая машина или новая колонка — принимаем как исходное состояние.
            if key not in snapshot:
                rows_to_append.append([
                    car_key,
                    column_name,
                    new_value,
                    now,
                ])
                snapshot[key] = new_value
                continue

            old_value = snapshot[key]

            if new_value == old_value:
                continue

            snapshot[key] = new_value
            existing_row_number = row_numbers.get(key)

            if existing_row_number:
                batch_updates.append({
                    "range": f"C{existing_row_number}:D{existing_row_number}",
                    "values": [[new_value, now]],
                })
            else:
                rows_to_append.append([
                    car_key,
                    column_name,
                    new_value,
                    now,
                ])

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

    if batch_updates:
        worksheet.batch_update(batch_updates)

    if rows_to_append:
        worksheet.append_rows(
            rows_to_append,
            value_input_option="USER_ENTERED",
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

def is_broadcast_active(value):
    normalized = str(value or "").strip().casefold()

    return normalized in {
        "да",
        "активен",
        "активно",
        "active",
        "yes",
        "true",
        "1",
        "✅",
        "+",
    }


def _find_broadcast_group_row(chat_id):
    worksheet = get_broadcast_groups_worksheet()
    values = worksheet.get_all_values()
    target = normalize_telegram_id(chat_id)

    for row_number, row in enumerate(values[1:], start=2):
        row_chat_id = normalize_telegram_id(
            row[1] if len(row) > 1 else ""
        )

        if row_chat_id == target:
            return row_number, row

    return None, None


def migrate_old_broadcast_groups():
    """
    Один раз подтягивает старые записи из BOT_ГРУППЫ
    в GOOGLE_SPREADSHEET_ID и отмечает их активными в BOT_РАССЫЛКА.

    Старый лист не удаляется и не изменяется.
    """
    if not GOOGLE_SPREADSHEET_ID:
        return 0

    try:
        old_spreadsheet = get_google_client().open_by_key(
            GOOGLE_SPREADSHEET_ID
        )

        try:
            old_worksheet = old_spreadsheet.worksheet("BOT_ГРУППЫ")
        except gspread.WorksheetNotFound:
            return 0

        old_values = old_worksheet.get_all_values()

        if len(old_values) <= 1:
            return 0

        registry = get_broadcast_groups_worksheet()
        registry_values = registry.get_all_values()

        existing_ids = {
            normalize_telegram_id(row[1])
            for row in registry_values[1:]
            if len(row) > 1 and normalize_telegram_id(row[1])
        }

        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        rows_to_append = []

        # Старый формат:
        # Chat ID | Название | Режим | Message ID | Добавлено
        for row in old_values[1:]:
            old_chat_id = normalize_telegram_id(
                row[0] if len(row) > 0 else ""
            )

            if not old_chat_id or old_chat_id in existing_ids:
                continue

            old_title = str(
                row[1] if len(row) > 1 else ""
            ).strip()

            old_mode = str(
                row[2] if len(row) > 2 else "send"
            ).strip() or "send"

            old_message_id = str(
                row[3] if len(row) > 3 else ""
            ).strip()

            rows_to_append.append([
                old_title,
                old_chat_id,
                "ДА",
                old_mode,
                old_message_id,
                now,
            ])

            existing_ids.add(old_chat_id)

        if rows_to_append:
            registry.append_rows(
                rows_to_append,
                value_input_option="USER_ENTERED",
            )

        return len(rows_to_append)

    except Exception as exc:
        print(
            f"Ошибка миграции старого BOT_ГРУППЫ: {exc}",
            flush=True,
        )
        return 0


def sync_broadcast_registry_from_clients():
    """
    Добавляет в BOT_РАССЫЛКА всех клиентов из листа «Клиенты».

    Новые строки создаются как НЕАКТИВНЫЕ, чтобы случайно не отправить
    рассылку всем клиентам. Уже выставленный статус не изменяется.
    """
    worksheet = get_broadcast_groups_worksheet()
    values = worksheet.get_all_values()

    existing_by_id = {}

    for row_number, row in enumerate(values[1:], start=2):
        chat_id = normalize_telegram_id(
            row[1] if len(row) > 1 else ""
        )

        if chat_id:
            existing_by_id[chat_id] = (row_number, row)

    clients_rows = get_clients_rows()
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

    rows_to_append = []
    updates = []

    for client_row in clients_rows:
        client_name = str(
            client_row.get(CLIENT_COLUMN, "")
        ).strip()

        chat_id = normalize_telegram_id(
            client_row.get(TELEGRAM_ID_COLUMN, "")
        )

        if not client_name or not chat_id:
            continue

        existing = existing_by_id.get(chat_id)

        if existing:
            row_number, row = existing

            # Если название клиента поменялось/было пустое — обновляем только его.
            current_name = str(
                row[0] if len(row) > 0 else ""
            ).strip()

            if current_name != client_name:
                updates.append({
                    "range": f"A{row_number}",
                    "values": [[client_name]],
                })

            continue

        rows_to_append.append([
            client_name,
            chat_id,
            "НЕТ",
            "send",
            "",
            now,
        ])

        existing_by_id[chat_id] = (None, rows_to_append[-1])

    if updates:
        worksheet.batch_update(updates)

    if rows_to_append:
        worksheet.append_rows(
            rows_to_append,
            value_input_option="USER_ENTERED",
        )

    if rows_to_append:
        print(
            f"BOT_РАССЫЛКА: автоматически добавлено "
            f"{len(rows_to_append)} новых чатов из листа «Клиенты»",
            flush=True,
        )

    return len(rows_to_append)


def add_broadcast_group(chat_id, title, mode="send", message_id=None):
    """
    Команда /addgroup остаётся для совместимости,
    но теперь просто создаёт/активирует строку в BOT_РАССЫЛКА.
    """
    worksheet = get_broadcast_groups_worksheet()
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

    row_number, row = _find_broadcast_group_row(chat_id)

    values = [[
        str(title or ""),
        normalize_telegram_id(chat_id),
        "ДА",
        str(mode or "send"),
        "" if message_id is None else str(message_id),
        now,
    ]]

    if row_number:
        worksheet.update(
            values=values,
            range_name=f"A{row_number}:F{row_number}",
        )
    else:
        worksheet.append_row(
            values[0],
            value_input_option="USER_ENTERED",
        )


def update_group_message_id(chat_id, message_id):
    worksheet = get_broadcast_groups_worksheet()
    row_number, row = _find_broadcast_group_row(chat_id)

    if not row_number:
        return

    while len(row) < 6:
        row.append("")

    row[4] = str(message_id)
    row[5] = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

    worksheet.update(
        values=[row[:6]],
        range_name=f"A{row_number}:F{row_number}",
    )


def remove_broadcast_group(chat_id):
    """
    Ничего не удаляем физически.
    /removegroup только ставит Активен = НЕТ.
    """
    worksheet = get_broadcast_groups_worksheet()
    row_number, row = _find_broadcast_group_row(chat_id)

    if not row_number:
        return False

    worksheet.update(
        values=[["НЕТ"]],
        range_name=f"C{row_number}",
    )
    worksheet.update(
        values=[[datetime.now(ZoneInfo(TIMEZONE)).isoformat()]],
        range_name=f"F{row_number}",
    )

    return True


def get_broadcast_groups():
    """
    Возвращает ТОЛЬКО строки, где Активен = ДА/АКТИВЕН/✅ и т.п.
    Это единственный источник получателей рассылки.
    """
    sync_broadcast_registry_from_clients()

    worksheet = get_broadcast_groups_worksheet()
    values = worksheet.get_all_values()

    rows = []

    for row in values[1:]:
        if not row:
            continue

        title = str(
            row[0] if len(row) > 0 else ""
        ).strip()

        chat_id = normalize_telegram_id(
            row[1] if len(row) > 1 else ""
        )

        active = str(
            row[2] if len(row) > 2 else ""
        ).strip()

        mode = str(
            row[3] if len(row) > 3 else "send"
        ).strip() or "send"

        message_id_raw = str(
            row[4] if len(row) > 4 else ""
        ).strip()

        if not chat_id or not is_broadcast_active(active):
            continue

        try:
            message_id = (
                int(message_id_raw)
                if message_id_raw
                else None
            )
        except ValueError:
            message_id = None

        rows.append((
            chat_id,
            title,
            mode,
            message_id,
        ))

    rows.sort(
        key=lambda item: (item[1] or "").casefold()
    )

    return rows


def log_broadcast_groups_count(prefix="BOT_РАССЫЛКА"):
    try:
        groups = get_broadcast_groups()
        count = len(groups)

        print(
            f"{prefix}: активно {count} чатов",
            flush=True,
        )

        return count

    except Exception as exc:
        print(
            f"{prefix}: ошибка чтения списка чатов: {exc}",
            flush=True,
        )

        return 0


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
            "📣 Сейчас нет активных чатов для рассылки.\n\n"
            "Управление рассылкой выполняется на листе "
            f"«{BROADCAST_GROUPS_SHEET_NAME}».\n"
            "Чтобы включить чат, поставьте в колонке «Активен» значение «ДА»."
        )

    text = (
        f"📣 Активные чаты рассылки: {len(rows)}\n\n"
    )

    for index, (chat_id, title, mode, message_id) in enumerate(
        rows,
        start=1,
    ):
        mode_text = (
            "закреп"
            if mode == "pin"
            else "обычная рассылка"
        )

        text += (
            f"{index}. {title}\n"
            f"ID: {chat_id}\n"
            f"Режим: {mode_text}\n\n"
        )

    return text


def send_custom_broadcast(text):
    groups = get_broadcast_groups()

    print(
        f"Ручная рассылка: найдено {len(groups)} чатов",
        flush=True,
    )

    if not groups:
        print(
            "Ручная рассылка отменена: BOT_ГРУППЫ пуст",
            flush=True,
        )
        return 0, 0

    success = 0
    errors = 0

    # Тестовый режим:
    # если в Render задан TEST_BROADCAST_CHAT_ID,
    # рассылка идет только в этот чат.
    if TEST_BROADCAST_CHAT_ID:
        groups = [
            group
            for group in groups
            if str(group[0]) == str(TEST_BROADCAST_CHAT_ID)
        ]

    sent_chat_ids = set()

    for chat_id, title, mode, message_id in groups:
        if str(chat_id) in sent_chat_ids:
            continue

        try:
            send_message(chat_id, text)

            sent_chat_ids.add(str(chat_id))
            success += 1

            print(
                f"Массовая рассылка отправлена: {title} ({chat_id})",
                flush=True,
            )

        except Exception as exc:
            errors += 1

            print(
                f"Ошибка массовой рассылки в {title} ({chat_id}): {exc}",
                flush=True,
            )

    return success, errors

def broadcast():
    groups = get_broadcast_groups()

    print(
        f"Автоматическая рассылка курса: найдено {len(groups)} чатов",
        flush=True,
    )

    if not groups:
        print(
            "Автоматическая рассылка курса отменена: BOT_ГРУППЫ пуст",
            flush=True,
        )
        return 0, 0

    success = 0
    errors = 0

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

            success += 1
            print(
                f"Рассылка выполнена: {title} ({chat_id})",
                flush=True,
            )

        except Exception as exc:
            errors += 1
            print(
                f"Ошибка отправки в {title} ({chat_id}): {exc}",
                flush=True,
            )

    print(
        f"Автоматическая рассылка завершена: успешно {success}, ошибок {errors}",
        flush=True,
    )
    return success, errors



def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    init_db()

    try:
        get_broadcast_groups_worksheet()
        get_snapshot_worksheet()

        migrated_count = migrate_old_broadcast_groups()
        synced_count = sync_broadcast_registry_from_clients()

        print(
            "Постоянное Google-хранилище бота подключено ✅",
            flush=True,
        )

        if migrated_count:
            print(
                f"BOT_РАССЫЛКА: перенесено {migrated_count} старых чатов "
                "из BOT_ГРУППЫ",
                flush=True,
            )

        if synced_count:
            print(
                f"BOT_РАССЫЛКА: добавлено {synced_count} чатов "
                "из листа «Клиенты»",
                flush=True,
            )

        log_broadcast_groups_count()
    except Exception as exc:
        print(f"Ошибка подключения Google-хранилища бота: {exc}", flush=True)

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

