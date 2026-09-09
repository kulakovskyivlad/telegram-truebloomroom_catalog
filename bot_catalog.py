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
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
ADMIN_CHAT_IDS = [
    int(chat_id.strip())
    for chat_id in os.environ["ADMIN_CHAT_IDS"].split(",")
    if chat_id.strip()
]

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

def get_orders_sheet():
    spreadsheet = get_spreadsheet()
    return spreadsheet.worksheet("Заказы_бот")

async def notify_admins(
    context: ContextTypes.DEFAULT_TYPE,
    order_number,
    cart,
    customer,
    telegram_id,
    telegram_username,
    telegram_name,
    order_date,
):
    total = 0

    text = (
        f"🔔 НОВЫЙ ЗАКАЗ №{order_number}\n\n"
        f"👤 Покупатель: {customer.get('name', '')}\n"
        f"📞 Телефон: {customer.get('phone', '')}\n"
        f"📍 Город: {customer.get('city', '')}\n\n"
        f"💬 Telegram:\n"
        f"ID: {telegram_id}\n"
        f"Username: {telegram_username or 'нет'}\n"
        f"Имя: {telegram_name}\n\n"
        f"🛒 Товары:\n"
    )

    for item in cart:
        quantity = item["quantity"]
        price = item["price"]
        line_total = quantity * price

        total += line_total

        text += (
            f"• {item['name']}\n"
            f"  {quantity} × "
            f"{format_price(price)} грн = "
            f"{format_price(line_total)} грн\n"
        )

    text += (
        f"\n💰 Итого: {format_price(total)} грн\n"
        f"📅 {order_date}"
    )

    for chat_id in ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
            )

            print(
                f"Уведомление отправлено: {chat_id}",
                flush=True
            )

        except Exception as error:
            print(
                f"ОШИБКА ОТПРАВКИ УВЕДОМЛЕНИЯ "
                f"{chat_id}: "
                f"{type(error).__name__}: {error}",
                flush=True
            )

def get_next_order_number():
    worksheet = get_orders_sheet()

    values = worksheet.col_values(1)

    if len(values) <= 1:
        return 1001

    numbers = []

    for value in values[1:]:
        try:
            numbers.append(int(str(value).strip()))
        except ValueError:
            continue

    if not numbers:
        return 1001

    return max(numbers) + 1

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

    # Если корзина пустая
    if not cart:
        keyboard = [
            [
                InlineKeyboardButton(
                    "← Назад к каталогу",
                    callback_data="back_categories"
                )
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "🛒 Корзина пуста.",
            reply_markup=reply_markup,
        )

        return

    text = "🛒 Ваша корзина\n\n"

    total = 0

    keyboard = []

    for index, item in enumerate(cart):

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

        # Кнопка удаления конкретного товара
        keyboard.append([
            InlineKeyboardButton(
                f"🗑 Удалить: {item['name']}",
                callback_data=f"remove_cart:{index}"
            )
        ])

    text += (
        f"Итого: **{format_price(total)} грн**"
    )

    keyboard.append([
        InlineKeyboardButton(
            "🛍 Оформить заказ",
            callback_data="checkout"
        )
    ])

    # Кнопка полной очистки корзины
    keyboard.append([
        InlineKeyboardButton(
            "🗑 Очистить корзину",
            callback_data="clear_cart"
        )
    ])

    # Возврат к каталогу
    keyboard.append([
        InlineKeyboardButton(
            "← Назад к каталогу",
            callback_data="back_categories"
        )
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def remove_cart_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer("Товар удалён 🗑")

    try:
        item_index = int(
            query.data.split(":")[1]
        )

    except (ValueError, IndexError):
        await query.edit_message_text(
            "Не удалось определить товар."
        )

        return

    cart = context.user_data.get(
        "cart",
        []
    )

    if item_index >= len(cart):
        await query.edit_message_text(
            "Товар уже отсутствует в корзине."
        )

        return

    removed_item = cart.pop(item_index)

    print(
        f"Удалён из корзины: "
        f"{removed_item['name']}",
        flush=True
    )

    # Если корзина стала пустой
    if not cart:
        keyboard = [
            [
                InlineKeyboardButton(
                    "← Назад к каталогу",
                    callback_data="back_categories"
                )
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "🛒 Корзина пуста.",
            reply_markup=reply_markup,
        )

        return

    # Показываем обновлённую корзину
    text = "🛒 Ваша корзина\n\n"

    total = 0

    keyboard = []

    for index, item in enumerate(cart):

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

        keyboard.append([
            InlineKeyboardButton(
                f"🗑 Удалить: {item['name']}",
                callback_data=f"remove_cart:{index}"
            )
        ])

    text += (
        f"Итого: **{format_price(total)} грн**"
    )

    keyboard.append([
        InlineKeyboardButton(
            "🛍 Оформить заказ",
            callback_data="checkout"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            "🗑 Очистить корзину",
            callback_data="clear_cart"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            "← Назад к каталогу",
            callback_data="back_categories"
        )
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def clear_cart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer("Корзина очищена 🗑")

    context.user_data["cart"] = []

    print(
        "=== CART CLEARED ===",
        flush=True
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "← Назад к каталогу",
                callback_data="back_categories"
            )
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🛒 Корзина пуста.",
        reply_markup=reply_markup,
    )

async def checkout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    cart = context.user_data.get(
        "cart",
        []
    )

    if not cart:
        await query.edit_message_text(
            "🛒 Корзина пуста."
        )

        return

    # Начинаем оформление заказа
    context.user_data["checkout_step"] = "name"
    context.user_data["customer"] = {}

    await query.edit_message_text(
        "📝 Оформление заказа\n\n"
        "Как вас зовут?"
    )

    print(
        "=== CHECKOUT STARTED ===",
        flush=True
    )

async def checkout_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    step = context.user_data.get(
        "checkout_step"
    )

    if not step:
        return

    text = update.message.text.strip()

    if not text:
        return

    customer = context.user_data.setdefault(
        "customer",
        {}
    )

    # -------------------------
    # ИМЯ
    # -------------------------

    if step == "name":

        customer["name"] = text

        context.user_data["checkout_step"] = "phone"

        await update.message.reply_text(
            "📞 Укажите номер телефона:"
        )

        return

    # -------------------------
    # ТЕЛЕФОН
    # -------------------------

    if step == "phone":

        customer["phone"] = text

        context.user_data["checkout_step"] = "city"

        await update.message.reply_text(
            "📍 Укажите город:"
        )

        return

    # -------------------------
    # ГОРОД
    # -------------------------

    if step == "city":

        customer["city"] = text

        context.user_data["checkout_step"] = None

        await show_order_confirmation(
            update,
            context
        )

        return

async def show_order_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    customer = context.user_data.get(
        "customer",
        {}
    )

    cart = context.user_data.get(
        "cart",
        []
    )

    if not cart:
        await update.message.reply_text(
            "🛒 Корзина пуста."
        )

        return

    text = (
        "📝 Проверьте заказ\n\n"
        f"👤 {customer.get('name', '')}\n"
        f"📞 {customer.get('phone', '')}\n"
        f"📍 {customer.get('city', '')}\n\n"
    )

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
        [
            InlineKeyboardButton(
                "✅ Подтвердить заказ",
                callback_data="confirm_order"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Изменить данные",
                callback_data="edit_customer"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Отменить",
                callback_data="cancel_checkout"
            )
        ],
    ]

    reply_markup = InlineKeyboardMarkup(
        keyboard
    )

    await update.message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def confirm_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    cart = context.user_data.get(
        "cart",
        []
    )

    customer = context.user_data.get(
        "customer",
        {}
    )

    if not cart:
        await query.edit_message_text(
            "🛒 Корзина пуста."
        )

        return

    try:
        # Получаем номер нового заказа
        order_number = get_next_order_number()

        # Получаем данные Telegram-профиля
        telegram_user = update.effective_user

        telegram_id = telegram_user.id

        telegram_username = (
            f"@{telegram_user.username}"
            if telegram_user.username
            else ""
        )

        telegram_name = (
            telegram_user.full_name
            or ""
        )

        # Дата и время заказа
        from datetime import datetime

        order_date = datetime.now().strftime(
            "%d.%m.%Y %H:%M:%S"
        )

        # Получаем лист заказов
        worksheet = get_orders_sheet()

        rows_to_add = []

        for item in cart:

            quantity = item["quantity"]
            price = item["price"]

            revenue = quantity * price

            rows_to_add.append([
                order_number,
                item["name"],
                quantity,
                price,
                revenue,
                customer.get("name", ""),
                customer.get("phone", ""),
                customer.get("city", ""),
                order_date,
                telegram_id,
                telegram_username,
                telegram_name,
            ])

        # Записываем заказ в Google Sheets
        worksheet.append_rows(
            rows_to_add,
            value_input_option="USER_ENTERED"
        )

        print(
            f"=== ORDER SAVED: {order_number} ===",
            flush=True
        )

        print(
            f"Telegram ID: {telegram_id}",
            flush=True
        )

        print(
            f"Telegram username: {telegram_username}",
            flush=True
        )

        print(
            f"Customer: {customer}",
            flush=True
        )

        print(
            f"Cart: {cart}",
            flush=True
        )

        # Отправляем уведомление администраторам.
        # Ошибка уведомления НЕ должна отменять заказ.
        try:
            await notify_admins(
                context=context,
                order_number=order_number,
                cart=cart,
                customer=customer,
                telegram_id=telegram_id,
                telegram_username=telegram_username,
                telegram_name=telegram_name,
                order_date=order_date,
            )

        except Exception as error:
            print(
                f"=== ADMIN NOTIFICATION ERROR: "
                f"{type(error).__name__}: {error}",
                flush=True
            )

        # Очищаем корзину после успешной записи
        context.user_data["cart"] = []
        context.user_data["checkout_step"] = None
        context.user_data["customer"] = {}

        # Кнопка возврата в каталог
        keyboard = [
            [
                InlineKeyboardButton(
                    "← Вернуться в каталог",
                    callback_data="back_categories"
                )
            ]
        ]

        reply_markup = InlineKeyboardMarkup(
            keyboard
        )

        # Сообщение клиенту
        await query.edit_message_text(
            f"✅ Заказ №{order_number} успешно оформлен!\n\n"
            "Спасибо за заказ 🌿\n"
            "Мы свяжемся с вами для подтверждения "
            "наличия товаров.",
            reply_markup=reply_markup,
        )

    except Exception as error:

        print(
            f"=== ORDER SAVE ERROR: "
            f"{type(error).__name__}: {error}",
            flush=True
        )

        await query.edit_message_text(
            "❌ Не удалось оформить заказ.\n\n"
            "Попробуйте ещё раз немного позже."
        )

async def cancel_checkout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    context.user_data["checkout_step"] = None
    context.user_data["customer"] = {}

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

    keyboard.append([
        InlineKeyboardButton(
            "🛒 Корзина",
            callback_data="show_cart"
        )
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "❌ Оформление заказа отменено.\n\n"
        "🌿 Каталог\n\n"
        "Выберите категорию:",
        reply_markup=reply_markup,
    )

    print(
        "=== CHECKOUT CANCELLED ===",
        flush=True
    )

async def edit_customer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    context.user_data["checkout_step"] = "name"
    context.user_data["customer"] = {}

    await query.edit_message_text(
        "📝 Введите имя заново:"
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

    app.add_handler(
        CallbackQueryHandler(
            remove_cart_item,
            pattern=r"^remove_cart:\d+$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            clear_cart,
            pattern=r"^clear_cart$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            checkout,
            pattern=r"^checkout$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            confirm_order,
            pattern=r"^confirm_order$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            edit_customer,
            pattern=r"^edit_customer$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            cancel_checkout,
            pattern=r"^cancel_checkout$"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            checkout_message
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