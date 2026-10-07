import os
import re
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from handlers.common import (
    myid_command,
    help_command,
    error_handler,
    is_media_plugin_enabled,
    is_alerts_plugin_enabled,
)
from handlers.admin import (
    start,
    invite_command,
    invites_list_command,
    clear_invites_command,
    users_command,
    restart_command,
)
from handlers.alerts import (
    alerts_command,
    status_command,
    alerts_on_command,
    alerts_off_command,
    map_command,
    putin_command,
    location_handler,
    cancel_handler,
    handle_callback_query,
    handle_unknown,
    alerts_monitoring_loop,
    auto_update,
)

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
    alerts_enabled = is_alerts_plugin_enabled()

    commands = [
        BotCommand("start", "Запустити бота"),
    ]
    if alerts_enabled:
        commands.extend([
            BotCommand("alerts", "Налаштування тривог та локація"),
            BotCommand("status", "Поточний стан тривог у вашому районі"),
            BotCommand("alerts_on", "Увімкнути сповіщення"),
            BotCommand("alerts_off", "Вимкнути сповіщення"),
            BotCommand("map", "Карта тривог України зараз"),
        ])
    commands.extend([
        BotCommand("help", "Як користуватися та можливості"),
        BotCommand("invite", "Конструктор запрошень (адмін)"),
        BotCommand("invites", "Список активних запрошень (адмін)"),
        BotCommand("clear_invites", "Анулювати невикористані інвайти (адмін)"),
        BotCommand("users", "Список користувачів (для адміна)"),
        BotCommand("restart", "Оновити з GitHub та перезапустити (адмін)"),
    ])
    await application.bot.set_my_commands(commands)

    logger.info(f"Bot commands registered (Alerts Enabled: {alerts_enabled}). Bot is ready.")

    # Запуск фонового моніторингу тривог (тільки якщо плагін увімкнено)
    if alerts_enabled:
        asyncio.create_task(alerts_monitoring_loop(application))

    # Запуск фонового автооновлення з GitHub
    asyncio.create_task(auto_update(application))

def main() -> None:
    media_enabled = is_media_plugin_enabled()
    alerts_enabled = is_alerts_plugin_enabled()
    print(f"Starting Telegram bot (Media: {media_enabled}, Alerts: {alerts_enabled})...")

    if media_enabled:
        downloads_dir = "downloads"
        if os.path.exists(downloads_dir):
            import shutil
            try:
                shutil.rmtree(downloads_dir)
            except Exception as e:
                logger.warning(f"Failed to clean up downloads directory on startup: {e}")
        os.makedirs(downloads_dir, exist_ok=True)

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Реєстрація централізованого обробника помилок
    application.add_error_handler(error_handler)

    # Реєстрація базових команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("invite", invite_command))
    application.add_handler(CommandHandler("invites", invites_list_command))
    application.add_handler(CommandHandler("clear_invites", clear_invites_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("restart", restart_command))

    # Реєстрація команд тривог (тільки якщо ENABLE_ALERTS_PLUGIN=true)
    if alerts_enabled:
        application.add_handler(CommandHandler("alerts", alerts_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CommandHandler("check", status_command))
        application.add_handler(CommandHandler("alerts_on", alerts_on_command))
        application.add_handler(CommandHandler("alerts_off", alerts_off_command))
        application.add_handler(CommandHandler("map", map_command))
        application.add_handler(CommandHandler(["putin", "dox", "dead"], putin_command))
        application.add_handler(MessageHandler(filters.LOCATION, location_handler))
        application.add_handler(MessageHandler(filters.Regex(r"^❌ Скасувати$"), cancel_handler))

    # Модульний плагін завантаження медіа (вмикається тільки якщо ENABLE_MEDIA_PLUGIN=true)
    if media_enabled:
        from handlers.media import handle_url
        url_pattern = re.compile(r"https?://[^\s]+")
        application.add_handler(MessageHandler(filters.Regex(url_pattern), handle_url))

    # Обробник callback кнопок
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Загальний обробник текстових повідомлень (кнопки меню та пошук міста)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
