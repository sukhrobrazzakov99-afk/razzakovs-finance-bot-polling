# main.py
# Finance Razzakov's — минимально самодостаточный бот учёта
# Зависимости: python-telegram-bot[webhooks,job-queue]==20.8, aiosqlite (не обязательно), sqlite3 в стандартной библиотеке

import os
import re
import csv
import io
import sqlite3
from datetime import datetime, date

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputFile,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackContext,
    ContextTypes,
    filters,
)

# ==========================
# НАСТРОЙКИ
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN") or "7611168200:AAHj7B6FelvvcoJMDBuKwKpveBHEo0NItnI"

WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # например: https://secure-consideration-production.up.railway.app
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fr_webhook_secret")
PORT = int(os.getenv("PORT", "8080"))
HOST = "0.0.0.0"

DB_PATH = os.getenv("DB_PATH", "finance.db")

# ==========================
# БАЗА
# ==========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ttype TEXT NOT NULL CHECK(ttype IN ('income','expense')),
            amount REAL NOT NULL,
            currency TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            due_date TEXT,               -- YYYY-MM-DD
            role TEXT NOT NULL CHECK(role IN ('debtor','creditor')), -- debtor: нам должны, creditor: мы должны
            status TEXT NOT NULL DEFAULT 'open', -- open / closed
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


# ==========================
# КЛАВИАТУРЫ
# ==========================
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["➕ Доход", "➖ Расход"],
        ["💰 Баланс", "📜 История"],
        ["📄 Excel", "🤖 AI"],
        ["💳 Должники/Кредиторы"],
    ],
    resize_keyboard=True,
)

DEBT_KB = ReplyKeyboardMarkup(
    [
        ["➕ Добавить долг", "📋 Список долгов"],
        ["✅ Закрыть долг", "⬅️ Назад"],
    ],
    resize_keyboard=True,
)

# ==========================
# УТИЛИТЫ
# ==========================
def parse_amount_and_note(text: str):
    """
    Пытаемся выделить сумму и валюту из текста.
    Пример: "еда 150000 uzs" -> amount=150000, currency="uzs", note="еда"
    """
    tokens = text.strip().split()
    amount = None
    currency = None

    # ищем число
    for tok in tokens:
        # 150000, 150000.50
        if re.fullmatch(r"\d+(?:[.,]\d+)?", tok):
            try:
                amount = float(tok.replace(",", "."))
                break
            except Exception:
                pass

    # ищем валюту (uzs, usd, rub, сум, $ и т.п.)
    # примитивно: последний или следующий токен после суммы
    # если нет — оставляем None
    if amount is not None:
        idx = tokens.index(tok)
        if idx + 1 < len(tokens):
            cur = tokens[idx + 1].lower()
            cur = cur.replace("сум", "uzs").replace("$", "usd").replace("₽", "rub")
            currency = cur

    note_parts = []
    for t in tokens:
        if t != tok and (currency is None or t.lower() != currency):
            note_parts.append(t)
    note = " ".join(note_parts).strip()

    return amount, currency, note


def add_transaction(user_id: int, ttype: str, amount: float, currency: str, note: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions (user_id, ttype, amount, currency, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, ttype, amount, currency, note, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_balance(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND ttype='income'",
        (user_id,),
    )
    inc = cur.fetchone()[0] or 0
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND ttype='expense'",
        (user_id,),
    )
    exp = cur.fetchone()[0] or 0
    conn.close()
    return inc, exp, inc - exp


def get_history(user_id: int, limit=10):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT ttype, amount, currency, note, created_at FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def export_csv(user_id: int) -> bytes:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, ttype, amount, currency, note, created_at FROM transactions WHERE user_id=? ORDER BY id",
        (user_id,),
    )
    rows = cur.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "type", "amount", "currency", "note", "created_at"])
    for r in rows:
        writer.writerow([r["id"], r["ttype"], r["amount"], r["currency"], r["note"], r["created_at"]])
    conn.close()
    return output.getvalue().encode("utf-8")


def add_debt(user_id: int, name: str, amount: float, due: str | None, role: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO debts (user_id, name, amount, due_date, role, status, created_at) VALUES (?,?,?,?,?,'open',?)",
        (user_id, name, amount, due, role, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def list_open_debts(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, amount, due_date, role, status FROM debts WHERE user_id=? AND status='open' ORDER BY id DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def close_debt(user_id: int, debt_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE debts SET status='closed' WHERE user_id=? AND id=?",
        (user_id, debt_id),
    )
    conn.commit()
    conn.close()


# ==========================
# ХЕНДЛЕРЫ
# ==========================
ASK_AMOUNT = range(1)
ASK_DEBT = range(1)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выбирай действие:\n\n"
        "Напиши вопрос/фразу — отвечу как помощник.\n\n"
        "Готово. Записи фиксируйте через кнопки или вводите одной строкой: 'еда 150000 uzs'.",
        reply_markup=MAIN_KB,
    )


async def ai_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Простой болталка-заглушка (без внешнего ИИ)
    text = update.message.text.strip()
    await update.message.reply_text(f"🤖 Понял: «{text}». Пока отвечаю кратко — модуль AI можно подключить позже.")


# ---- Доход/Расход (диалог 1 шаг — ввести строку) ----
async def income_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ttype"] = "income"
    await update.message.reply_text("Сумма и валюта (пример: 150000 uzs или 20 usd)", reply_markup=ReplyKeyboardRemove())
    return ASK_AMOUNT


async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ttype"] = "expense"
    await update.message.reply_text("Сумма и валюта (пример: 150000 uzs или 20 usd)", reply_markup=ReplyKeyboardRemove())
    return ASK_AMOUNT


async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ttype = context.user_data.get("ttype")
    text = update.message.text
    amount, currency, note = parse_amount_and_note(text)

    if not amount:
        await update.message.reply_text("Не нашёл сумму. Введите ещё раз, например: 150000 uzs\nИли нажмите /cancel.")
        return ASK_AMOUNT

    add_transaction(update.message.from_user.id, ttype, amount, currency or "", note)
    await update.message.reply_text("Записал ✅", reply_markup=MAIN_KB)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
    return ConversationHandler.END


# ---- Баланс ----
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inc, exp, bal = get_balance(update.message.from_user.id)
    await update.message.reply_text(
        f"Доход: {inc:.2f}\nРасход: {exp:.2f}\n------\nБаланс: {bal:.2f}",
        reply_markup=MAIN_KB,
    )


# ---- История ----
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_history(update.message.from_user.id, 10)
    if not rows:
        await update.message.reply_text("История пуста.", reply_markup=MAIN_KB)
        return
    lines = []
    for r in rows:
        t = "Доход" if r["ttype"] == "income" else "Расход"
        note = (r["note"] or "").strip()
        cur = (r["currency"] or "").upper()
        when = r["created_at"].split("T")[0]
        lines.append(f"{when} · {t}: {r['amount']:.2f} {cur} · {note}")
    await update.message.reply_text("Последние операции:\n" + "\n".join(lines), reply_markup=MAIN_KB)


# ---- Excel (CSV) ----
async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = export_csv(update.message.from_user.id)
    bio = io.BytesIO(data)
    bio.name = "transactions.csv"
    await update.message.reply_document(document=InputFile(bio), caption="Экспорт операций (CSV).", reply_markup=MAIN_KB)


# ---- ДОЛЖНИКИ/КРЕДИТОРЫ ----
async def debt_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Должники/Кредиторы:\n"
        "➕ Добавить долг — формат: Имя Сумма [YYYY-MM-DD], далее выбрать роль.\n"
        "📋 Список долгов — покажу открытые.\n"
        "✅ Закрыть долг — укажи ID из списка.",
        reply_markup=DEBT_KB,
    )


async def debt_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введи одной строкой: Имя Сумма [YYYY-MM-DD]\nНапример: Иван 150000 2025-08-25",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_DEBT


async def debt_add_parse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("Нужно минимум Имя и Сумма. Попробуй ещё раз или /cancel.")
        return ASK_DEBT

    name = parts[0]
    try:
        amount = float(parts[1].replace(",", "."))
    except Exception:
        await update.message.reply_text("Сумма некорректна. Попробуй ещё раз или /cancel.")
        return ASK_DEBT

    due = None
    if len(parts) >= 3:
        # пробуем YYYY-MM-DD
        try:
            _ = datetime.strptime(parts[2], "%Y-%m-%d")
            due = parts[2]
        except Exception:
            due = None

    # спрашиваем роль
    context.user_data["new_debt"] = {"name": name, "amount": amount, "due": due}
    kb = ReplyKeyboardMarkup([["Нам должны (дебитор)", "Мы должны (кредитор)"]], resize_keyboard=True)
    await update.message.reply_text("Кто кому должен?", reply_markup=kb)
    return ASK_DEBT


async def debt_add_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip().lower()
    data = context.user_data.get("new_debt")
    if not data:
        await update.message.reply_text("Нет данных. /cancel", reply_markup=MAIN_KB)
        return ConversationHandler.END

    if "дебитор" in choice or "нам должны" in choice:
        role = "debtor"
    elif "кредитор" in choice or "мы должны" in choice:
        role = "creditor"
    else:
        await update.message.reply_text("Не понял. Выбери один из вариантов или /cancel.")
        return ASK_DEBT

    add_debt(update.message.from_user.id, data["name"], data["amount"], data["due"], role)
    await update.message.reply_text("Долг добавлен ✅", reply_markup=DEBT_KB)
    context.user_data.pop("new_debt", None)
    return ConversationHandler.END


async def debt_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_open_debts(update.message.from_user.id)
    if not rows:
        await update.message.reply_text("Открытых долгов нет.", reply_markup=DEBT_KB)
        return
    lines = []
    for r in rows:
        role = "нам должны" if r["role"] == "debtor" else "мы должны"
        due = r["due_date"] or "—"
        lines.append(f"ID {r['id']}: {r['name']} · {r['amount']:.2f} · {role} · срок: {due}")
    await update.message.reply_text("Открытые долги:\n" + "\n".join(lines), reply_markup=DEBT_KB)


async def debt_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Укажи ID долга для закрытия (из списка).", reply_markup=ReplyKeyboardRemove())
    context.user_data["close_wait"] = True


async def debt_close_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("close_wait"):
        return
    text = update.message.text.strip()
    if not re.fullmatch(r"\d+", text):
        await update.message.reply_text("Это не число. Попробуй ещё раз или /cancel.")
        return
    close_debt(update.message.from_user.id, int(text))
    context.user_data.pop("close_wait", None)
    await update.message.reply_text("Готово ✅", reply_markup=DEBT_KB)


# ---- Напоминания по долгам ----
async def notify_overdues(context: CallbackContext):
    """
    Раз в 12 часов: напоминаем о просроченных долгах (open + due_date < сегодня).
    """
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT user_id, id, name, amount, due_date, role
        FROM debts
        WHERE status='open' AND due_date IS NOT NULL
        """
    )
    rows = cur.fetchall()
    conn.close()

    today = date.today()
    for r in rows:
        try:
            due = datetime.strptime(r["due_date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if due < today:
            role = "нам должны" if r["role"] == "debtor" else "мы должны"
            msg = f"⏰ Просрочен долг (ID {r['id']}): {r['name']} · {r['amount']:.2f} · {role} · срок был {r['due_date']}"
            await context.bot.send_message(chat_id=r["user_id"], text=msg)


# ==========================
# POST INIT: снимаем старый вебхук и запускаем JobQueue
# ==========================
from telegram.ext import Application  # чтобы type-hint не ругался

async def _post_init(app: Application):
    # На всякий случай снимаем старый вебхук — PTB при run_webhook сам поставит новый.
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print("delete_webhook warning:", e)

    # Планировщик — в PTB 20.x job_queue доступен из app (extras [job-queue] обязательны)
    try:
        app.job_queue.run_repeating(notify_overdues, interval=60 * 60 * 12, first=60)
    except Exception as e:
        print("JobQueue disabled:", e)


# ==========================
# СБОРКА ПРИЛОЖЕНИЯ
# ==========================
def build_app() -> Application:
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = _post_init

    # /start
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))

    # Доход/Расход
    conv_amount = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^(➕ Доход)$"), income_start),
            MessageHandler(filters.Regex(r"^(➖ Расход)$"), expense_start),
        ],
        states={
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        per_message=False,
    )
    app.add_handler(conv_amount)

    # Баланс / История / Экспорт / AI
    app.add_handler(MessageHandler(filters.Regex(r"^(💰 Баланс)$"), balance))
    app.add_handler(MessageHandler(filters.Regex(r"^(📜 История)$"), history))
    app.add_handler(MessageHandler(filters.Regex(r"^(📄 Excel)$"), export_excel))
    # AI — любой произвольный текст, который не совпал с кнопками
    # но чтобы кнопки работали приоритетно — AI снизу:
    app.add_handler(MessageHandler(filters.Regex(r"^(🤖 AI)$"), lambda u, c: u.message.reply_text("Напиши вопрос/фразу — отвечу.")))

    # Долги
    app.add_handler(MessageHandler(filters.Regex(r"^(💳 Должники/Кредиторы)$"), debt_menu))
    conv_debt = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^(➕ Добавить долг)$"), debt_add_start),
        ],
        states={
            ASK_DEBT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, debt_add_parse),
                MessageHandler(filters.Regex(r"^(Нам должны \(дебитор\)|Мы должны \(кредитор\))$"), debt_add_role),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        per_message=False,
    )
    app.add_handler(conv_debt)
    app.add_handler(MessageHandler(filters.Regex(r"^(📋 Список долгов)$"), debt_list))
    app.add_handler(MessageHandler(filters.Regex(r"^(✅ Закрыть долг)$"), debt_close))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, debt_close_id), group=1)

    # Любой другой текст — отправим в "AI" заглушку
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_answer), group=2)

    return app


# ==========================
# ЗАПУСК (webhook или polling)
# ==========================
def main():
    app = build_app()

    if WEBHOOK_URL:
        # webhook режим
        app.run_webhook(
            listen=HOST,
            port=PORT,
            secret_token=WEBHOOK_SECRET,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{WEBHOOK_SECRET}",
            drop_pending_updates=True,
        )
    else:
        # polling режим
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
