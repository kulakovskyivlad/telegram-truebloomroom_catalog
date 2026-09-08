import os
import re
import json
import asyncio
import threading

import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request, jsonify

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


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
    3. Цена берется из последней строки этого товара.
    4. Категория берется из последней строки этого товара.
    5. Товар показывается только если:
       - суммарный остаток > 0
       - цена из последней строки > 0
    6. Остаток клиенту не показываем.
    """

    worksheet = get_stock_sheet()

    rows = worksheet.get_all_values()

    if len(rows) < 2:
        return []

    # Заголовки находятся во второй строке
    headers = rows[1]

    # Определяем номера нужных колонок
    try:
        product_index = headers.index("Товар")
        stock_index = headers.index("Осталось")
        price_index = headers.index("Цена продажи")
        category_index = headers.index("Категория")
    except ValueError as error:
        raise ValueError(
            f"Не найдена необходимая колонка в листе '{SHEET_NAME}'. "
            f"Нужны колонки: Товар, Осталось, Цена продажи, Категория. "
            f"Найдено: {headers}"
        ) from error

    products = {}

    # Товары начинаются с третьей строки
    for row in rows[2:]:

        if len(row) <= max(
            product_index,
            stock_index,
            price_index,
            category_index
        ):
            row = row + [""] * (
                max(
                    product_index,
                    stock_index,
                    price_index,
                    category_index
                ) + 1 - len(row)
            )

        product_name = str(
            row[product_index]
        ).strip()

        if not product_name:
            continue

        stock = parse_number(
            row[stock_index]
        )

        price = parse_number(
            row[price_index]
        )

        category = str(
            row[category_index]
        ).strip()

        key = normalize_product_name(
            product_name
        )

        if key not in products:
            products[key] = {
                "name": product_name,
                "stock": 0.0,
                "price": 0.0,
                "category": category,
            }

        # Остаток суммируем по всем строкам
        products[key]["stock"] += stock

        # Цена берется из последней строки
        products[key]["price"] = price

        # Название берем из последней строки
        products[key]["name"] = product_name

        # Категория берем из последней строки
        products[key]["category"] = category

    # Формируем итоговый каталог
    catalog = []

    for product in products.values():

        if product["stock"] <= 0:
            continue

        if product["price"] <= 0:
            continue

        if not product["category"]:
            continue

        catalog.append({
            "name": product["name"],
            "price": product["price"],
            "category": product["category"],
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

    # Сохраняем каталог для дальнейшей работы с кнопками
    context.user_data["catalog"] = catalog

    # Собираем уникальные категории
    categories = []

    for product in catalog:
        category = product["category"]

        if category not in categories:
            categories.append(category)

    # Создаем кнопки категорий
    keyboard = []

    for index, category in enumerate(categories):
        keyboard.append([
            InlineKeyboardButton(
                category,
                callback_data=f"category:{index}"
            )
        ])

    # Кнопка корзины
    keyboard.append([
        InlineKeyboardButton(
            "🛒 Корзина",
            callback_data="show_cart"
        )
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🌿 Каталог\n\nВыберите категорию:",
        reply_markup=reply_markup,
    )

    print(
        f"Категорий: {len(categories)}",
        flush=True
    )

    print("Каталог отправлен пользователю", flush=True)

async def category_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    print("=== CATEGORY BUTTON ===", flush=True)

    try:
        category_index = int(
            query.data.split(":")[1]
        )

    except (ValueError, IndexError):
        await query.edit_message_text(
            "Не удалось определить категорию."
        )

        return

    catalog = context.user_data.get("catalog")

    if not catalog:
        await query.edit_message_text(
            "Каталог устарел. Нажмите /start и попробуйте снова."
        )

        return

    categories = []

    for product in catalog:
        category = product["category"]

        if category not in categories:
            categories.append(category)

    if category_index >= len(categories):
        await query.edit_message_text(
            "Категория больше недоступна. Нажмите /start."
        )

        return

    selected_category = categories[category_index]

    products = [
        product
        for product in catalog
        if product["category"] == selected_category
    ]

    if not products:
        await query.edit_message_text(
            "В этой категории сейчас нет товаров."
        )

        return

    # Сохраняем товары выбранной категории
    context.user_data["category_products"] = products

    keyboard = []

    for index, product in enumerate(products):
        keyboard.append([
            InlineKeyboardButton(
                f"{product['name']} — "
                f"{format_price(product['price'])} грн",
                callback_data=f"product:{index}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "← Назад к категориям",
            callback_data="back_categories"
        )
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🌿 {selected_category}\n\n"
        "Выберите товар:",
        reply_markup=reply_markup,
    )

    print(
        f"Выбрана категория: {selected_category}",
        flush=True
    )

async def product_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    print("=== PRODUCT BUTTON ===", flush=True)

    try:
        product_index = int(
            query.data.split(":")[1]
        )

    except (ValueError, IndexError):
        await query.edit_message_text(
            "Не удалось определить товар."
        )

        return

    products = context.user_data.get(
        "category_products"
    )

    if not products:
        await query.edit_message_text(
            "Список товаров устарел. Нажмите /start."
        )

        return

    if product_index >= len(products):
        await query.edit_message_text(
            "Этот товар больше недоступен. Нажмите /start."
        )

        return

    product = products[product_index]

    # Запоминаем выбранный товар
    context.user_data["selected_product"] = product

    # Начальное количество
    context.user_data["selected_quantity"] = 1

    keyboard = [
        [
            InlineKeyboardButton(
                "−",
                callback_data="quantity_minus"
            ),
            InlineKeyboardButton(
                "1",
                callback_data="quantity_current"
            ),
            InlineKeyboardButton(
                "+",
                callback_data="quantity_plus"
            ),
        ],
        [
            InlineKeyboardButton(
                "🛒 Добавить в корзину",
                callback_data="add_to_cart"
            )
        ],
        [
            InlineKeyboardButton(
                "← Назад к товарам",
                callback_data="back_products"
            )
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🌿 {product['name']}\n\n"
        f"Цена: {format_price(product['price'])} грн\n\n"
        "Выберите количество:",
        reply_markup=reply_markup,
    )

    print(
        f"Выбран товар: {product['name']}",
        flush=True
    )

async def quantity_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    product = context.user_data.get(
        "selected_product"
    )

    if not product:
        await query.edit_message_text(
            "Товар не выбран. Нажмите /start."
        )

        return

    quantity = context.user_data.get(
        "selected_quantity",
        1
    )

    if query.data == "quantity_plus":
        quantity += 1

    elif query.data == "quantity_minus":
        quantity = max(1, quantity - 1)

    context.user_data["selected_quantity"] = quantity

    keyboard = [
        [
            InlineKeyboardButton(
                "−",
                callback_data="quantity_minus"
            ),
            InlineKeyboardButton(
                str(quantity),
                callback_data="quantity_current"
            ),
            InlineKeyboardButton(
                "+",
                callback_data="quantity_plus"
            ),
        ],
        [
            InlineKeyboardButton(
                "🛒 Добавить в корзину",
                callback_data="add_to_cart"
            )
        ],
        [
            InlineKeyboardButton(
                "← Назад к товарам",
                callback_data="back_products"
            )
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_reply_markup(
        reply_markup=reply_markup
    )

async def add_to_cart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer("Добавлено в корзину ✅")

    product = context.user_data.get(
        "selected_product"
    )

    quantity = context.user_data.get(
        "selected_quantity",
        1
    )

    if not product:
        await query.edit_message_text(
            "Товар не выбран. Нажмите /start."
        )

        return

    cart = context.user_data.setdefault(
        "cart",
        []
    )

    # Проверяем, есть ли уже этот товар
    existing_item = None

    for item in cart:
        if item["name"] == product["name"]:
            existing_item = item
            break

    if existing_item:
        existing_item["quantity"] += quantity
    else:
        cart.append({
            "name": product["name"],
            "price": product["price"],
            "quantity": quantity,
        })

    keyboard = [
        [
            InlineKeyboardButton(
                "🛒 Перейти в корзину",
                callback_data="show_cart"
            )
        ],
        [
            InlineKeyboardButton(
                "← Назад к товарам",
                callback_data="back_products"
            )
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"✅ Добавлено в корзину\n\n"
        f"{product['name']}\n"
        f"Количество: {quantity}\n"
        f"Цена: {format_price(product['price'])} грн",
        reply_markup=reply_markup,
    )

    print(
        f"Добавлено в корзину: "
        f"{product['name']} x {quantity}",
        flush=True
    )

async def show_cart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    cart = context.user_data.get(
        "cart",
        []
    )

    # Кнопка возврата к каталогу
    back_button = InlineKeyboardButton(
        "← Назад к каталогу",
        callback_data="back_categories"
    )

    # Если корзина пустая
    if not cart:
        keyboard = [
            [back_button]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "🛒 Корзина пуста.",
            reply_markup=reply_markup,
        )

        return

    text = "🛒 Ваша корзина\n\n"

    total = 0

    for item in cart:
        line_total = (
            item["price"] *
            item["quantity"]
        )

        total += line_total

        text += (
            f"• {item['name']}\n"
            f"  {item['quantity']} × "
            f"{format_price(item['price'])} грн = "
            f"{format_price(line_total)} грн\n\n"
        )

    text += (
        f"Итого: **{format_price(total)} грн**"
    )

    keyboard = [
        [back_button]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def back_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    products = context.user_data.get(
        "category_products"
    )

    if not products:
        await query.edit_message_text(
            "Список товаров устарел. Нажмите /start."
        )

        return

    keyboard = []

    for index, product in enumerate(products):
        keyboard.append([
            InlineKeyboardButton(
                f"{product['name']} — "
                f"{format_price(product['price'])} грн",
                callback_data=f"product:{index}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "← Назад к категориям",
            callback_data="back_categories"
        )
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Получаем категорию из первого товара
    category = products[0]["category"]

    await query.edit_message_text(
        f"🌿 {category}\n\n"
        "Выберите товар:",
        reply_markup=reply_markup,
    )

async def back_categories(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    catalog = context.user_data.get("catalog")

    if not catalog:
        await query.edit_message_text(
            "Каталог устарел. Нажмите /start и попробуйте снова."
        )

        return

    categories = []

    for product in catalog:
        category = product["category"]

        if category not in categories:
            categories.append(category)

    keyboard = []

    for index, category in enumerate(categories):
        keyboard.append([
            InlineKeyboardButton(
                category,
                callback_data=f"category:{index}"
            )
        ])

    # Корзина также доступна после возврата к категориям
    keyboard.append([
        InlineKeyboardButton(
            "🛒 Корзина",
            callback_data="show_cart"
        )
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🌿 Каталог\n\nВыберите категорию:",
        reply_markup=reply_markup,
    )

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
    
    app.add_handler(
        CallbackQueryHandler(
            category_button,
            pattern=r"^category:\d+$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            back_categories,
            pattern=r"^back_categories$"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            product_button,
            pattern=r"^product:\d+$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            quantity_button,
            pattern=r"^quantity_(minus|plus|current)$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            add_to_cart,
            pattern=r"^add_to_cart$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            show_cart,
            pattern=r"^show_cart$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            back_products,
            pattern=r"^back_products$"
        )
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