import logging
import re
from datetime import datetime
from io import BytesIO

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

import db
from ai_helper import parse_free_text, ai_answer

TOKEN = "7611168200:AAFkdTWAz1xMawJOKF0Mu21ViFA5Oz8wblk"
AUTHORIZED_IDS = [564415186, 1038649944]

MENU_KB = ReplyKeyboardMarkup(
    [
        ["➕ Доход", "➖ Расход"],
        ["💰 Баланс", "🧾 История"],
        ["📤 Excel", "🤖 AI"]
    ],
    resize_keyboard=True
)

ADD_AMOUNT, ADD_DESC = range(2)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("razzakovs_bot")


def allowed(user_id: int) -> bool:
    return user_id in AUTHORIZED_IDS


async def deny(update: Update) -> None:
    await update.effective_chat.send_message("⛔ Доступ запрещён.")


async def show_menu(update: Update) -> None:
    await update.effective_chat.send_message("Выбирай действие:", reply_markup=MENU_KB)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return await deny(update)
    await show_menu(update)


async def ask_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["kind"] = "income"
    await update.message.reply_text(
        "Сумма и валюта (пример: `150000 uzs` или `20 usd`)",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return ADD_AMOUNT


async def ask_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["kind"] = "expense"
    await update.message.reply_text(
        "Сумма и валюта (пример: `120000 uzs` или `5 usd`)",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return ADD_AMOUNT


async def got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update.effective_user.id):
        return ConversationHandler.END
    parsed = parse_free_text(update.message.text or "")
    if not parsed or not parsed.get("amount") or not parsed.get("currency"):
        await update.message.reply_text(
            "Не понял сумму/валюту. Пример: `150000 uzs` или `20 usd`",
            parse_mode="Markdown",
        )
        return ADD_AMOUNT
    context.user_data["amount"] = parsed["amount"]
    context.user_data["currency"] = parsed["currency"]
    await update.message.reply_text("Короткое описание: на что / откуда?")
    return ADD_DESC


async def got_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update.effective_user.id):
        return ConversationHandler.END
    desc = (update.message.text or "").strip()
    kind = context.user_data.get("kind")
    amount = float(context.user_data.get("amount"))
    currency = context.user_data.get("currency")
    ts = datetime.utcnow().isoformat(timespec="seconds")

    db.add_operation(
        user_id=update.effective_user.id,
        kind=kind, amount=amount, currency=currency, description=desc, ts=ts
    )
    await update.message.reply_text("✅ Сохранил. Меню ниже.", reply_markup=MENU_KB)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.", reply_markup=MENU_KB)
    return ConversationHandler.END


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return await deny(update)
    bal = db.get_balance()
    uzs = int(bal.get("UZS", 0))
    usd = float(bal.get("USD", 0))
    text = f"💰 Баланс:\n• UZS: {uzs:,}\n• USD: {usd:.2f}"
    await update.message.reply_text(text.replace(",", " "), reply_markup=MENU_KB)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return await deny(update)
    rows = db.get_recent(limit=15)
    if not rows:
        return await update.message.reply_text("История пуста.", reply_markup=MENU_KB)
    lines = ["🧾 Последние операции:"]
    for r in rows:
        sign = "+" if r["kind"] == "income" else "-"
        amt = f"{int(r['amount'])}" if r["currency"] == "UZS" else f"{r['amount']:.2f}"
        when = r["ts"][5:16]
        lines.append(f"{when} | {sign}{amt} {r['currency']} | {r['description']}")
    await update.message.reply_text("\n".join(lines), reply_markup=MENU_KB)


async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return await deny(update)
    import pandas as pd
    df = db.get_dataframe(limit=1000)
    if df.empty:
        return await update.message.reply_text("Нет данных для экспорта.", reply_markup=MENU_KB)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="operations")
    output.seek(0)
    await update.message.reply_document(
        document=InputFile(output, filename="export.xlsx"),
        caption="Экспорт операций",
        reply_markup=MENU_KB
    )


async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return await deny(update)
    await update.message.reply_text(
        "Напиши вопрос/фразу. Я отвечу как помощник.",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data["ai_mode"] = True


async def ai_or_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return await deny(update)
    if context.user_data.get("ai_mode"):
        text = update.message.text or ""
        reply = ai_answer(text)
        context.user_data["ai_mode"] = False
        return await update.message.reply_text(reply, reply_markup=MENU_KB)
    parsed = parse_free_text(update.message.text or "")
    if parsed and parsed.get("amount") and parsed.get("currency"):
        db.add_operation(
            user_id=update.effective_user.id,
            kind="expense",
            amount=float(parsed["amount"]),
            currency=parsed["currency"],
            description=parsed.get("desc", ""),
            ts=datetime.utcnow().isoformat(timespec="seconds"),
        )
        return await update.message.reply_text("✅ Сохранил как расход. Меню ниже.", reply_markup=MENU_KB)
    await update.message.reply_text("Выбери действие на клавиатуре ниже.", reply_markup=MENU_KB)



def build_app() -> Application:
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Доход$"), ask_income),
            MessageHandler(filters.Regex("^➖ Расход$"), ask_expense),
        ],
        states={
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(MessageHandler(filters.Regex("^💰 Баланс$"), balance))
    app.add_handler(MessageHandler(filters.Regex("^🧾 История$"), history))
    app.add_handler(MessageHandler(filters.Regex("^📤 Excel$"), export_excel))
    app.add_handler(MessageHandler(filters.Regex("^🤖 AI$"), ai_chat))

    app.add_handler(conv)

    # Любой другой текст — либо AI режим, либо возвращаем в меню
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_or_menu))

    return app


def main():
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
