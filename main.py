# main.py — Телеграм-бот с офлайн "AI"-разбором текста (эвристики), учётом доходов/расходов (SQLite)
# и webhook под Railway. Ничего дописывать не нужно.
# Требует: python-telegram-bot[webhooks]==21.4

import os
import re
import sqlite3
import time
import logging
from datetime import datetime
from typing import Optional, Tuple

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# === ЗАПОЛНЕНО: токен и адрес Railway (webhook) ===
BOT_TOKEN = "7611168200:AAHj7B6FelvvcoJMDBuKwKpveBHEo0NItnI"
WEBHOOK_URL = "https://beautiful-love.up.railway.app"  # адрес твоего деплоя Railway
PORT = int(os.environ.get("PORT", "8080"))

# === ЛОГИ ===
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("razzakovs-ai-bot")

DB_PATH = "finance.db"

# === ИНИЦИАЛИЗАЦИЯ БД ===
def init_db():
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tx (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ttype TEXT NOT NULL CHECK (ttype IN ('income','expense')),
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            category TEXT NOT NULL,
            note TEXT,
            ts INTEGER NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_ts ON tx(user_id, ts)")
    con.commit()
    con.close()

init_db()

# === КЛАВИАТУРА ===
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("➕ Доход"), KeyboardButton("➖ Расход")],
        [KeyboardButton("💰 Баланс"), KeyboardButton("📜 История")],
        [KeyboardButton("📊 Отчёт (месяц)"), KeyboardButton("🤖 AI-ответ")]
    ],
    resize_keyboard=True
)

# === ЭВРИСТИКИ ("AI" без внешних API) ===
CURRENCY_SIGNS = {
    "usd": ["$", "usd", "дол", "доллар"],
    "uzs": ["сум", "sum", "uzs", "сумы", "сумов"]
}
CATEGORY_KEYWORDS = {
    "Еда": ["еда", "продукт", "обед", "ужин", "завтрак", "кафе", "ресторан", "самса", "плов", "шаурма", "пицца"],
    "Транспорт": ["такси", "топливо", "бензин", "газ", "метро", "автобус", "аренда авто", "аренда машины"],
    "Зарплата": ["зарплата", "оклад", "премия", "бонус", "аванс"],
    "Здоровье": ["аптека", "врач", "стоматолог", "лекар", "витамин"],
    "Развлечения": ["кино", "игра", "cs2", "steam", "подписка", "spotify", "netflix"],
    "Дом": ["аренда", "квартира", "коммунал", "электр", "интернет", "ремонт"],
    "Детское": ["памперс", "подгуз", "коляска", "игруш", "детск", "дочка", "хадиджа"],
    "Спорт": ["зал", "спорт", "креатин", "протеин", "гейнер", "абонемент"],
    "Прочее": []
}

def detect_currency(text: str) -> str:
    t = text.lower()
    for cur, signs in CURRENCY_SIGNS.items():
        if any(s in t for s in signs):
            return cur
    return "uzs"

def parse_amount(text: str) -> Optional[float]:
    # находим 120000 / 120 000 / 120,000 / 12.5 / 12,5
    m = re.findall(r"(?:(?<=\s)|^)(\d{1,3}(?:[ \u00A0,\.]\d{3})+|\d+)(?:[.,](\d{1,2}))?", text)
    if not m:
        return None
    raw, frac = m[-1]
    num = re.sub(r"[ \u00A0,\.]", "", raw)  # убираем разделители тысяч
    if frac:
        return float(f"{num}.{frac}")
    return float(num)

def guess_type(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["зарплата", "премия", "бонус", "получил", "пришло", "доход"]):
        return "income"
    if any(w in t for w in ["расход", "купил", "оплатил", "заплатил", "потратил", "снял"]):
        return "expense"
    return "expense"

def guess_category(text: str) -> str:
    t = text.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in t for k in kws):
            return cat
    if "зарплат" in t or "прем" in t or "бонус" in t:
        return "Зарплата"
    return "Прочее"

def ai_classify_finance(text: str) -> Tuple[str, Optional[float], str, str]:
    ttype = guess_type(text)
    amount = parse_amount(text)
    currency = detect_currency(text)
    category = guess_category(text)
    return ttype, amount, currency, category

def ai_chat_reply(text: str) -> str:
    t = text.strip().lower()
    if any(w in t for w in ["как добавить", "как внести", "что писать", "помощ"]):
        return ("Пиши простым текстом: «самса 18 000 сум», «такси 25 000», «зарплата 800$».\n"
                "Кнопки: Баланс, История, Отчёт (месяц).")
    if "баланс" in t:
        return "Нажми «💰 Баланс» — покажу остатки по UZS и USD."
    if any(w in t for w in ["отчёт", "отчет", "месяц"]):
        return "Нажми «📊 Отчёт (месяц)» — дам суммарно доход/расход и баланс за текущий месяц."
    if any(w in t for w in ["копить", "эконом", "совет", "как сэкономить"]):
        return "Совет: фиксируй все траты 7 дней, потом урежь топ-3 категории на 20% — обычно это +10–15% к чистой прибыли."
    return "Принято ✅ Могу разобрать финансовую фразу или показать баланс/историю кнопками ниже."

# === РАБОТА С БД ===
def add_tx(user_id: int, ttype: str, amount: float, currency: str, category: str, note: str):
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.execute(
        "INSERT INTO tx (user_id, ttype, amount, currency, category, note, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, ttype, amount, currency, category, note, int(time.time()))
    )
    con.commit()
    con.close()

def get_balance(user_id: int) -> Tuple[float, float]:
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    def s(ttype, cur):
        c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype=? AND currency=?",
                  (user_id, ttype, cur))
        return c.fetchone()[0]
    bal_uzs = s("income", "uzs") - s("expense", "uzs")
    bal_usd = s("income", "usd") - s("expense", "usd")
    con.close()
    return bal_uzs, bal_usd

def month_report(user_id: int, y: int, m: int):
    start = int(datetime(y, m, 1).timestamp())
    end = int(datetime(y + (m == 12), (m % 12) + 1, 1).timestamp())
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    def sum_where(ttype, cur):
        c.execute("""SELECT COALESCE(SUM(amount),0)
                     FROM tx WHERE user_id=? AND ttype=? AND currency=? AND ts BETWEEN ? AND ?""",
                  (user_id, ttype, cur, start, end))
        return c.fetchone()[0]
    inc_uzs = sum_where("income", "uzs"); exp_uzs = sum_where("expense", "uzs")
    inc_usd = sum_where("income", "usd"); exp_usd = sum_where("expense", "usd")
    con.close()
    return inc_uzs, exp_uzs, inc_usd, exp_usd

def last_txs(user_id: int, limit: int = 10):
    con = sqlite3.connect(DB_PATH)
    c = con.cursor()
    c.execute("""SELECT ttype, amount, currency, category, note, ts
                 FROM tx WHERE user_id=? ORDER BY id DESC LIMIT ?""", (user_id, limit))
    rows = c.fetchall()
    con.close()
    return rows

# === ХЭНДЛЕРЫ ===
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Razzakov’s Finance 🤖\n"
        "Пиши: «самса 18 000 сум», «такси 25 000», «зарплата 800$» — разберу и сохраню.\n"
        "Кнопки снизу — быстрые функции.",
        reply_markup=MAIN_KB
    )

async def ai_button(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши вопрос — отвечу советом/подсказкой.", reply_markup=MAIN_KB)

async def balance_handler(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bal_uzs, bal_usd = get_balance(uid)
    msg = (
        f"Баланс:\n"
        f"• UZS: {int(bal_uzs):,}".replace(",", " ") + "\n"
        f"• USD: {bal_usd:.2f}"
    )
    await update.message.reply_text(msg, reply_markup=MAIN_KB)

async def history_handler(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = last_txs(uid, 10)
    if not rows:
        await update.message.reply_text("История пуста.", reply_markup=MAIN_KB); return
    lines = []
    for ttype, amount, cur, cat, note, ts in rows:
        dt = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
        sign = "➕" if ttype == "income" else "➖"
        lines.append(f"{dt} {sign} {amount:.2f} {cur.upper()} • {cat} • {note or '-'}")
    await update.message.reply_text("Последние операции:\n" + "\n".join(lines), reply_markup=MAIN_KB)

async def report_handler(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = datetime.now()
    inc_uzs, exp_uzs, inc_usd, exp_usd = month_report(uid, now.year, now.month)
    bal_uzs = inc_uzs - exp_узs
    bal_usd = inc_usd - exp_usd
    msg = (
        f"Отчёт за {now.strftime('%B %Y')}:\n"
        f"• Доход UZS: {int(inc_uzs):,}".replace(",", " ") + "\n"
        f"• Расход UZS: {int(exp_uzs):,}".replace(",", " ") + "\n"
        f"• Баланс UZS: {int(bal_uzs):,}".replace(",", " ") + "\n"
        f"• Баланс USD: {bal_usd:.2f}"
    )
    await update.message.reply_text(msg, reply_markup=MAIN_KB)

async def text_router(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    low = text.lower()
    if "баланс" in low:
        await balance_handler(update, _); return
    if "история" in low:
        await history_handler(update, _); return
    if "отчёт" in low or "отчет" in low:
        await report_handler(update, _); return
    if "ai" in low or "🤖" in low:
        await ai_button(update, _); return

    # Пробуем распарсить как финансовую транзакцию
    ttype, amount, currency, category = ai_classify_finance(text)
    if amount is not None:
        add_tx(uid, ttype, amount, currency, category, text)
        sign = "Доход" if ttype == "income" else "Расход"
        await update.message.reply_text(
            f"{sign}: {amount:.2f} {currency.upper()} • {category}\n✓ Сохранено",
            reply_markup=MAIN_KB
        )
        return

    # Иначе — короткий AI-совет
    reply = ai_chat_reply(text)
    await update.message.reply_text(reply, reply_markup=MAIN_KB)

async def unknown_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажми кнопку ниже или напиши траты/доход.", reply_markup=MAIN_KB)

# === ЗАПУСК ЧЕРЕЗ WEBHOOK ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    log.info("Starting webhook on port %s ...", PORT)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()


