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

import platform
import subprocess
import time
import signal

def convert_to_mp3_throttled(input_path: str, output_path: str):
    cmd = ["ffmpeg", "-y", "-i", input_path, "-threads", "1", "-q:a", "2", output_path]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    if platform.system() != "Windows":
        while proc.poll() is None:
            try:
                proc.send_signal(signal.SIGSTOP)
                time.sleep(0.15)
                proc.send_signal(signal.SIGCONT)
                time.sleep(0.01)
            except Exception:
                time.sleep(0.05)
    else:
        proc.wait()
        
    if proc.returncode != 0:
        # If it fails (e.g. ffmpeg not found), just return the original file
        return input_path
    
    # Delete original file
    try:
        os.remove(input_path)
    except Exception:
        pass
        
    return output_path

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
    }
    
    try:
        print(f"[DEBUG] Searching YouTube (primary): {query}")
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if 'entries' in info and len(info['entries']) == 0:
                raise Exception("No results found on YouTube")
            filename = ydl.prepare_filename(info)
    except Exception as e:
        print(f"[DEBUG] Primary search failed: {e}")
        if "ytsearch" in query:
            filename = None
            # If we used "Topic", fallback to youtube without "Topic" first
            if " Topic" in query and "No results found" in str(e):
                fallback_query = query.replace(" Topic", "")
                try:
                    print(f"[DEBUG] Searching YouTube (fallback): {fallback_query}")
                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(fallback_query, download=True)
                        if 'entries' in info and len(info['entries']) == 0:
                            raise Exception("No results")
                        filename = ydl.prepare_filename(info)
                except Exception as fb_e:
                    print(f"[DEBUG] Fallback YouTube search failed: {fb_e}")
            
            # If we still don't have a filename (either it wasn't a Topic search, or the Topic fallback failed)
            # We fallback to SoundCloud
            if not filename:
                sc_query = query.replace("ytsearch", "scsearch").replace(" Topic", "")
                print(f"[DEBUG] Searching SoundCloud (fallback): {sc_query}")
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(sc_query, download=True)
                    if 'entries' in info and len(info['entries']) == 0:
                        raise Exception("No results found on soundcloud")
                    filename = ydl.prepare_filename(info)
        else:
            raise e
            
    if not os.path.exists(filename):
        import glob
        files = glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}.*"))
        if files:
            filename = files[0]
            
    # Convert to MP3 safely
    mp3_path = os.path.join(DOWNLOADS_DIR, f"{file_id}.mp3")
    final_path = convert_to_mp3_throttled(filename, mp3_path)
        
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
