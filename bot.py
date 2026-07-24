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
from spotify_api import get_spotify_track_info
import pytz
from datetime import datetime
import subprocess
import time
import aiohttp
from PIL import Image
import io

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
        "👋 *Що я вмію?*\n\n"
        "Я універсальний бот для завантаження медіа! Просто надішліть мені посилання, і я зроблю все інше.\n\n"
        "🎥 *Відео та Фото*\n"
        "• *TikTok*: Завантажую відео без водяного знаку + окремо відправляю музику з нього!\n"
        "• *TikTok Слайди*: Завантажую всі фотографії зі слайдшоу + музику.\n"
        "• *YouTube, Instagram (Reels), Twitter (X)* та інші: Завантажую відео у найкращій якості.\n\n"
        "🎵 *Музика*\n"
        "• *Spotify, SoundCloud, YouTube Music*: Просто киньте лінк на трек, і я відправлю вам повноцінний аудіофайл (MP3) з правильною назвою, виконавцем та красивою обкладинкою!\n\n"
        "🛠 *Додатково*\n"
        "• Якщо щось не завантажується, просто перевірте посилання.\n\n"
        "🚀 *Чекаю на ваші лінки!*"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.message.from_user.id)
    if ADMIN_ID and user_id != ADMIN_ID:
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return
        
    await update.message.reply_text("⏳ Завантажую оновлення з GitHub і перезавантажуюсь... 🔄")
    import asyncio
    await asyncio.sleep(1)
    
    import subprocess
    try:
        subprocess.run(["git", "pull"], check=True)
    except Exception as e:
        logger.error(f"Git pull failed during restart: {e}")
        
    os.execv(sys.executable, ['python'] + sys.argv)

async def send_typing_action(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.constants import ChatAction
    while True:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            import asyncio
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            break
        except Exception:
            import asyncio
            await asyncio.sleep(4)

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text
    
    # Send a waiting message
    status_msg = await update.message.reply_text("⏳ Завантажую, зачекайте...")
    
    import asyncio
    typing_task = asyncio.create_task(send_typing_action(update.message.chat_id, context))
    
    try:
        supported_domains = [
            "tiktok.com",
            "youtube.com", "youtu.be",
            "instagram.com",
            "twitter.com", "x.com",
            "spotify.com", "spotify.link", "spotify:",
            "music.youtube.com",
            "soundcloud.com"
        ]
        
        if not any(domain in url.lower() for domain in supported_domains):
            await update.message.reply_text("Вибачте, але я поки що не підтримую завантаження з цього сайту 😔\n\nЯ вмію працювати з: **TikTok, YouTube, Instagram, Twitter(X), Spotify та SoundCloud**. Спробуйте надіслати посилання з цих платформ!", parse_mode="Markdown")
            return
            
        if any(x in url.lower() for x in ["/album/", "/playlist/", "/artist/", "/podcast/", "/show/", "/episode/", "/user/"]) and any(domain in url.lower() for domain in ["spotify", "music.youtube", "soundcloud"]):
            await update.message.reply_text("Вибачте, але я поки що не вмію завантажувати цілі альбоми, плейлисти чи подкасти 😔\n\nБудь ласка, надішліть посилання на **окремий трек** (пісню), і я з радістю його завантажу! 🎵", parse_mode="Markdown")
            return

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
                    
                # Send audio if available
                if data.get("audio"):
                    with open(data["audio"], 'rb') as audio_file:
                        await update.message.reply_audio(
                            audio=audio_file,
                            title=data.get("title", "TikTok Audio"),
                            performer="TikTok",
                            write_timeout=60, 
                            read_timeout=60, 
                            pool_timeout=60
                        )
                    if os.path.exists(data["audio"]):
                        os.remove(data["audio"])
                    
        elif any(domain in url for domain in ["spotify.com", "spotify.link", "spotify:", "music.youtube.com", "soundcloud.com"]):
            # For Spotify, we fetch metadata first then search YT. 
            # For YT Music / SoundCloud, we pass the URL directly to yt-dlp.
            if "spotify" in url:
                track_info = await get_spotify_track_info(url)
                search_query = f"ytsearch1:{track_info['search_query']} audio"
                audio_result = await download_audio(search_query)
            else:
                audio_result = await download_audio(url)
                track_info = {
                    "title": audio_result["title"],
                    "performer": audio_result["performer"],
                    "thumbnail_url": audio_result["thumbnail_url"]
                }
                
            file_path = audio_result["file_path"]
            
            thumb_path = None
            if track_info.get("thumbnail_url"):
                thumb_path = f"downloads/thumb_{int(time.time())}.jpg"
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(track_info["thumbnail_url"]) as resp:
                            if resp.status == 200:
                                img_bytes = await resp.read()
                                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                                img.thumbnail((320, 320), Image.Resampling.LANCZOS)
                                img.save(thumb_path, "JPEG", quality=90)
                except Exception as e:
                    logger.warning(f"Failed to download thumbnail: {e}")
                    thumb_path = None
            
            with open(file_path, 'rb') as audio_file:
                thumb_file = open(thumb_path, 'rb') if thumb_path and os.path.exists(thumb_path) else None
                try:
                    await update.message.reply_audio(
                        audio=audio_file,
                        title=track_info["title"],
                        performer=track_info["performer"],
                        thumbnail=thumb_file,
                        write_timeout=60,
                        read_timeout=60,
                        pool_timeout=60
                    )
                finally:
                    if thumb_file:
                        thumb_file.close()
                        
            if os.path.exists(file_path):
                os.remove(file_path)
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)
                
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
        
    except Exception as e:
        logger.error(f"Error handling URL: {e}")
        await update.message.reply_text(f"❌ Сталася помилка при завантаженні відео.\n\nДеталі помилки (перешліть розробнику):\n`{str(e)}`", parse_mode="Markdown")
        
    finally:
        typing_task.cancel()
        try:
            await status_msg.delete()
        except Exception:
            pass

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
            await application.bot.send_message(chat_id=int(ADMIN_ID), text="Бот успішно запущений і готовий до роботи! ✅")
        except Exception as e:
            logger.warning(f"Could not send startup message to admin: {e}")
            
    # Start auto-update task
    import asyncio
    asyncio.create_task(auto_update(application))

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

async def auto_update(application: Application) -> None:
    tz = pytz.timezone('Europe/Kiev')
    import asyncio
    while True:
        await asyncio.sleep(60)
        now = datetime.now(tz)
        if now.hour == 3 and now.minute == 0:
            if ADMIN_ID:
                try:
                    await application.bot.send_message(chat_id=int(ADMIN_ID), text="🕒 3:00 ночі. Завантажую оновлення з GitHub і перезапускаюсь... 🔄")
                except Exception:
                    pass
            
            try:
                subprocess.run(["git", "pull"], check=True)
            except Exception as e:
                logger.error(f"Git pull failed: {e}")
                
            await asyncio.sleep(2)
            os.execv(sys.executable, ['python'] + sys.argv)

if __name__ == "__main__":
    main()
