import os
import sys
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

from handlers.common import myid_command, help_command, error_handler
from handlers.alerts import (
    alerts_command,
    status_command,
    alerts_on_command,
    alerts_off_command,
    map_command,
    location_handler,
    cancel_handler,
    handle_callback_query,
    handle_unknown,
    alerts_monitoring_loop,
    auto_update
)

# Load environment variables
load_dotenv()

TOKEN = os.getenv("ALERTS_BOT_TOKEN") or os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not TOKEN:
    raise ValueError("No ALERTS_BOT_TOKEN provided. Please set ALERTS_BOT_TOKEN in .env file.")

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Налаштування тривог та локація"),
        BotCommand("alerts", "Налаштування тривог та локація"),
        BotCommand("status", "Поточний стан тривог у вашому районі"),
        BotCommand("alerts_on", "Увімкнути сповіщення"),
        BotCommand("alerts_off", "Вимкнути сповіщення"),
        BotCommand("map", "Карта тривог України зараз"),
        BotCommand("help", "Як користуватися та можливості"),
    ]
    await application.bot.set_my_commands(commands)

    if ADMIN_ID:
        try:
            await application.bot.send_message(
                chat_id=int(ADMIN_ID),
                text="🔔 Бот тривог успішно запущений і готовий до роботи! ✅\nПідключено асинхронну БД (aiosqlite) та моніторинг NEPTUN.",
                disable_notification=True
            )
        except Exception as e:
            logger.warning(f"Could not send startup message to admin: {e}")

    # Запуск фонового моніторингу тривог та автооновлення
    asyncio.create_task(alerts_monitoring_loop(application))
    asyncio.create_task(auto_update(application))

def main() -> None:
    print("Starting specialized NEPTUN Alerts Bot...")

    downloads_dir = "downloads"
    os.makedirs(downloads_dir, exist_ok=True)

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Реєстрація централізованого обробника помилок
    application.add_error_handler(error_handler)

    # Команди бота тривог
    application.add_handler(CommandHandler("start", alerts_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("check", status_command))
    application.add_handler(CommandHandler("alerts_on", alerts_on_command))
    application.add_handler(CommandHandler("alerts_off", alerts_off_command))
    application.add_handler(CommandHandler("map", map_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("help", help_command))

    # Обробник геолокації та скасування
    application.add_handler(MessageHandler(filters.LOCATION, location_handler))
    application.add_handler(MessageHandler(filters.Regex(r"^❌ Скасувати$"), cancel_handler))

    # Обробник callback кнопок
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Загальний обробник текстових повідомлень (пошук міст)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
