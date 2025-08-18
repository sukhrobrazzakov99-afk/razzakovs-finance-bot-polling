# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, CallbackQueryHandler, filters, JobQueue
)
from ai_helper import parse_free_text, parse_due
from db import DB

# === ТОКЕН (новый) ===
TOKEN = "7611168200:AAHj7B6FelvvcoJMDBuKwKpveBHEo0NItnI"

# Разрешённые пользователи (по username)
OWNER_USERNAMES = ["SukhrobAbdurazzakov", "revivemd"]

db = DB("data.sqlite")

# Состояния
ADD_AMOUNT, ADD_CATEGORY, ADD_NOTE, DEBT_CP, DEBT_AMOUNT, DEBT_DUE, DEBT_NOTE, DEBT_CLOSE_ID = range(8)

EXPENSE_CATS = ["Еда", "Транспорт", "Жильё", "Связь/интернет", "Здоровье", "Одежда", "Развлечения", "Образование", "Подарки", "Другое"]
INCOME_CATS  = ["Зарплата", "Бонус", "Подарок", "Другое"]

def menu_kb():
    rows = [
        ["➕ Доход", "➖ Расход"],
        ["💰 Баланс", "📒 История"],
        ["📊 Отчёт", "🤝 Дебиторы"],
        ["💳 Кредиторы"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def cat_kb(mode: str):
    cats = INCOME_CATS if mode == "income" else EXPENSE_CATS
    rows = [cats[i:i+3] for i in range(0, len(cats), 3)]
    rows.append(["Пропустить"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def is_allowed(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    if u.username and u.username in OWNER_USERNAMES:
        return True
    return db.is_allowed(u.id)

# ---------------- БАЗА ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        if db.allow_count() < 2:
            db.allow(update.effective_user.id)
        else:
            await update.message.reply_text("⛔️ Доступ ограничен.")
            return
    await update.message.reply_text("Выбирай действие:", reply_markup=menu_kb())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    inc, exp, net = db.get_balance(update.effective_user.id)
    rec, pay = db.get_debt_totals(update.effective_user.id)
    txt = (
        f"Доходы: {inc:.2f}\nРасходы: {exp:.2f}\nИтого: {net:.2f}\n"
        f"Дебиторы (вам должны): {rec:.2f}\nКредиторы (вы должны): {pay:.2f}"
    )
    await update.message.reply_text(txt, reply_markup=menu_kb())

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    rows = db.last_tx(update.effective_user.id, 20)
    if not rows:
        await update.message.reply_text("Пока пусто.", reply_markup=menu_kb()); return
    lines = []
    for r in rows:
        sign = "+" if r["type"] == "income" else "-"
        when = datetime.fromtimestamp(r["created_at"]/1000).strftime("%Y-%m-%d %H:%M")
        cat = r["category"] or "Без категории"
        lines.append(f"{when} • {sign}{r['amount']} {r['currency']} • {cat}")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_kb())

# -------- Доход/Расход с категориями --------
async def ask_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    context.user_data.clear(); context.user_data["mode"] = "income"
    await update.message.reply_text(
        "Доход: сумма (напр. 120000 или 20 USD):",
        reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)
    )
    return ADD_AMOUNT

async def ask_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    context.user_data.clear(); context.user_data["mode"] = "expense"
    await update.message.reply_text(
        "Расход: сумма (напр. 120000 или 20 USD):",
        reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)
    )
    return ADD_AMOUNT

async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    txt = update.message.text or ""
    if txt.lower() == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=menu_kb()); return ConversationHandler.END
    p = parse_free_text(txt)
    if not p.get("amount"):
        await update.message.reply_text("Не распознал сумму. Ещё раз или Отмена:"); return ADD_AMOUNT
    context.user_data["amount"] = float(p["amount"])
    context.user_data["currency"] = p.get("currency", "UZS")
    if p.get("category"):
        context.user_data["category"] = p["category"]
        await update.message.reply_text("Комментарий (можно пусто):"); return ADD_NOTE
    await update.message.reply_text(
        "Выберите категорию (или 'Пропустить'):",
        reply_markup=cat_kb(context.user_data["mode"])
    )
    return ADD_CATEGORY

async def got_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    cat = update.message.text or ""
    if cat == "Пропустить":
        context.user_data["category"] = None
    else:
        context.user_data["category"] = cat
    await update.message.reply_text(
        "Комментарий (можно пусто):",
        reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)
    )
    return ADD_NOTE

async def got_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    note = update.message.text or None
    db.add_tx(
        update.effective_user.id,
        context.user_data.get("mode"),
        float(context.user_data.get("amount", 0)),
        context.user_data.get("currency", "UZS"),
        context.user_data.get("category"),
        note,
    )
    await update.message.reply_text("✅ Сохранено.", reply_markup=menu_kb())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=menu_kb())
    return ConversationHandler.END

# ------------- Отчёт по категориям -------------
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    now = datetime.now()
    start = datetime(now.year, now.month, 1)
    end = (start + timedelta(days=32)).replace(day=1)
    s_ms, e_ms = int(start.timestamp()*1000), int(end.timestamp()*1000)

    ei = db.totals_by_category(update.effective_user.id, "income", s_ms, e_ms)
    ee = db.totals_by_category(update.effective_user.id, "expense", s_ms, e_ms)

    lines = ["📊 Отчёт за текущий месяц:"]
    if ei:
        lines.append("\nДоходы:")
        for r in ei:
            lines.append(f"• {r['cat']}: {r['s']:.2f}")
    if ee:
        lines.append("\nРасходы:")
        for r in ee:
            lines.append(f"• {r['cat']}: {r['s']:.2f}")
    if not ei and not ee:
        lines.append("Нет данных.")
    await update.message.reply_text("\n".join(lines), reply_markup=menu_kb())

# -------- Дебиторы/Кредиторы --------
def debt_menu_markup(kind: str):
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
    if not is_allowed(update): await update.callback_query.answer(); return
    q = update.callback_query; await q.answer()
    data = q.data
    if data.endswith("_new"):
        kind = "receivable" if "receivable" in data else "payable"
        context.user_data.clear(); context.user_data["debt_kind"] = kind
        await q.message.reply_text("Имя/название контрагента:",
            reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True))
        return DEBT_CP
    elif data.endswith("_list"):
        kind = "receivable" if "receivable" in data else "payable"
        rows = db.open_debts(update.effective_user.id, kind)
        if not rows: await q.message.reply_text("Открытых долгов нет.")
        else:
            lines = [f"ID#{r['id']} • {r['cp_name']} • {r['amount']} {r['currency']} • "
                     f"до {datetime.fromtimestamp(r['due_date']/1000).strftime('%d.%m.%Y') if r['due_date'] else '—'}"
                     for r in rows]
            await q.message.reply_text("\n".join(lines))
    elif data.endswith("_close"):
        context.user_data.clear()
        await q.message.reply_text("Отправьте ID долга для закрытия:",
            reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True))
        return DEBT_CLOSE_ID
    elif data.endswith("_balance"):
        kind = "receivable" if "receivable" in data else "payable"
        total = db.debt_total(update.effective_user.id, kind)
        label = "вам должны" if kind=="receivable" else "вы должны"
        await q.message.reply_text(f"Всего {label}: {total:.2f}")

async def debt_get_cp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    t = update.message.text or ""
    if t.lower() == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=menu_kb()); return ConversationHandler.END
    context.user_data["cp_name"] = t.strip()
    await update.message.reply_text("Сумма (например: 500 или 20 USD):"); return DEBT_AMOUNT

async def debt_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    p = parse_free_text(update.message.text or "")
    if not p.get("amount"):
        await update.message.reply_text("Не распознал сумму. Ещё раз:"); return DEBT_AMOUNT
    context.user_data["amount"] = float(p["amount"])
    context.user_data["currency"] = p.get("currency","UZS")
    await update.message.reply_text("Срок (дд.мм.гггг) или 'сегодня'/'завтра', '-' без срока:"); return DEBT_DUE

async def debt_get_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    t = (update.message.text or "").strip()
    due_ts = None if t == "-" else parse_due(t)
    if t != "-" and due_ts is None:
        await update.message.reply_text("Не понял дату. Дд.мм.гггг / сегодня / завтра / '-':"); return DEBT_DUE
    context.user_data["due_ts"] = due_ts
    await update.message.reply_text("Комментарий (можно пусто):"); return DEBT_NOTE

async def debt_get_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    d = context.user_data
    db.add_debt(update.effective_user.id, d["debt_kind"], d["cp_name"], d["amount"], d["currency"], update.message.text or None, d.get("due_ts"))
    await update.message.reply_text("✅ Долг добавлен.", reply_markup=menu_kb())
    return ConversationHandler.END

async def debt_close_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    t = (update.message.text or "").strip()
    if t.lower() == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=menu_kb()); return ConversationHandler.END
    try:
        debt_id = int(t.lstrip("#"))
    except:
        await update.message.reply_text("Нужен числовой ID. Ещё раз или Отмена:"); return DEBT_CLOSE_ID
    ok = db.close_debt(update.effective_user.id, debt_id)
    await update.message.reply_text("✅ Закрыто." if ok else "Не найден открытый долг с таким ID.", reply_markup=menu_kb())
    return ConversationHandler.END

# ---------- Быстрая запись ----------
async def quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    p = parse_free_text(update.message.text or "")
    if p.get("amount"):
        db.add_tx(
            update.effective_user.id,
            p.get("mode","expense"),
            float(p["amount"]),
            p.get("currency","UZS"),
            p.get("category"),
            p.get("note"),
        )
        await update.message.reply_text("✅ Записал. Напишите 'баланс', 'история' или 'отчёт'.", reply_markup=menu_kb())
    else:
        await update.message.reply_text("Не понял. Нажмите кнопки или напишите 'меню'.", reply_markup=menu_kb())

# ---------- Напоминания (JobQueue) ----------
async def notify_overdues(context: ContextTypes.DEFAULT_TYPE):
    for uid in db.list_allowed_ids():
        rows = db.overdue_debts(uid)
        if not rows: continue
        lines = [
            f"ID#{r['id']} • {r['cp_name']} • {r['amount']} {r['currency']} • "
            f"срок был {datetime.fromtimestamp(r['due_date']/1000).strftime('%d.%m.%Y')}"
            for r in rows
        ]
        try:
            await context.bot.send_message(uid, "🔔 Просроченные долги:\n" + "\n".join(lines))
        except Exception as e:
            print("notify error:", e)

async def _post_init(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print("delete_webhook warning:", e)

    try:
        if app.job_queue is None:
            jq = JobQueue(loop=asyncio.get_running_loop())
            jq.set_application(app)
            app.job_queue = jq
        app.job_queue.run_repeating(notify_overdues, interval=60*60*12, first=60)
    except Exception as e:
        print("JobQueue disabled:", e)

def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).post_init(_post_init).build()

    conv_tx = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^\+|^➕|^Доход$"), ask_income),
            MessageHandler(filters.Regex(r"^\-|^➖|^Расход$"), ask_expense),
        ],
        states={
            ADD_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount)],
            ADD_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_category)],
            ADD_NOTE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_note)],
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
        per_chat=True,
        per_user=True,
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r"^(Меню|меню)$"), menu))
    app.add_handler(MessageHandler(filters.Regex(r"^💰|^Баланс$"), balance))
    app.add_handler(MessageHandler(filters.Regex(r"^📒|^История$"), history))
    app.add_handler(MessageHandler(filters.Regex(r"^📊 Отчёт$|^отч(е|ё)т$"), report))
    app.add_handler(MessageHandler(filters.Regex(r"^🤝 Дебиторы$"), debtors_entry))
    app.add_handler(MessageHandler(filters.Regex(r"^💳 Кредиторы$"), creditors_entry))
    app.add_handler(conv_tx)
    app.add_handler(conv_debt)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, quick_add))
    return app

def main():
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()


