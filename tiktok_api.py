import aiohttp
import os
import time

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
                media_paths = []
                # Limit to 10 images for Telegram MediaGroup limits
                for i, img_url in enumerate(post_data["images"][:10]): 
                    img_path = os.path.join(downloads_dir, f"tiktok_img_{int(time.time())}_{i}.jpg")
                    async with session.get(img_url) as i_resp:
                        with open(img_path, 'wb') as f:
                            f.write(await i_resp.read())
                    media_paths.append(img_path)
                    
                return {
                    "type": "slideshow",
                    "media": media_paths,
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
