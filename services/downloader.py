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
        'format': 'bestaudio[ext=mp3]/bestaudio/best',
        'outtmpl': output_template,
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
    
    filename = None
    info = None

    # 1. Спроба первинного пошуку (YouTube або прямий URL)
    try:
        print(f"[DEBUG] Primary audio search/download: {query}")
        with YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(query, download=True)
            if 'entries' in res and len(res['entries']) == 0:
                raise Exception("No results found")
            info = res['entries'][0] if 'entries' in res else res
            
            cand_name = ydl.prepare_filename(info)
            base, _ = os.path.splitext(cand_name)
            mp3_filename = base + ".mp3"
            if os.path.exists(mp3_filename):
                filename = mp3_filename
            elif os.path.exists(cand_name):
                filename = cand_name
    except Exception as e:
        print(f"[DEBUG] Primary search failed: {e}")

    # 2. Якщо це був пошук на YouTube і він містив " Topic", пробуємо без " Topic"
    if not filename and "ytsearch" in query and " Topic" in query:
        fallback_query = query.replace(" Topic", "")
        try:
            print(f"[DEBUG] YouTube fallback search: {fallback_query}")
            with YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(fallback_query, download=True)
                if 'entries' in res and len(res['entries']) == 0:
                    raise Exception("No results on YouTube fallback")
                info = res['entries'][0] if 'entries' in res else res
                cand_name = ydl.prepare_filename(info)
                base, _ = os.path.splitext(cand_name)
                mp3_filename = base + ".mp3"
                if os.path.exists(mp3_filename):
                    filename = mp3_filename
                elif os.path.exists(cand_name):
                    filename = cand_name
        except Exception as fb_e:
            print(f"[DEBUG] YouTube fallback failed: {fb_e}")

    # 3. Надійний фолбек на SoundCloud з перебором кандидатів (автоматично оминає DRM/Go+ прев'ю)
    if not filename and ("ytsearch" in query or "scsearch" in query):
        import re
        clean_query = re.sub(r'^(ytsearch\d*:|scsearch\d*:)', '', query).replace(" Topic", "").strip()
        sc_search_query = f"scsearch5:{clean_query}"
        print(f"[DEBUG] Searching SoundCloud candidates: {sc_search_query}")
        
        search_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
        }
        sc_entries = []
        try:
            with YoutubeDL(search_opts) as ydl_s:
                sc_results = ydl_s.extract_info(sc_search_query, download=False)
                sc_entries = sc_results.get('entries', []) if sc_results else []
        except Exception as sc_s_err:
            print(f"[DEBUG] SoundCloud search failed: {sc_s_err}")

        for sc_idx, sc_entry in enumerate(sc_entries):
            sc_url = sc_entry.get('url')
            if not sc_url:
                continue
            print(f"[DEBUG] Trying SoundCloud candidate {sc_idx + 1}: {sc_entry.get('title')}")
            try:
                with YoutubeDL(ydl_opts) as ydl_sc:
                    cand_info = ydl_sc.extract_info(sc_url, download=True)
                    cand_name = ydl_sc.prepare_filename(cand_info)
                    base, _ = os.path.splitext(cand_name)
                    mp3_filename = base + ".mp3"
                    if os.path.exists(mp3_filename):
                        filename = mp3_mp3 = mp3_filename
                    elif os.path.exists(cand_name):
                        filename = cand_name
                    if filename and os.path.exists(filename):
                        info = cand_info
                        print(f"[DEBUG] Successfully downloaded SoundCloud candidate: {info.get('title')}")
                        break
            except Exception as cand_err:
                print(f"[DEBUG] SoundCloud candidate {sc_idx + 1} failed: {cand_err}")
                continue

    if not filename or not os.path.exists(filename):
        import glob
        files = glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}.*"))
        if files:
            filename = files[0]

    if not filename or not os.path.exists(filename):
        raise Exception("Не вдалося завантажити аудіо (спроби через YouTube та SoundCloud завершилися невдачею).")
        
    final_path = filename
        
    thumbnail = None
    if info and info.get('thumbnails'):
        thumbnail = info['thumbnails'][-1]['url']
    elif info and info.get('thumbnail'):
        thumbnail = info['thumbnail']
        
    performer = (info.get('artist') or info.get('uploader') or info.get('creator') or 'Unknown Artist') if info else 'Unknown Artist'
    title = (info.get('title') or 'Unknown Title') if info else 'Unknown Title'
        
    return {
        'file_path': final_path,
        'title': title,
        'performer': performer,
        'thumbnail_url': thumbnail
    }

async def download_audio(query: str) -> dict:
    """
    Downloads audio from the given query (URL or ytsearch).
    """
    return await asyncio.to_thread(_download_audio_sync, query)
