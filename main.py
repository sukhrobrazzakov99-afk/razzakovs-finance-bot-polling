# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, CallbackQueryHandler, filters
)
from ai_helper import parse_free_text, parse_due
from db import DB

# === КОНФИГ ===
TOKEN = "7611168200:AAFkdTWAz1xMawJOKF0Mu21ViFA5Oz8wblk"
OWNER_USERNAMES = ["SukhrobAbdurazzakov", "revivemd"]  # у кого доступ по username

# === БД ===
db = DB("data.sqlite")

# === СОСТОЯНИЯ ДИАЛОГОВ ===
ADD_AMOUNT, ADD_DESC, DEBT_CP, DEBT_AMOUNT, DEBT_DUE, DEBT_NOTE, DEBT_CLOSE_ID = range(7)

def menu_kb():
    rows = [
        ["➕ Доход", "➖ Расход"],
        ["💰 Баланс", "📒 История"],
        ["🤝 Дебиторы", "💳 Кредиторы"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def is_allowed(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    if u.username and u.username in OWNER_USERNAMES:
        return True
    return db.is_allowed(u.id)

# ---------- БАЗОВЫЕ КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        # авто-онбординг: первых двоих добавляем в allowlist
        if db.allow_count() < 2:
            db.allow(update.effective_user.id)
        else:
            await update.message.reply_text("⛔️ Доступ ограничен.")
            return
    await update.message.reply_text("Выбирай действие:", reply_markup=menu_kb())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    inc, exp, net = db.get_balance(update.effective_user.id)
    rec, pay = db.get_debt_totals(update.effective_user.id)
    txt = (
        f"Доходы: {inc:.2f}\n"
        f"Расходы: {exp:.2f}\n"
        f"Итого: {net:.2f}\n"
        f"Дебиторы (вам должны): {rec:.2f}\n"
        f"Кредиторы (вы должны): {pay:.2f}"
    )
    await update.message.reply_text(txt, reply_markup=menu_kb())

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    rows = db.last_tx(update.effective_user.id, 20)
    if not rows:
        await update.message.reply_text("Пока пусто.", reply_markup=menu_kb())
        return
    lines = []
    for r in rows:
        sign = "+" if r["type"] == "income" else "-"
        when = datetime.fromtimestamp(r["created_at"] / 1000).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{when} • {sign}{r['amount']} {r['currency']} • {r['category'] or 'Без категории'}")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_kb())

# ---------- ДОХОД / РАСХОД ----------
async def ask_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    context.user_data.clear()
    context.user_data["mode"] = "income"
    await update.message.reply_text("Доход: введите сумму (напр. 120000 или 15.5 USD):",
                                    reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True))
    return ADD_AMOUNT

async def ask_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    context.user_data.clear()
    context.user_data["mode"] = "expense"
    await update.message.reply_text("Расход: введите сумму (напр. 120000 или 15.5 USD):",
                                    reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True))
    return ADD_AMOUNT

async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    text = update.message.text or ""
    if text.lower() == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=menu_kb())
        return ConversationHandler.END
    p = parse_free_text(text)
    if not p.get("amount"):
        await update.message.reply_text("Не смог распознать сумму. Введите ещё раз или нажмите Отмена:")
        return ADD_AMOUNT
    context.user_data["amount"] = p["amount"]
    context.user_data["currency"] = p.get("currency", "UZS")
    await update.message.reply_text("Добавьте описание / категорию (или отправьте пусто).")
    return ADD_DESC

async def got_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    note = update.message.text or None
    mode = context.user_data.get("mode")
    amount = float(context.user_data.get("amount", 0))
    currency = context.user_data.get("currency", "UZS")
    db.add_tx(update.effective_user.id, mode, amount, currency, None, note)
    await update.message.reply_text("✅ Сохранено.", reply_markup=menu_kb())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=menu_kb())
    return ConversationHandler.END

# ---------- ДЕБИТОРЫ / КРЕДИТОРЫ ----------
def debt_menu_markup(kind: str):
    # kind: 'receivable' (вам должны) или 'payable' (вы должны)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Новый", callback_data=f"debt_{kind}_new"),
         InlineKeyboardButton("📋 Открытые", callback_data=f"debt_{kind}_list")],
        [InlineKeyboardButton("✅ Погасить", callback_data=f"debt_{kind}_close"),
         InlineKeyboardButton("📊 Баланс", callback_data=f"debt_{kind}_balance")],
    ])

async def debtors_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text("Дебиторы — выберите действие:", reply_markup=menu_kb())
    await update.message.reply_text("Меню:", reply_markup=debt_menu_markup("receivable"))

async def creditors_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text("Кредиторы — выберите действие:", reply_markup=menu_kb())
    await update.message.reply_text("Меню:", reply_markup=debt_menu_markup("payable"))

async def debt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.callback_query.answer()
        return
    q = update.callback_query
    await q.answer()
    data = q.data
    if data.endswith("_new"):
        kind = "receivable" if "receivable" in data else "payable"
        context.user_data.clear()
        context.user_data["debt_kind"] = kind
        await q.message.reply_text("Введите имя/название контрагента:",
                                   reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True))
        return DEBT_CP
    elif data.endswith("_list"):
        kind = "receivable" if "receivable" in data else "payable"
        rows = db.open_debts(update.effective_user.id, kind)
        if not rows:
            await q.message.reply_text("Открытых долгов нет.")
        else:
            lines = [f"ID#{r['id']} • {r['cp_name']} • {r['amount']} {r['currency']} • "
                     f"до {datetime.fromtimestamp(r['due_date']/1000).strftime('%d.%m.%Y') if r['due_date'] else '—'}"
                     for r in rows]
            await q.message.reply_text("\n".join(lines))
    elif data.endswith("_close"):
        kind = "receivable" if "receivable" in data else "payable"
        context.user_data.clear()
        context.user_data["debt_kind"] = kind
        await q.message.reply_text("Отправьте ID долга для закрытия (посмотрите через 'Открытые'):",
                                   reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True))
        return DEBT_CLOSE_ID
    elif data.endswith("_balance"):
        kind = "receivable" if "receivable" in data else "payable"
        total = db.debt_total(update.effective_user.id, kind)
        label = "вам должны" if kind == "receivable" else "вы должны"
        await q.message.reply_text(f"Всего {label}: {total:.2f}")

async def debt_get_cp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    text = update.message.text or ""
    if text.lower() == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=menu_kb()); return ConversationHandler.END
    context.user_data["cp_name"] = text.strip()
    await update.message.reply_text("Введите сумму (например: 500 или 20 USD):")
    return DEBT_AMOUNT

async def debt_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    text = update.message.text or ""
    p = parse_free_text(text)
    if not p.get("amount"):
        await update.message.reply_text("Не распознал сумму. Введите ещё раз:")
        return DEBT_AMOUNT
    context.user_data["amount"] = float(p["amount"])
    context.user_data["currency"] = p.get("currency", "UZS")
    await update.message.reply_text("Срок (дд.мм.гггг), можно 'сегодня', 'завтра' или '-' если без срока:")
    return DEBT_DUE

async def debt_get_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    text = update.message.text or ""
    if text.strip() == "-":
        due_ts = None
    else:
        due_ts = parse_due(text)
        if due_ts is None:
            await update.message.reply_text("Не понял дату. Введите дд.мм.гггг, 'сегодня' или 'завтра', либо '-' без срока:")
            return DEBT_DUE
    context.user_data["due_ts"] = due_ts
    await update.message.reply_text("Комментарий (можно пропустить пустым сообщением):")
    return DEBT_NOTE

async def debt_get_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    note = update.message.text or None
    d = context.user_data
    db.add_debt(update.effective_user.id, d["debt_kind"], d["cp_name"], d["amount"], d["currency"], note, d.get("due_ts"))
    await update.message.reply_text("✅ Долг добавлен.", reply_markup=menu_kb())
    return ConversationHandler.END

async def debt_close_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    text = update.message.text or ""
    if text.lower() == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=menu_kb()); return ConversationHandler.END
    try:
        debt_id = int(text.strip().lstrip("#"))
    except:
        await update.message.reply_text("Нужен числовой ID. Введите ещё раз или Отмена:")
        return DEBT_CLOSE_ID
    ok = db.close_debt(update.effective_user.id, debt_id)
    if ok:
        await update.message.reply_text("✅ Закрыто.", reply_markup=menu_kb())
    else:
        await update.message.reply_text("Не найден открытый долг с таким ID.", reply_markup=menu_kb())
    return ConversationHandler.END

# ---------- Быстрая запись по свободному тексту ----------
async def quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text or ""
    p = parse_free_text(text)
    if p.get("amount"):
        db.add_tx(update.effective_user.id, p.get("mode", "expense"), float(p["amount"]),
                  p.get("currency", "UZS"), None, p.get("note"))
        await update.message.reply_text("✅ Записал. Напишите 'баланс' или 'история'.", reply_markup=menu_kb())
    else:
        await update.message.reply_text("Не понял. Нажмите кнопки внизу или напишите 'меню'.", reply_markup=menu_kb())

# ---------- Уведомления о просрочках ----------
async def notify_overdues(context: ContextTypes.DEFAULT_TYPE):
    for uid in db.list_allowed_ids():
        rows = db.overdue_debts(uid)
        if not rows:
            continue
        lines = [
            f"ID#{r['id']} • {r['cp_name']} • {r['amount']} {r['currency']} • "
            f"срок был {datetime.fromtimestamp(r['due_date']/1000).strftime('%d.%m.%Y')}"
            for r in rows
        ]
        try:
            await context.bot.send_message(uid, "🔔 Просроченные долги:\n" + "\n".join(lines))
        except Exception as e:
            print("notify error:", e)

# >>> ДОБАВЛЕНО: post_init, чтобы job_queue был инициализирован
async def _post_init(app: Application):
    app.job_queue.run_repeating(notify_overdues, interval=60*60*12, first=60)

# ---------- Сборка приложения ----------
def build_app() -> Application:
    # >>> ИЗМЕНЕНО: регистрируем post_init здесь
    app = ApplicationBuilder().token(TOKEN).post_init(_post_init).build()

    conv_tx = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^\+|^➕|^Доход$"), ask_income),
            MessageHandler(filters.Regex(r"^\-|^➖|^Расход$"), ask_expense),
        ],
        states={
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount)],
            ADD_DESC:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    conv_debt = ConversationHandler(
        entry_points=[CallbackQueryHandler(debt_cb, pattern=r"^debt_")],
        states={
            DEBT_CP:       [MessageHandler(filters.TEXT & ~filters.COMMAND, debt_get_cp)],
            DEBT_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, debt_get_amount)],
            DEBT_DUE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, debt_get_due)],
            DEBT_NOTE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, debt_get_note)],
            DEBT_CLOSE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, debt_close_id)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r"^(Меню|меню)$"), menu))
    app.add_handler(MessageHandler(filters.Regex(r"^💰|^Баланс$"), balance))
    app.add_handler(MessageHandler(filters.Regex(r"^📒|^История$"), history))
    app.add_handler(MessageHandler(filters.Regex(r"^🤝 Дебиторы$"), debtors_entry))
    app.add_handler(MessageHandler(filters.Regex(r"^💳 Кредиторы$"), creditors_entry))
    app.add_handler(conv_tx)
    app.add_handler(conv_debt)
    app.add_handler(CallbackQueryHandler(debt_cb, pattern=r"^debt_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, quick_add))

    return app

async def _boot():
    app = build_app()
    # Снять любой активный webhook перед polling
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print("delete_webhook warning:", e)
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(_boot())
