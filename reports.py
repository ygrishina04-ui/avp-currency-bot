import sqlite3
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TIMEZONE = "Asia/Vladivostok"
DB_NAME = "daily_summaries.db"


def init_reports_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            summary_date TEXT,
            created INTEGER,
            calculated INTEGER,
            not_created INTEGER,
            hanging INTEGER,
            without_feedback INTEGER,
            passed_rate INTEGER,
            passed_clients TEXT,
            raw_text TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def parse_summary(text):
    data = {
        "summary_date": None,
        "created": 0,
        "calculated": 0,
        "not_created": 0,
        "hanging": 0,
        "without_feedback": 0,
        "passed_rate": 0,
        "passed_clients": []
    }

    date_match = re.search(r"Сводка за:\s*(\d{2}/\d{2})", text, re.IGNORECASE)
    if date_match:
        data["summary_date"] = date_match.group(1)

    patterns = {
        "created": r"Запросов заведено\s*(\d+)",
        "calculated": r"Запросов посчитано\s*(\d+)",
        "not_created": r"Запросов не заведено\s*(\d+)",
        "hanging": r"Зависшие запросы\s*(\d+)",
        "without_feedback": r"Запросов без ОС\s*(\d+)",
        "passed_rate": r"Прошли по ставке:?\s*(\d+)"
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            data[key] = int(match.group(1))

    lines = text.splitlines()
    collect_clients = False

    for line in lines:
        line = line.strip()

        if not line:
            continue

        if line.lower().startswith("прошли по ставке"):
            collect_clients = True
            continue

        if collect_clients:
            data["passed_clients"].append(line)

    return data


def save_daily_summary(message):
    parsed = parse_summary(message.text)

    user = message.from_user
    now = datetime.now(ZoneInfo(TIMEZONE))

    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    passed_clients_text = "\n".join(parsed["passed_clients"])

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO daily_summaries (
            chat_id,
            user_id,
            username,
            full_name,
            summary_date,
            created,
            calculated,
            not_created,
            hanging,
            without_feedback,
            passed_rate,
            passed_clients,
            raw_text,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message.chat.id,
        user.id,
        user.username,
        full_name,
        parsed["summary_date"],
        parsed["created"],
        parsed["calculated"],
        parsed["not_created"],
        parsed["hanging"],
        parsed["without_feedback"],
        parsed["passed_rate"],
        passed_clients_text,
        message.text,
        now.strftime("%Y-%m-%d %H:%M:%S")
    ))

    conn.commit()
    conn.close()


def build_weekly_report(chat_id):
    today = datetime.now(ZoneInfo(TIMEZONE)).date()

    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)

    start_dt = datetime.combine(last_monday, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
    end_dt = datetime.combine(last_sunday, datetime.max.time()).strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT 
            summary_date,
            full_name,
            created,
            calculated,
            not_created,
            hanging,
            without_feedback,
            passed_rate,
            passed_clients
        FROM daily_summaries
        WHERE chat_id = ?
        AND created_at BETWEEN ? AND ?
        ORDER BY created_at ASC
    """, (chat_id, start_dt, end_dt))

    rows = cur.fetchall()
    conn.close()

    if not rows:
        return "За прошлую неделю сводок не найдено."

    total_created = 0
    total_calculated = 0
    total_not_created = 0
    total_hanging = 0
    total_without_feedback = 0
    total_passed_rate = 0
    all_clients = []

    daily_blocks = []

    for row in rows:
        summary_date = row[0]
        full_name = row[1]
        created = row[2]
        calculated = row[3]
        not_created = row[4]
        hanging = row[5]
        without_feedback = row[6]
        passed_rate = row[7]
        passed_clients = row[8]

        total_created += created
        total_calculated += calculated
        total_not_created += not_created
        total_hanging += hanging
        total_without_feedback += without_feedback
        total_passed_rate += passed_rate

        if passed_clients:
            all_clients.extend(passed_clients.splitlines())

        daily_blocks.append(
            f"📅 {summary_date} — {full_name}\n"
            f"Заведено: {created}, посчитано: {calculated}, не заведено: {not_created}, "
            f"зависшие: {hanging}, без ОС: {without_feedback}, прошли по ставке: {passed_rate}"
        )

    clients_text = "\n".join([f"— {client}" for client in all_clients]) if all_clients else "— нет"

    report = (
        f"📊 Итоговая сводка за неделю\n"
        f"{last_monday.strftime('%d.%m')}–{last_sunday.strftime('%d.%m')}\n\n"
        f"ИТОГО:\n"
        f"Запросов заведено: {total_created}\n"
        f"Запросов посчитано: {total_calculated}\n"
        f"Запросов не заведено: {total_not_created}\n"
        f"Зависшие запросы: {total_hanging}\n"
        f"Запросов без ОС: {total_without_feedback}\n"
        f"Прошли по ставке: {total_passed_rate}\n\n"
        f"Клиенты, которые прошли по ставке:\n"
        f"{clients_text}\n\n"
        f"Детализация по дням:\n"
        f"{chr(10).join(daily_blocks)}"
    )

    return report


def send_long_message(bot, chat_id, text):
    max_len = 3900

    for i in range(0, len(text), max_len):
        bot.send_message(chat_id, text[i:i + max_len])


def register_reports_handlers(bot):

    @bot.message_handler(func=lambda message: message.chat.type in ["group", "supergroup"])
    def collect_daily_summary(message):
        if not message.text:
            return

        text = message.text.strip()

        if text.lower().startswith("сводка за:"):
            save_daily_summary(message)
            bot.reply_to(message, "✅ Сводка принята")

    @bot.message_handler(commands=["weekly_report"])
    def manual_weekly_report(message):
        report = build_weekly_report(message.chat.id)
        send_long_message(bot, message.chat.id, report)


def weekly_report_loop(bot):
    last_sent_date = None

    while True:
        now = datetime.now(ZoneInfo(TIMEZONE))

        if now.weekday() == 0 and now.hour == 9 and now.minute == 0:
            today = now.strftime("%Y-%m-%d")

            if last_sent_date != today:
                conn = sqlite3.connect(DB_NAME)
                cur = conn.cursor()

                cur.execute("""
                    SELECT DISTINCT chat_id 
                    FROM daily_summaries
                """)

                chats = cur.fetchall()
                conn.close()

                for chat in chats:
                    chat_id = chat[0]
                    report = build_weekly_report(chat_id)
                    send_long_message(bot, chat_id, report)

                last_sent_date = today

        time.sleep(30)


def start_reports(bot):
    init_reports_db()
    register_reports_handlers(bot)

    threading.Thread(
        target=weekly_report_loop,
        args=(bot,),
        daemon=True
    ).start()

    print("Модуль отчетов запущен")
