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

# Названия свойств (колонок) в базе Notion.
PROP_TITLE = "Описание"     # title — сюда пишем "Категория — Месяц Год"
PROP_AMOUNT = "Сумма"       # number — сумма трат по категории за месяц (накапливается)
PROP_CATEGORY = "Категория" # select
PROP_MONTH = "Месяц"        # select — ключ вида "2026-09", новая колонка, добавьте её в Notion
PROP_DATE = "Дата"          # date — дата последнего обновления строки (для наглядности)

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
# ---------------------------------------------------------------------------
CATEGORIES = {
    "Продукты": ["продукт", "магазин", "супермаркет", "еда", "пятерочка", "перекресток", "ашан", "лента", "овощ", "фрукт"],
    "Кафе и рестораны": ["кафе", "ресторан", "кофе", "старбакс", "бар", "пицца", "суши", "фастфуд", "макдональдс", "ланч", "обед"],
    "Транспорт": ["такси", "метро", "автобус", "бензин", "заправка", "парковка", "проезд", "билет на", "яндекс го", "убер"],
    "Одежда и обувь": ["одежда", "обувь", "куртка", "джинсы", "футболка", "кроссовки", "магазин одежды"],
    "Здоровье": ["аптека", "лекарств", "врач", "клиника", "стоматолог", "больница", "анализы", "витамины"],
    "Дом и коммуналка": ["коммуналка", "квартплата", "аренда", "электричество", "вода", "газ", "интернет дома", "ремонт"],
    "Связь и интернет": ["телефон", "связь", "мобильный", "сим", "билайн", "мтс", "мегафон", "теле2"],
    "Развлечения": ["кино", "театр", "концерт", "игра", "развлечен", "боулинг", "бильярд", "клуб"],
    "Подписки": ["подписка", "netflix", "spotify", "яндекс плюс", "youtube premium", "icloud"],
}
DEFAULT_CATEGORY = "Прочее"
CATEGORY_LIST = list(CATEGORIES.keys()) + [DEFAULT_CATEGORY]

AMOUNT_PATTERN = re.compile(r"(\d+[.,]?\d*)")


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


def current_month_key() -> str:
    now = datetime.now(TZ)
    return now.strftime("%Y-%m")


def month_title(month_key: str, category: str) -> str:
    year, month = month_key.split("-")
    return f"{category} — {RU_MONTHS[int(month)]} {year}"


# ---------------------------------------------------------------------------
# NOTION: одна строка = (месяц, категория), сумма в ней накапливается
# ---------------------------------------------------------------------------
def find_month_category_page(month_key: str, category: str):
    response = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={
            "and": [
                {"property": PROP_MONTH, "select": {"equals": month_key}},
                {"property": PROP_CATEGORY, "select": {"equals": category}},
            ]
        },
        page_size=1,
    )
    results = response.get("results", [])
    return results[0] if results else None


def add_to_category_total(month_key: str, category: str, delta: float) -> float:
    """Прибавляет (или вычитает, если delta отрицательная) сумму к строке
    месяц+категория. Создаёт строку, если её ещё нет. Возвращает новый итог."""
    page = find_month_category_page(month_key, category)
    today = datetime.now(TZ).date().isoformat()

    if page:
        current_total = page["properties"][PROP_AMOUNT]["number"] or 0
        new_total = round(current_total + delta, 2)
        notion.pages.update(
            page_id=page["id"],
            properties={
                PROP_AMOUNT: {"number": new_total},
                PROP_DATE: {"date": {"start": today}},
            },
        )
        return new_total
    else:
        new_total = round(delta, 2)
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                PROP_TITLE: {"title": [{"text": {"content": month_title(month_key, category)}}]},
                PROP_AMOUNT: {"number": new_total},
                PROP_CATEGORY: {"select": {"name": category}},
                PROP_MONTH: {"select": {"name": month_key}},
                PROP_DATE: {"date": {"start": today}},
            },
        )
        return new_total


# ---------------------------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пиши мне траты в свободной форме, например:\n"
        "«500 продукты» или «потратил 1200 на такси»\n\n"
        "Я определю категорию и прибавлю сумму к итогу этой категории за текущий месяц."
    )


async def handle_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    amount, description = parse_message(text)

    if amount is None:
        await update.message.reply_text(
            "Не нашёл сумму в сообщении 🤔 Напиши, например: «500 продукты»"
        )
        return

    category = detect_category(description)
    month_key = current_month_key()

    try:
        new_total = add_to_category_total(month_key, category, amount)
    except Exception as e:
        logger.exception("Ошибка записи в Notion")
        await update.message.reply_text(f"Не получилось записать в Notion: {e}")
        return

    # Запоминаем последнюю операцию — понадобится, если нажмут "исправить категорию"
    context.user_data["last_entry"] = {
        "amount": amount,
        "category": category,
        "month_key": month_key,
    }

    keyboard = [
        [InlineKeyboardButton(cat, callback_data=f"fix|{i}")]
        for i, cat in enumerate(CATEGORY_LIST)
    ]

    await update.message.reply_text(
        f"Записал: {amount:.2f} — {description}\n"
        f"Категория: {category}\n"
        f"Итого по «{category}» за {RU_MONTHS[int(month_key.split('-')[1])]}: {new_total:.2f}\n\n"
        f"Если категория неверная, выбери правильную:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_category_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    last_entry = context.user_data.get("last_entry")
    if not last_entry:
        await query.edit_message_text(
            "Не могу исправить — прошло слишком много времени с момента записи "
            "(бот помнит только последнюю операцию)."
        )
        return

    _, idx = query.data.split("|")
    new_category = CATEGORY_LIST[int(idx)]
    old_category = last_entry["category"]
    amount = last_entry["amount"]
    month_key = last_entry["month_key"]

    if new_category == old_category:
        await query.edit_message_text(f"Категория уже «{new_category}» ✅")
        return

    try:
        add_to_category_total(month_key, old_category, -amount)
        add_to_category_total(month_key, new_category, amount)
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

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_expense))
    app.add_handler(CallbackQueryHandler(handle_category_fix, pattern=r"^fix\|"))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
