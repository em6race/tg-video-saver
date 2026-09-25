import os
import time
import logging
import asyncio
import io
import aiohttp
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes

from downloader import download_video, download_audio
from tiktok_api import fetch_tiktok_data
from spotify_api import get_spotify_track_info
from database import get_cached_file_id, set_cached_file_id
from handlers.common import check_access

logger = logging.getLogger(__name__)

async def send_typing_action(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.constants import ChatAction
    while True:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(4)

async def clean_old_downloads():
    downloads_dir = "downloads"
    if not os.path.exists(downloads_dir):
        os.makedirs(downloads_dir, exist_ok=True)
        return

    current_time = time.time()
    for filename in os.listdir(downloads_dir):
        file_path = os.path.join(downloads_dir, filename)
        try:
            if os.path.isfile(file_path) and current_time - os.path.getmtime(file_path) > 3600:
                os.remove(file_path)
            elif os.path.isdir(file_path) and current_time - os.path.getmtime(file_path) > 3600:
                import shutil
                shutil.rmtree(file_path)
        except Exception as e:
            logger.warning(f"Could not delete old file {file_path}: {e}")

async def process_audio(target_message, context, url):
    cached_id = get_cached_file_id(url, "audio")
    if cached_id:
        try:
            await target_message.reply_audio(audio=cached_id)
            logger.info(f"Served audio from cache for {url}")
            return
        except Exception as e:
            logger.warning(f"Failed to use cached audio for {url}, redownloading: {e}")

    try:
        if "spotify" in url:
            track_info = await get_spotify_track_info(url)
            search_query = f"ytsearch1:{track_info['search_query']} Topic"
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
                msg = await target_message.reply_audio(
                    audio=audio_file,
                    title=track_info["title"],
                    performer=track_info["performer"],
                    thumbnail=thumb_file,
                    write_timeout=60,
                    read_timeout=60,
                    pool_timeout=60
                )
                if msg and msg.audio:
                    set_cached_file_id(url, "audio", msg.audio.file_id)
            finally:
                if thumb_file:
                    thumb_file.close()

        if os.path.exists(file_path):
            os.remove(file_path)
        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)

    except Exception as e:
        logger.error(f"Error in process_audio: {e}")
        await target_message.reply_text(f"❌ Сталася помилка при завантаженні аудіо.\n\nДеталі помилки (перешліть розробнику):\n`{str(e)}`", parse_mode="Markdown")

async def process_video(target_message, context, url):
    cached_id = get_cached_file_id(url, "video")
    if cached_id:
        try:
            await target_message.reply_video(video=cached_id)
            logger.info(f"Served video from cache for {url}")
            return
        except Exception as e:
            logger.warning(f"Failed to use cached video for {url}, redownloading: {e}")

    try:
        file_path = await download_video(url)
        with open(file_path, 'rb') as video_file:
            try:
                msg = await target_message.reply_video(
                    video=video_file,
                    write_timeout=60,
                    read_timeout=60,
                    pool_timeout=60
                )
                if msg and msg.video:
                    set_cached_file_id(url, "video", msg.video.file_id)
            except Exception as vid_e:
                logger.warning(f"Failed to send as video: {vid_e}. Trying as document...")
                video_file.seek(0)
                msg = await target_message.reply_document(
                    document=video_file,
                    write_timeout=60,
                    read_timeout=60,
                    pool_timeout=60
                )
                if msg and msg.document:
                    set_cached_file_id(url, "video", msg.document.file_id)
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.error(f"Error in process_video: {e}")
        await target_message.reply_text(f"❌ Сталася помилка при завантаженні відео.\n\nДеталі помилки (перешліть розробнику):\n`{str(e)}`", parse_mode="Markdown")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    url = update.message.text
    await clean_old_downloads()

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
            await update.message.reply_text("Вибачте, але я поки що не підтримую завантаження з цього сайту 😔\n\nЯ вмію працювати з: **TikTok, YouTube, Instagram, Twitter(X), Spotify та SoundCloud**.", parse_mode="Markdown")
            return

        if any(x in url.lower() for x in ["/album/", "/playlist/", "/artist/", "/podcast/", "/show/", "/episode/", "/user/"]) and any(domain in url.lower() for domain in ["spotify", "music.youtube", "soundcloud"]):
            await update.message.reply_text("Будь ласка, надішліть посилання на **окремий трек** (пісню), і я з радістю його завантажу! 🎵", parse_mode="Markdown")
            return

        if any(domain in url.lower() for domain in ["youtube.com", "youtu.be"]):
            keyboard = [
                [
                    InlineKeyboardButton("🎬 Відео", callback_data="yt_video"),
                    InlineKeyboardButton("🎵 Аудіо", callback_data="yt_audio"),
                ],
                [
                    InlineKeyboardButton("🎬+🎵 Відео та Аудіо", callback_data="yt_both")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Оберіть, що саме ви хочете завантажити з YouTube:", reply_markup=reply_markup, reply_to_message_id=update.message.message_id)
            return

        status_msg = await update.message.reply_text("⏳ Завантажую, зачекайте...")
        typing_task = asyncio.create_task(send_typing_action(update.message.chat_id, context))

        try:
            if "tiktok.com" in url:
                try:
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

                        if data.get("audio"):
                            with open(data["audio"], 'rb') as audio_file:
                                await update.message.reply_audio(audio=audio_file, write_timeout=60, read_timeout=60, pool_timeout=60)
                            if os.path.exists(data["audio"]):
                                os.remove(data["audio"])
                    else:
                        video_path = data["media"][0]
                        with open(video_path, 'rb') as video_file:
                            try:
                                await update.message.reply_video(video=video_file, write_timeout=60, read_timeout=60, pool_timeout=60)
                            except Exception as vid_e:
                                logger.warning(f"Failed to send as video: {vid_e}. Trying as document...")
                                video_file.seek(0)
                                await update.message.reply_document(document=video_file, write_timeout=60, read_timeout=60, pool_timeout=60)
                        if os.path.exists(video_path):
                            os.remove(video_path)

                        if data.get("audio"):
                            with open(data["audio"], 'rb') as audio_file:
                                await update.message.reply_audio(
                                    audio=audio_file,
                                    title=data.get("title", "TikTok Audio"),
                                    performer="TikTok",
                                    write_timeout=60, read_timeout=60, pool_timeout=60
                                )
                            if os.path.exists(data["audio"]):
                                os.remove(data["audio"])
                except Exception as tk_err:
                    logger.warning(f"TikTok API failed ({tk_err}). Falling back to yt-dlp...")
                    await process_video(update.message, context, url)

            elif any(domain in url for domain in ["spotify.com", "spotify.link", "spotify:", "music.youtube.com", "soundcloud.com"]):
                await process_audio(update.message, context, url)
            else:
                await process_video(update.message, context, url)
        finally:
            typing_task.cancel()
            try:
                await status_msg.delete()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Error handling URL: {e}")
        await update.message.reply_text(f"❌ Сталася помилка при завантаженні відео.\n\nДеталі помилки (перешліть розробнику):\n`{str(e)}`", parse_mode="Markdown")
