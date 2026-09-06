import os
import re
import logging
from datetime import datetime, timezone

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

# Названия свойств (колонок) в базе Notion — должны совпадать с тем,
# что вы создали в самой таблице Notion.
PROP_TITLE = "Описание"     # title-свойство (в Notion у каждой базы обязательно есть одно)
PROP_AMOUNT = "Сумма"       # number
PROP_CATEGORY = "Категория" # select
PROP_DATE = "Дата"          # date

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

# ---------------------------------------------------------------------------
# КАТЕГОРИИ И КЛЮЧЕВЫЕ СЛОВА
# Отредактируйте под себя: ключ — название категории (то, что попадёт в таблицу),
# значение — список слов, по которым бот будет её угадывать.
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
    """Достаёт сумму и описание из свободного текста вида
    '500 продукты' или 'потратил 1500 на такси до аэропорта'."""
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


# ---------------------------------------------------------------------------
# NOTION
# ---------------------------------------------------------------------------
def append_expense(amount: float, description: str, category: str) -> str:
    """Создаёт страницу (строку) в базе Notion и возвращает её page_id."""
    page = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            PROP_TITLE: {"title": [{"text": {"content": description}}]},
            PROP_AMOUNT: {"number": amount},
            PROP_CATEGORY: {"select": {"name": category}},
            PROP_DATE: {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        },
    )
    return page["id"]


def update_category(page_id: str, category: str):
    notion.pages.update(
        page_id=page_id,
        properties={PROP_CATEGORY: {"select": {"name": category}}},
    )


# ---------------------------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пиши мне траты в свободной форме, например:\n"
        "«500 продукты» или «потратил 1200 на такси»\n\n"
        "Я сам определю категорию и запишу в таблицу Notion."
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

    try:
        page_id = append_expense(amount, description, category)
    except Exception as e:
        logger.exception("Ошибка записи в Notion")
        await update.message.reply_text(f"Не получилось записать в Notion: {e}")
        return

    # page_id без дефисов короче — экономим место в callback_data (лимит 64 байта)
    short_id = page_id.replace("-", "")
    keyboard = [
        [InlineKeyboardButton(cat, callback_data=f"fix|{short_id}|{i}")]
        for i, cat in enumerate(CATEGORY_LIST)
    ]

    await update.message.reply_text(
        f"Записал: {amount:.2f} — {description}\nКатегория: {category}\n\n"
        f"Если категория неверная, выбери правильную:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_category_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, short_id, idx = query.data.split("|")
    category = CATEGORY_LIST[int(idx)]
    try:
        update_category(short_id, category)
        await query.edit_message_text(f"Категория обновлена на: {category} ✅")
    except Exception as e:
        logger.exception("Ошибка обновления категории")
        await query.edit_message_text(f"Не получилось обновить категорию: {e}")


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
