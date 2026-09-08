import os
import re
import json
import asyncio
import threading

import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

SHEET_NAME = os.environ.get("SHEET_NAME", "Склад")
RENDER_EXTERNAL_URL = os.environ["RENDER_EXTERNAL_URL"]
PORT = int(os.environ.get("PORT", 10000))

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_credentials():
    return Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=SCOPES,
    )


def get_spreadsheet():
    credentials = get_credentials()
    return gspread.authorize(credentials).open_by_key(SPREADSHEET_ID)


def get_stock_sheet():
    spreadsheet = get_spreadsheet()
    return spreadsheet.worksheet(SHEET_NAME)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def parse_number(value):
    """
    Преобразует значения типа:

    100
    100.00
    100,00
    "1 250,50"

    в число.
    """

    if value is None:
        return 0.0

    text = str(value).strip()

    if not text:
        return 0.0

    text = text.replace(" ", "")
    text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize_product_name(value):
    """
    Нормализация названия для поиска одинаковых товаров.

    Например:

    "Лейка"
    " лейка "
    "ЛЕЙКА"

    будут считаться одним товаром.
    """

    text = str(value or "").strip()

    text = re.sub(r"\s+", " ", text)

    return text.casefold()


def format_price(value):
    """
    Красивое отображение цены.
    """

    number = float(value)

    if number.is_integer():
        return str(int(number))

    return f"{number:.2f}".replace(".", ",")


# ============================================================
# ФОРМИРОВАНИЕ КАТАЛОГА
# ============================================================

def load_catalog():
    """
    Читает лист Склад и формирует каталог.

    Правила:

    1. Одинаковые товары объединяются.
    2. Остаток суммируется по всем строкам.
    3. Цена берется из ПОСЛЕДНЕЙ строки этого товара.
    4. Товар показывается только если:
       - суммарный остаток > 0
       - цена из последней строки > 0
    5. Остаток клиенту не показываем.
    """

    worksheet = get_stock_sheet()

    rows = worksheet.get_all_values()

    if not rows:
        return []

    headers = rows[0]

    # Определяем номера нужных колонок
    try:
        product_index = headers.index("Товар")
        stock_index = headers.index("Осталось")
        price_index = headers.index("Цена продажи")
    except ValueError as error:
        raise ValueError(
            f"Не найдена необходимая колонка в листе '{SHEET_NAME}'. "
            f"Нужны колонки: Товар, Осталось, Цена продажи. "
            f"Найдено: {headers}"
        ) from error

    products = {}

    # Читаем строки начиная со второй
    for row in rows[1:]:

        # Если строка короче заголовков — дополняем пустыми значениями
        if len(row) <= max(product_index, stock_index, price_index):
            row = row + [""] * (
                max(product_index, stock_index, price_index) + 1 - len(row)
            )

        product_name = str(row[product_index]).strip()

        if not product_name:
            continue

        stock = parse_number(row[stock_index])
        price = parse_number(row[price_index])

        key = normalize_product_name(product_name)

        if key not in products:
            products[key] = {
                "name": product_name,
                "stock": 0.0,
                "price": 0.0,
            }

        # Остаток суммируем
        products[key]["stock"] += stock

        # Цена ВСЕГДА заменяется.
        # Поэтому после прохода по таблице
        # здесь останется цена последней строки.
        products[key]["price"] = price

        # Название тоже берем из последней строки.
        products[key]["name"] = product_name

    # Оставляем только товары:
    # остаток > 0
    # цена > 0
    catalog = []

    for product in products.values():

        if product["stock"] <= 0:
            continue

        if product["price"] <= 0:
            continue

        catalog.append({
            "name": product["name"],
            "price": product["price"],
        })

    return catalog


# ============================================================
# TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("=== START COMMAND ===", flush=True)

    try:
        print("Пробуем загрузить каталог...", flush=True)

        catalog = load_catalog()

        print(
            f"Каталог загружен. Товаров: {len(catalog)}",
            flush=True
        )

    except Exception as error:
        print(
            f"ОШИБКА ЗАГРУЗКИ КАТАЛОГА: {type(error).__name__}: {error}",
            flush=True
        )

        await update.message.reply_text(
            "Не удалось загрузить каталог. Попробуйте еще раз позже."
        )

        return

    if not catalog:
        print("Каталог пустой", flush=True)

        await update.message.reply_text(
            "Сейчас в каталоге нет доступных товаров."
        )

        return

    text = "🌿 Каталог\n\n"

    for product in catalog:
        text += (
            f"• {product['name']} — "
            f"{format_price(product['price'])} грн\n"
        )

    await update.message.reply_text(text)

    print("Каталог отправлен пользователю", flush=True)


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

application = None
BOT_LOOP = None
BOT_READY = threading.Event()


def build_application():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    return app


async def run_bot():

    print("=== RUN_BOT START ===", flush=True)

    global application
    global BOT_LOOP

    BOT_LOOP = asyncio.get_running_loop()

    print("=== EVENT LOOP READY ===", flush=True)

    application = build_application()

    print("=== APPLICATION CREATED ===", flush=True)

    print("=== INITIALIZING TELEGRAM ===", flush=True)

    await application.initialize()

    print("=== TELEGRAM INITIALIZED ===", flush=True)

    print("=== STARTING TELEGRAM APPLICATION ===", flush=True)

    await application.start()

    print("=== TELEGRAM APPLICATION STARTED ===", flush=True)

    webhook_url = f"{RENDER_EXTERNAL_URL}/telegram"

    print(
        f"=== SETTING WEBHOOK: {webhook_url} ===",
        flush=True
    )

    await application.bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
    )

    print("=== WEBHOOK SET ===", flush=True)

    print("Telegram bot started", flush=True)
    print(f"Webhook: {webhook_url}", flush=True)

    BOT_READY.set()

    print("=== BOT READY ===", flush=True)

    await asyncio.Event().wait()

def run_bot_loop():
    print("=== BOT THREAD START ===", flush=True)

    try:
        asyncio.run(run_bot())
    except Exception as error:
        print(
            f"=== BOT THREAD ERROR: {type(error).__name__}: {error}",
            flush=True
        )


# ============================================================
# FLASK
# ============================================================

flask_app = Flask(__name__)


@flask_app.get("/")
def index():
    return "Customer bot is running"


@flask_app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_ready": BOT_READY.is_set(),
    })


@flask_app.post("/telegram")
def telegram_webhook():

    if not BOT_READY.is_set():
        return jsonify({
            "status": "bot_not_ready"
        }), 503

    try:
        data = request.get_json(force=True)

        update = Update.de_json(
            data,
            application.bot,
        )

        future = asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            BOT_LOOP,
        )

        future.result(timeout=50)

        return jsonify({
            "status": "ok"
        })

    except Exception as error:

        print("Webhook error:", error)

        return jsonify({
            "status": "error"
        }), 500


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    bot_thread = threading.Thread(
        target=run_bot_loop,
        daemon=True,
    )

    bot_thread.start()

    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
    )