import aiohttp
import asyncio
import os
import time
import uuid

async def fetch_tiktok_data(url: str):
    """
    Fetches TikTok video or slideshow using tikwm API.
    Returns:
        dict: {
            "type": "video" | "slideshow",
            "media": [list of file paths],
            "audio": audio_file_path or None,
            "title": str
        }
    """
    api_url = "https://www.tikwm.com/api/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    }
    
    async with aiohttp.ClientSession(headers=headers) as session:
        # Automatically resolve shortlinks to full URLs to prevent tikwm parsing errors
        if "vt.tiktok.com" in url or "vm.tiktok.com" in url or "t.tiktok.com" in url:
            try:
                async with session.head(url, allow_redirects=True, timeout=10) as head_resp:
                    url = str(head_resp.url).split("?")[0]
            except Exception:
                pass

        async with session.get(api_url, params={"url": url}) as resp:
            data = await resp.json()
            
            if data.get("code") != 0:
                raise Exception(f"TikTok API Error: {data.get('msg', 'Unknown error')}")
                
            post_data = data.get("data", {})
            title = post_data.get("title", "TikTok Video")
            audio_url = post_data.get("music")
            
            downloads_dir = "downloads"
            os.makedirs(downloads_dir, exist_ok=True)
            
            # Download audio
            audio_path = None
            if audio_url:
                audio_path = os.path.join(downloads_dir, f"tiktok_audio_{int(time.time())}.mp3")
                async with session.get(audio_url) as a_resp:
                    with open(audio_path, 'wb') as f:
                        f.write(await a_resp.read())
            
            # Check if it's a slideshow
            if "images" in post_data and post_data["images"]:
                req_id = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
                async def _download_img(idx: int, img_u: str) -> str:
                    img_p = os.path.join(downloads_dir, f"tiktok_img_{req_id}_{idx}.jpg")
                    async with session.get(img_u) as i_resp:
                        content = await i_resp.read()
                        with open(img_p, 'wb') as f:
                            f.write(content)
                    return img_p

                media_paths = await asyncio.gather(*[_download_img(i, u) for i, u in enumerate(post_data["images"])])
                    
                return {
                    "type": "slideshow",
                    "media": list(media_paths),
                    "audio": audio_path,
                    "title": title
                }
            else:
                # Normal video
                video_url = post_data.get("play")
                if not video_url:
                    raise Exception("No video URL found")
                    
                video_path = os.path.join(downloads_dir, f"tiktok_video_{int(time.time())}.mp4")
                async with session.get(video_url) as v_resp:
                    with open(video_path, 'wb') as f:
                        f.write(await v_resp.read())
                        
                return {
                    "type": "video",
                    "media": [video_path],
                    "audio": audio_path,
                    "title": title
                }
