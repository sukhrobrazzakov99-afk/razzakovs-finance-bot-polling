# main.py — Телеграм-бот финансов (webhook + "AI"-классификация + SQLite)
# Требуется: python-telegram-bot==21.4
# Установка зависимостей (requirements.txt):
# python-telegram-bot==21.4
# pydantic==2.8.2

import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Optional, Tuple

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# === Конфигурация через переменные окружения (без хардкода секретов) ===
# Обязательно задайте BOT_TOKEN и WEBHOOK_URL в окружении деплоя
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", "8080"))

# Секрет для валидации вебхука Telegram (не включаем токен бота в URL)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", TOKEN)

DB_PATH = "finance.db"

# ==== ИНИЦИАЛИЗАЦИЯ БД ====
def _connect_db():
    # Увеличенный таймаут + WAL для снижения блокировок под нагрузкой
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def init_db():
    con = _connect_db()
    c = con.cursor()
    c.execute(
        """
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
        """
    )
    c.execute(
        """CREATE INDEX IF NOT EXISTS idx_user_ts ON tx(user_id, ts)"""
    )
    con.commit()
    con.close()

init_db()

# ==== КЛАВИАТУРА ====
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("➕ Доход"), KeyboardButton("➖ Расход")],
        [KeyboardButton("💰 Баланс"), KeyboardButton("📜 История")],
        [KeyboardButton("📊 Отчёт (месяц)"), KeyboardButton("ℹ️ Помощь")]
    ],
    resize_keyboard=True
)

# ==== УТИЛИТЫ ДЛЯ "AI"-КЛАССИФИКАЦИИ (без внешних API) ====
CURRENCY_SIGNS = {
    "usd": ["$", "usd", "дол", "доллар"],
    "uzs": ["сум", "sum", "uzs", "сумы", "сумов"]
}

CATEGORY_KEYWORDS = {
    "Еда": ["еда", "продукт", "продукты", "обед", "ужин", "завтрак", "кафе", "ресторан", "самса", "плов", "шаурма", "пицца"],
    "Транспорт": ["такси", "топливо", "бензин", "газ", "метро", "автобус", "аренда авто", "аренда машины"],
    "Зарплата": ["зарплата", "оклад", "премия", "бонус", "аванс"],
    "Здоровье": ["аптека", "врач", "стоматолог", "мед", "лекар", "витамин"],
    "Развлечения": ["кино", "игра", "cs2", "steam", "подписка", "spotify", "netflix"],
    "Дом": ["аренда", "квартира", "коммунал", "электр", "интернет", "ремонт"],
    "Детское": ["памперс", "подгуз", "коляска", "игруш", "сок для ребёнка", "детск", "дочка", "хадиджа"],
    "Спорт": ["зал", "спорт", "креатин", "протеин", "гейнер", "абонемент"],
    "Прочее": []
}

def detect_currency(text: str) -> str:
    t = text.lower()
    for cur, signs in CURRENCY_SIGNS.items():
        if any(s in t for s in signs):
            return cur
    # по умолчанию UZS
    return "uzs"

def parse_amount(text: str) -> Optional[float]:
    # Ищем числа вида 120000, 120 000, 120,000, 12.5, 12,5, $120
    candidates = re.findall(r"(?:(?<=\s)|^)(\d{1,3}(?:[ ,.\u00A0]\d{3})+|\d+)(?:[.,](\d{1,2}))?", text)
    if not candidates:
        return None
    raw, frac = candidates[-1]
    num = re.sub(r"[ ,\u00A0]", "", raw)
    if frac:
        return float(f"{num}.{frac}")
    return float(num)

def guess_type(text: str) -> str:
    t = text.lower()
    # явные маркеры дохода
    if any(w in t for w in ["доход", "получил", "зарплата", "премия", "бонус", "зачислили", "перевод пришел", "пришло"]):
        return "income"
    # явные маркеры расхода
    if any(w in t for w in ["расход", "купил", "оплатил", "заплатил", "снял", "потратил", "оплата"]):
        return "expense"
    # по умолчанию считаем расход
    return "expense"

def guess_category(text: str) -> str:
    t = text.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in t for k in kws):
            return cat
    # эвристика по ключам валют/контексту
    if "зарплат" in t or "прем" in t or "бонус" in t:
        return "Зарплата"
    return "Прочее"

def ai_classify(text: str) -> Tuple[str, Optional[float], str, str]:
    """
    Возвращает: (ttype, amount, currency, category)
    ttype: income|expense
    currency: usd|uzs
    """
    ttype = guess_type(text)
    amount = parse_amount(text)
    currency = detect_currency(text)
    category = guess_category(text)
    return ttype, amount, currency, category

# ==== РАБОТА С БД ====
def add_tx(user_id: int, ttype: str, amount: float, currency: str, category: str, note: str):
    con = _connect_db()
    c = con.cursor()
    c.execute(
        "INSERT INTO tx (user_id, ttype, amount, currency, category, note, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, ttype, amount, currency, category, note, int(time.time()))
    )
    con.commit()
    con.close()

def get_balance(user_id: int) -> Tuple[float, float]:
    con = _connect_db()
    c = con.cursor()
    c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype='income' AND currency='uzs'", (user_id,))
    inc_uzs = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype='expense' AND currency='uzs'", (user_id,))
    exp_uzs = c.fetchone()[0]
    bal_uzs = inc_uzs - exp_uzs

    c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype='income' AND currency='usd'", (user_id,))
    inc_usd = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype='expense' AND currency='usd'", (user_id,))
    exp_usd = c.fetchone()[0]
    bal_usd = inc_usd - exp_usd

    con.close()
    return bal_uzs, bal_usd

def month_report(user_id: int, y: int, m: int) -> Tuple[float, float, float, float]:
    # Суммы за месяц по валютам
    start = int(datetime(y, m, 1).timestamp())
    if m == 12:
        end = int(datetime(y + 1, 1, 1).timestamp())
    else:
        end = int(datetime(y, m + 1, 1).timestamp())

    con = _connect_db()
    c = con.cursor()
    def sum_where(ttype, cur):
        c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM tx WHERE user_id=? AND ttype=? AND currency=? AND ts>=? AND ts<?",
            (user_id, ttype, cur, start, end)
        )
        return c.fetchone()[0]

    inc_uzs = sum_where("income", "uzs")
    exp_uzs = sum_where("expense", "uzs")
    inc_usd = sum_where("income", "usd")
    exp_usd = sum_where("expense", "usd")
    con.close()
    return inc_uzs - exp_uzs, inc_usd - exp_usd, inc_uzs, exp_uzs

def last_txs(user_id: int, limit: int = 10):
    con = _connect_db()
    c = con.cursor()
    c.execute(
        "SELECT ttype, amount, currency, category, note, ts FROM tx WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = c.fetchall()
    con.close()
    return rows

# ==== ХЭНДЛЕРЫ ====
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Razzakov’s Finance ✅\n"
        "Пиши простым текстом: например, «самса 18 000 сум», «такси 25 000», «зарплата 800$».\n"
        "Или используй кнопки ниже.",
        reply_markup=MAIN_KB
    )

async def help_handler(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Примеры:\n"
        "• самса 18 000 сум → расход, Еда\n"
        "• такси 25 000 → расход, Транспорт\n"
        "• зарплата 800$ → доход, Зарплата\n"
        "Команды: «Баланс», «История», «Отчёт (месяц)».",
        reply_markup=MAIN_KB
    )

async def handle_income_btn(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши доход, например: «зарплата 6 000 000 сум»", reply_markup=MAIN_KB)

async def handle_expense_btn(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши расход, например: «такси 25 000» или «еда 120 000 сум»", reply_markup=MAIN_KB)

async def balance_handler(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bal_uzs, bal_usd = get_balance(uid)
    await update.message.reply_text(
        f"Баланс:\n"
        f"• UZS: {int(bal_uzs):,}".replace(",", " ") + "\n"
        f"• USD: {bal_usd:.2f}",
        reply_markup=MAIN_KB
    )

async def history_handler(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = last_txs(uid, 10)
    if not rows:
        await update.message.reply_text("История пуста.", reply_markup=MAIN_KB)
        return
    lines = []
    for ttype, amount, cur, cat, note, ts in rows:
        dt = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
        sign = "➕" if ttype == "income" else "➖"
        lines.append(f"{dt} {sign} {amount:.2f} {cur.upper()} • {cat} • {note or '-'}")
    await update.message.reply_text("Последние операции:\n" + "\n".join(lines), reply_markup=MAIN_KB)

async def monthly_report_handler(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = datetime.now()
    bal_m_uzs, bal_m_usd, inc_uzs, exp_uzs = month_report(uid, now.year, now.month)
    await update.message.reply_text(
        f"Отчёт за {now.strftime('%B %Y')}:\n"
        f"• Доход UZS: {int(inc_uzs):,}".replace(",", " ") + "\n"
        f"• Расход UZS: {int(exp_uzs):,}".replace(",", " ") + "\n"
        f"• Баланс UZS (месяц): {int(bal_m_uzs):,}".replace(",", " ") + "\n"
        f"• Баланс USD (месяц): {bal_m_usd:.2f}",
        reply_markup=MAIN_KB
    )

async def text_router(update: Update, _: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # Кнопки
    low = text.lower()
    if "баланс" in low:
        await balance_handler(update, _); return
    if "история" in low:
        await history_handler(update, _); return
    if "отчёт" in low or "отчет" in low:
        await monthly_report_handler(update, _); return
    if "помощ" in low or "help" in low:
        await help_handler(update, _); return
    if "доход" in low:
        await handle_income_btn(update, _); return
    if "расход" in low:
        await handle_expense_btn(update, _); return

    # "AI" классификация
    ttype, amount, currency, category = ai_classify(text)
    if amount is None:
        await update.message.reply_text("Не вижу сумму. Пример: «еда 45 000 сум».", reply_markup=MAIN_KB)
        return

    add_tx(uid, ttype, amount, currency, category, text)
    sign = "Добавлен доход" if ttype == "income" else "Добавлен расход"
    await update.message.reply_text(
        f"{sign}: {amount:.2f} {currency.upper()} • {category}\n✓ Сохранено",
        reply_markup=MAIN_KB
    )

async def unknown_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Нажми кнопку или напиши сумму/описание.", reply_markup=MAIN_KB)

# ==== ЗАПУСК ЧЕРЕЗ WEBHOOK ====
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    # PTB сам вызовет setWebhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{WEBHOOK_URL}/webhook" if WEBHOOK_URL else None,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
