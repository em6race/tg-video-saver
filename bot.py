import os
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from downloader import download_video

# Load environment variables
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("No BOT_TOKEN provided. Please set it in .env file.")

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привіт! Я бот для завантаження відео 🎥\n"
        "Просто відправ мені посилання на TikTok, Instagram, YouTube або Twitter, і я завантажу його для тебе!"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Як це працює:\n"
        "1. Знаходиш цікаве відео.\n"
        "2. Копіюєш посилання.\n"
        "3. Відправляєш це посилання мені.\n"
        "4. Чекаєш кілька секунд/хвилин і отримуєш відео у форматі MP4.\n\n"
        "Підтримувані платформи: TikTok, Instagram, YouTube, Twitter та багато інших!"
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text
    
    # Send a waiting message
    status_msg = await update.message.reply_text("⏳ Завантажую відео, зачекайте...")
    
    try:
        # Download video
        file_path = await download_video(url)
        
        # Send video
        with open(file_path, 'rb') as video_file:
            try:
                await update.message.reply_video(
                    video=video_file,
                    caption="Ось ваше відео! 🎬",
                    write_timeout=60,
                    read_timeout=60,
                    pool_timeout=60
                )
            except Exception as vid_e:
                logger.warning(f"Failed to send as video: {vid_e}. Trying as document...")
                video_file.seek(0)
                await update.message.reply_document(
                    document=video_file,
                    caption="Ось ваш файл (не вдалося відправити як відео) 🎬",
                    write_timeout=60,
                    read_timeout=60,
                    pool_timeout=60
                )
        
        # Clean up the file after sending
        os.remove(file_path)
        
        # Delete the waiting message
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"Error downloading {url}: {e}")
        await status_msg.edit_text(f"❌ Сталася помилка при завантаженні відео.\n\nДеталі помилки (перешліть розробнику):\n`{e}`", parse_mode="Markdown")

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Будь ласка, відправте мені дійсне посилання на відео.")

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    print(f"Dummy web server started on port {port}")
    server.serve_forever()

def main() -> None:
    print("Starting bot...")
    
    # Start the dummy web server in a background thread for Render
    threading.Thread(target=run_dummy_server, daemon=True).start()

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    
    # Regex to match URLs
    url_pattern = re.compile(r'https?://[^\s]+')
    application.add_handler(MessageHandler(filters.Regex(url_pattern), handle_url))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
