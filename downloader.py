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
        'postprocessor_args': ['-threads', '1'],
        'extractor_args': {'youtube': {'player_client': ['android']}},
        'concurrent_fragment_downloads': 10,
    }
    
    import shutil
    if shutil.which("aria2c"):
        ydl_opts['external_downloader'] = 'aria2c'
        ydl_opts['external_downloader_args'] = ['-x', '10', '-s', '10', '-k', '1M']
    
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

import platform
import subprocess
import time
import signal

def _download_audio_sync(query: str) -> dict:
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOADS_DIR, f"{file_id}.%(ext)s")
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'extractor_args': {'youtube': {'player_client': ['android']}},
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'postprocessor_args': ['-threads', '1'],
        'concurrent_fragment_downloads': 10,
    }

    import shutil
    if shutil.which("aria2c"):
        ydl_opts['external_downloader'] = 'aria2c'
        ydl_opts['external_downloader_args'] = ['-x', '10', '-s', '10', '-k', '1M']
    
    try:
        print(f"[DEBUG] Searching YouTube (primary): {query}")
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if 'entries' in info and len(info['entries']) == 0:
                raise Exception("No results found on YouTube")
            
            # yt-dlp changes the extension to .mp3 after processing
            filename = ydl.prepare_filename(info)
            base, _ = os.path.splitext(filename)
            mp3_filename = base + ".mp3"
            
            if os.path.exists(mp3_filename):
                filename = mp3_filename
    except Exception as e:
        print(f"[DEBUG] Primary search failed: {e}")
        if "ytsearch" in query:
            filename = None
            if " Topic" in query and "No results found" in str(e):
                fallback_query = query.replace(" Topic", "")
                try:
                    print(f"[DEBUG] Searching YouTube (fallback): {fallback_query}")
                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(fallback_query, download=True)
                        if 'entries' in info and len(info['entries']) == 0:
                            raise Exception("No results")
                        filename = ydl.prepare_filename(info)
                        base, _ = os.path.splitext(filename)
                        mp3_filename = base + ".mp3"
                        if os.path.exists(mp3_filename):
                            filename = mp3_filename
                except Exception as fb_e:
                    print(f"[DEBUG] Fallback YouTube search failed: {fb_e}")
            
            if not filename:
                sc_query = query.replace("ytsearch", "scsearch").replace(" Topic", "")
                print(f"[DEBUG] Searching SoundCloud (fallback): {sc_query}")
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(sc_query, download=True)
                    if 'entries' in info and len(info['entries']) == 0:
                        raise Exception("No results found on soundcloud")
                    filename = ydl.prepare_filename(info)
                    base, _ = os.path.splitext(filename)
                    mp3_filename = base + ".mp3"
                    if os.path.exists(mp3_filename):
                        filename = mp3_filename
        else:
            raise e
            
    if not os.path.exists(filename):
        import glob
        files = glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}.*"))
        if files:
            filename = files[0]
            
    final_path = filename
        
    thumbnail = None
    if info.get('thumbnails'):
        thumbnail = info['thumbnails'][-1]['url']
    elif info.get('thumbnail'):
        thumbnail = info['thumbnail']
        
    performer = info.get('artist') or info.get('uploader') or info.get('creator') or 'Unknown Artist'
        
    return {
        'file_path': final_path,
        'title': info.get('title', 'Unknown Title'),
        'performer': performer,
        'thumbnail_url': thumbnail
    }

async def download_audio(query: str) -> dict:
    """
    Downloads audio from the given query (URL or ytsearch).
    """
    return await asyncio.to_thread(_download_audio_sync, query)
