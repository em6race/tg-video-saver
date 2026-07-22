import aiohttp
import re

async def get_spotify_track_info(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    }
    
    result = {
        "title": "Spotify Track",
        "performer": "Unknown Artist",
        "thumbnail_url": None,
        "search_query": "Spotify Track"
    }
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as resp:
                html = await resp.text()
                
                title_m = re.search(r'<meta property="og:title" content="(.*?)"', html)
                desc_m = re.search(r'<meta property="og:description" content="(.*?)"', html)
                img_m = re.search(r'<meta property="og:image" content="(.*?)"', html)
                
                if title_m:
                    result["title"] = title_m.group(1)
                    
                if desc_m:
                    desc = desc_m.group(1)
                    # Split by '·' (which is often represented as \xb7 or similar)
                    parts = desc.split('·')
                    if len(parts) > 0:
                        result["performer"] = parts[0].strip()
                        
                if img_m:
                    result["thumbnail_url"] = img_m.group(1)
                    
                result["search_query"] = f'{result["title"]} {result["performer"]}'
    except Exception as e:
        print(f"Error extracting spotify info: {e}")
        
    return result
