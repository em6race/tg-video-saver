import os
import sys
import logging
import re
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

from services.server import start_server_thread
from handlers.common import myid_command, help_command, error_handler
from handlers.admin import (
    start,
    invite_command,
    clear_invites_command,
    users_command,
    restart_command
)
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
from handlers.media import handle_url

# Load environment variables
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not TOKEN:
    raise ValueError("No BOT_TOKEN provided. Please set it in .env file.")

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Запустити бота"),
        BotCommand("alerts", "Налаштування тривог та локація"),
        BotCommand("status", "Поточний стан тривог у вашому районі"),
        BotCommand("alerts_on", "Увімкнути сповіщення"),
        BotCommand("alerts_off", "Вимкнути сповіщення"),
        BotCommand("map", "Карта тривог України зараз"),
        BotCommand("help", "Як користуватися та можливості"),
        BotCommand("invite", "Створити запрошення (для адміна)"),
        BotCommand("clear_invites", "Видалити невикористані інвайти (адмін)"),
        BotCommand("users", "Список користувачів (для адміна)"),
        BotCommand("restart", "Перезапустити бота (для адміна)"),
    ]
    await application.bot.set_my_commands(commands)

    if ADMIN_ID:
        try:
            await application.bot.send_message(
                chat_id=int(ADMIN_ID),
                text="Бот успішно запущений і готовий до роботи! ✅\nПідключено асинхронну БД (aiosqlite) та централізований обробник помилок.",
                disable_notification=True
            )
        except Exception as e:
            logger.warning(f"Could not send startup message to admin: {e}")

    # Запуск фонового моніторингу тривог та автооновлення
    asyncio.create_task(alerts_monitoring_loop(application))
    asyncio.create_task(auto_update(application))

def main() -> None:
    print("Starting bot with async architecture & error handler...")

    downloads_dir = "downloads"
    if os.path.exists(downloads_dir):
        import shutil
        try:
            shutil.rmtree(downloads_dir)
        except Exception as e:
            logger.warning(f"Failed to clean up downloads directory on startup: {e}")
    os.makedirs(downloads_dir, exist_ok=True)

    # Запуск тестового HTTP-сервера для хостингу
    start_server_thread()

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Реєстрація централізованого обробника помилок
    application.add_error_handler(error_handler)

    # Реєстрація команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("check", status_command))
    application.add_handler(CommandHandler("alerts_on", alerts_on_command))
    application.add_handler(CommandHandler("alerts_off", alerts_off_command))
    application.add_handler(CommandHandler("map", map_command))
    application.add_handler(CommandHandler("invite", invite_command))
    application.add_handler(CommandHandler("clear_invites", clear_invites_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("restart", restart_command))

    # Обробник геолокації та скасування
    application.add_handler(MessageHandler(filters.LOCATION, location_handler))
    application.add_handler(MessageHandler(filters.Regex(r"^❌ Скасувати$"), cancel_handler))

    # Обробник посилань та callback кнопок
    url_pattern = re.compile(r'https?://[^\s]+')
    application.add_handler(MessageHandler(filters.Regex(url_pattern), handle_url))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Загальний обробник текстових повідомлень
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
