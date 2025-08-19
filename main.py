import os
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# 🔑 Токен и URL (всегда заполняю сам)
BOT_TOKEN = "7611168200:AAHj7B6FeIvvoJMDBuKwKpveBHEoNItnI"
WEBHOOK_URL = "https://beautiful-love.up.railway.app"  # твой Railway адрес

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает ✅")

# Основная функция
def main():
    # Создаём приложение
    application = Application.builder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))

    # 🚀 Настраиваем Webhook
    bot = Bot(BOT_TOKEN)
    import asyncio
    asyncio.run(bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}"))

    # Запуск через webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8443)),
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()
