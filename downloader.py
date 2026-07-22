import os
import uuid
import asyncio
from yt_dlp import YoutubeDL

DOWNLOADS_DIR = "downloads"

# Ensure downloads directory exists
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

def _download_video_sync(url: str) -> str:
    # Generate unique filename
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOADS_DIR, f"{file_id}.%(ext)s")
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
    }
    
    with YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=True)
        # yt-dlp might change the extension during merge, get the actual filename
        filename = ydl.prepare_filename(info_dict)
        
        # If the file extension was changed during merging (e.g., to .mp4)
        if not os.path.exists(filename):
            base, _ = os.path.splitext(filename)
            mp4_filename = base + ".mp4"
            if os.path.exists(mp4_filename):
                filename = mp4_filename
                
        return filename

async def download_video(url: str) -> str:
    """
    Downloads a video from the given URL and returns the path to the downloaded file.
    Runs synchronously but wrapped to not block the event loop.
    """
    return await asyncio.to_thread(_download_video_sync, url)

def _download_audio_sync(query: str) -> dict:
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOADS_DIR, f"{file_id}.%(ext)s")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=True)
        
    thumbnail = None
    if info.get('thumbnails'):
        thumbnail = info['thumbnails'][-1]['url']
    elif info.get('thumbnail'):
        thumbnail = info['thumbnail']
        
    performer = info.get('artist') or info.get('uploader') or info.get('creator') or 'Unknown Artist'
        
    return {
        'file_path': os.path.join(DOWNLOADS_DIR, f"{file_id}.mp3"),
        'title': info.get('title', 'Unknown Title'),
        'performer': performer,
        'thumbnail_url': thumbnail
    }

async def download_audio(query: str) -> dict:
    """
    Downloads audio from the given query (URL or ytsearch).
    """
    return await asyncio.to_thread(_download_audio_sync, query)
