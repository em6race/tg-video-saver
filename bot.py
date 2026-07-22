import os
import sys
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import Update, BotCommand, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from downloader import download_video, download_audio
from tiktok_api import fetch_tiktok_data
from spotify_api import get_spotify_track_name

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привіт! Я бот для завантаження відео 🎥\n"
        "Просто відправ мені посилання на TikTok, Instagram, YouTube або Twitter, і я завантажу його для тебе!"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Щоб завантажити відео, фото або звук з TikTok, Twitter, YouTube Shorts, SoundCloud та інших — надішли мені посилання 😉\n\n"
        "Можливості бота:\n"
        "🔹 Завантаження відео без водяних знаків;\n"
        "🔹 Завантаження фото-слайдшоу;\n"
        "🔹 Завантаження аудіо;\n"
        "🔹 Висока якість завантаження."
    )
    await update.message.reply_text(help_text)

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.message.from_user.id)
    if ADMIN_ID and user_id != ADMIN_ID:
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return
        
    await update.message.reply_text("Перезавантажується... 🔄")
    import asyncio
    await asyncio.sleep(1)
    os.execv(sys.executable, ['python'] + sys.argv)

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text
    
    # Send a waiting message
    status_msg = await update.message.reply_text("⏳ Завантажую відео, зачекайте...")
    
    try:
        if "tiktok.com" in url:
            data = await fetch_tiktok_data(url)
            
            if data["type"] == "slideshow":
                media_group = []
                opened_files = []
                for i, photo_path in enumerate(data["media"]):
                    f = open(photo_path, 'rb')
                    opened_files.append(f)
                    media_group.append(InputMediaPhoto(f))
                
                try:
                    await update.message.reply_media_group(media=media_group, write_timeout=60, read_timeout=60, pool_timeout=60)
                finally:
                    for f in opened_files:
                        f.close()
                    for photo_path in data["media"]:
                        if os.path.exists(photo_path):
                            os.remove(photo_path)
                
                # Send audio if available
                if data.get("audio"):
                    with open(data["audio"], 'rb') as audio_file:
                        await update.message.reply_audio(audio=audio_file, write_timeout=60, read_timeout=60, pool_timeout=60)
                    if os.path.exists(data["audio"]):
                        os.remove(data["audio"])
            else:
                # Normal video from TikTok API
                video_path = data["media"][0]
                with open(video_path, 'rb') as video_file:
                    try:
                        await update.message.reply_video(
                            video=video_file,
                            write_timeout=60,
                            read_timeout=60,
                            pool_timeout=60
                        )
                    except Exception as vid_e:
                        logger.warning(f"Failed to send as video: {vid_e}. Trying as document...")
                        video_file.seek(0)
                        await update.message.reply_document(
                            document=video_file,
                            write_timeout=60,
                            read_timeout=60,
                            pool_timeout=60
                        )
                if os.path.exists(video_path):
                    os.remove(video_path)
                    
        elif "spotify" in url:
            # Download Spotify track via yt-dlp search
            track_name = await get_spotify_track_name(url)
            search_query = f"ytsearch1:{track_name} audio"
            file_path = await download_audio(search_query)
            
            with open(file_path, 'rb') as audio_file:
                await update.message.reply_audio(
                    audio=audio_file,
                    write_timeout=60,
                    read_timeout=60,
                    pool_timeout=60
                )
            if os.path.exists(file_path):
                os.remove(file_path)
                
        else:
            # Download video using yt-dlp for non-TikTok links
            file_path = await download_video(url)
            
            # Send video
            with open(file_path, 'rb') as video_file:
                try:
                    await update.message.reply_video(
                        video=video_file,
                        write_timeout=60,
                        read_timeout=60,
                        pool_timeout=60
                    )
                except Exception as vid_e:
                    logger.warning(f"Failed to send as video: {vid_e}. Trying as document...")
                    video_file.seek(0)
                    await update.message.reply_document(
                        document=video_file,
                        write_timeout=60,
                        read_timeout=60,
                        pool_timeout=60
                    )
            
            # Clean up the file after sending
            if os.path.exists(file_path):
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

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Запустити бота"),
        BotCommand("help", "Як користуватися та можливості"),
        BotCommand("restart", "Перезапустити бота (для адміна)"),
    ]
    await application.bot.set_my_commands(commands)
    
    # Send startup message to admin
    if ADMIN_ID:
        try:
            await application.bot.send_message(chat_id=ADMIN_ID, text="Бот успішно запущений і готовий до роботи! ✅")
        except Exception as e:
            logger.warning(f"Could not send startup message to admin: {e}")

def main() -> None:
    print("Starting bot...")
    
    # Start the dummy web server in a background thread for Render
    threading.Thread(target=run_dummy_server, daemon=True).start()

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("restart", restart_command))
    
    # Regex to match URLs
    url_pattern = re.compile(r'https?://[^\s]+')
    application.add_handler(MessageHandler(filters.Regex(url_pattern), handle_url))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
