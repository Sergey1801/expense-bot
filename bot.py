import os
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from notion_client import Client as NotionClient
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

# Часовой пояс, по которому определяется "текущий месяц".
TZ = ZoneInfo("Europe/Moscow")

# Название title-колонки в базе Notion (переименуйте колонку "Name" в это имя).
PROP_TITLE = "Категория"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

RU_MONTHS = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

# ---------------------------------------------------------------------------
# КАТЕГОРИИ И КЛЮЧЕВЫЕ СЛОВА
# Фиксированный список категорий — это же порядок будет у строк в таблице.
# "Разное" — категория по умолчанию, если ничего не подошло (ключевые слова не нужны).
# ---------------------------------------------------------------------------
CATEGORIES = {
    "Света": ["света"],
    "Ипотека": ["ипотека", "ипотек"],
    "Еда": ["еда", "продукты", "кафе", "ресторан", "кофе", "обед", "ужин", "завтрак",
            "супермаркет", "пятерочка", "перекресток", "ашан", "лента", "магнит",
            "фастфуд", "пицца", "суши", "бар"],
    "Машина": ["бензин", "заправка", "машина", "автосервис", "автомойка", "шиномонтаж",
               "техобслуживание", "то ", "парковка", "каско", "осаго", "шины"],
    "Развлечения": ["кино", "театр", "концерт", "боулинг", "клуб", "развлечен", "игра",
                     "аттракцион", "квест"],
    "Магазины": ["одежда", "обувь", "магазин", "техника", "покупка", "wildberries",
                 "ozon", "вайлдберриз", "озон", "шопинг", "куртка", "джинсы", "кроссовки"],
    "Путешествия": ["путешестви", "отель", "авиабилет", "самолет", "поезд", "отпуск",
                     "виза", "booking", "airbnb", "круиз"],
    "Ариша": ["ариша"],
    "Разное": [],
}
DEFAULT_CATEGORY = "Разное"
CATEGORY_LIST = list(CATEGORIES.keys())

AMOUNT_PATTERN = re.compile(r"(\d+[.,]?\d*)")

# Кэш, чтобы не дёргать Notion лишний раз с проверкой "существует ли колонка месяца"
_known_month_columns = set()


def detect_category(text: str) -> str:
    lowered = text.lower()
    for category, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in lowered:
                return category
    return DEFAULT_CATEGORY


def parse_message(text: str):
    match = AMOUNT_PATTERN.search(text)
    if not match:
        return None, None
    amount_str = match.group(1).replace(",", ".")
    try:
        amount = float(amount_str)
    except ValueError:
        return None, None
    description = (text[: match.start()] + text[match.end():]).strip(" .,-")
    if not description:
        description = "без описания"
    return amount, description


def current_month_name() -> str:
    return RU_MONTHS[datetime.now(TZ).month]


# ---------------------------------------------------------------------------
# NOTION: строки = категории (фиксированный список), колонки = месяцы
# ---------------------------------------------------------------------------
def ensure_month_column(month_name: str):
    """Добавляет числовую колонку с именем месяца в базу, если её ещё нет."""
    if month_name in _known_month_columns:
        return
    db = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
    if month_name not in db["properties"]:
        notion.databases.update(
            database_id=NOTION_DATABASE_ID,
            properties={month_name: {"number": {"format": "number"}}},
        )
    _known_month_columns.add(month_name)


def find_category_page(category: str):
    response = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={"property": PROP_TITLE, "title": {"equals": category}},
        page_size=1,
    )
    results = response.get("results", [])
    return results[0] if results else None


def ensure_category_page(category: str):
    page = find_category_page(category)
    if page:
        return page
    return notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={PROP_TITLE: {"title": [{"text": {"content": category}}]}},
    )


def ensure_all_categories():
    """Один раз при старте создаёт строки для всех категорий, чтобы таблица
    сразу выглядела как задумано, даже до первой траты."""
    for category in CATEGORY_LIST:
        ensure_category_page(category)


def add_to_month_total(category: str, month_name: str, delta: float) -> float:
    ensure_month_column(month_name)
    page = ensure_category_page(category)
    props = page["properties"]
    current = 0.0
    if month_name in props and props[month_name].get("number") is not None:
        current = props[month_name]["number"]
    new_total = round(current + delta, 2)
    notion.pages.update(page_id=page["id"], properties={month_name: {"number": new_total}})
    return new_total


# ---------------------------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пиши мне траты в свободной форме, например:\n"
        "«500 еда» или «потратил 1200 на бензин»\n\n"
        "Я определю категорию и прибавлю сумму к текущему месяцу в таблице.\n\n"
        "Команда /month — покажет сумму по всем категориям за текущий месяц."
    )


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_name = current_month_name()

    try:
        response = notion.databases.query(database_id=NOTION_DATABASE_ID, page_size=100)
    except Exception as e:
        logger.exception("Ошибка чтения из Notion")
        await update.message.reply_text(f"Не получилось прочитать таблицу: {e}")
        return

    order = {category: i for i, category in enumerate(CATEGORY_LIST)}
    rows = []
    total = 0.0

    for page in response.get("results", []):
        title_list = page["properties"].get(PROP_TITLE, {}).get("title", [])
        category = title_list[0]["plain_text"] if title_list else "—"
        amount = 0.0
        month_prop = page["properties"].get(month_name)
        if month_prop and month_prop.get("number") is not None:
            amount = month_prop["number"]
        rows.append((category, amount))
        total += amount

    rows.sort(key=lambda row: order.get(row[0], 999))

    lines = [f"📊 Траты за {month_name}:\n"]
    for category, amount in rows:
        lines.append(f"{category}: {amount:.2f}")
    lines.append(f"\nИтого: {total:.2f}")

    await update.message.reply_text("\n".join(lines))


async def handle_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    amount, description = parse_message(text)

    if amount is None:
        await update.message.reply_text(
            "Не нашёл сумму в сообщении 🤔 Напиши, например: «500 еда»"
        )
        return

    category = detect_category(description)
    month_name = current_month_name()

    try:
        new_total = add_to_month_total(category, month_name, amount)
    except Exception as e:
        logger.exception("Ошибка записи в Notion")
        await update.message.reply_text(f"Не получилось записать в Notion: {e}")
        return

    context.user_data["last_entry"] = {
        "amount": amount,
        "category": category,
        "month_name": month_name,
    }

    keyboard = [
        [InlineKeyboardButton(cat, callback_data=f"fix|{i}")]
        for i, cat in enumerate(CATEGORY_LIST)
    ]

    await update.message.reply_text(
        f"Записал: {amount:.2f} — {description}\n"
        f"Категория: {category}\n"
        f"Итого «{category}» за {month_name}: {new_total:.2f}\n\n"
        f"Если категория неверная, выбери правильную:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_category_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    last_entry = context.user_data.get("last_entry")
    if not last_entry:
        await query.edit_message_text(
            "Не могу исправить — бот помнит только последнюю операцию, "
            "а прошло слишком много времени."
        )
        return

    _, idx = query.data.split("|")
    new_category = CATEGORY_LIST[int(idx)]
    old_category = last_entry["category"]
    amount = last_entry["amount"]
    month_name = last_entry["month_name"]

    if new_category == old_category:
        await query.edit_message_text(f"Категория уже «{new_category}» ✅")
        return

    try:
        add_to_month_total(old_category, month_name, -amount)
        add_to_month_total(new_category, month_name, amount)
    except Exception as e:
        logger.exception("Ошибка при исправлении категории")
        await query.edit_message_text(f"Не получилось исправить категорию: {e}")
        return

    last_entry["category"] = new_category
    context.user_data["last_entry"] = last_entry

    await query.edit_message_text(
        f"Перенёс {amount:.2f} из «{old_category}» в «{new_category}» ✅"
    )


def main():
    if not TELEGRAM_BOT_TOKEN or not NOTION_TOKEN or not NOTION_DATABASE_ID:
        raise RuntimeError(
            "Заполните TELEGRAM_BOT_TOKEN, NOTION_TOKEN и NOTION_DATABASE_ID в .env"
        )

    ensure_all_categories()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_expense))
    app.add_handler(CallbackQueryHandler(handle_category_fix, pattern=r"^fix\|"))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
