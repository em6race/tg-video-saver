import aiohttp
import re
import html
import urllib.parse

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
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        html_content = await resp.text()
                        
                        title_m = re.search(r'<meta property="og:title" content="(.*?)"', html_content)
                        desc_m = re.search(r'<meta property="og:description" content="(.*?)"', html_content)
                        img_m = re.search(r'<meta property="og:image" content="(.*?)"', html_content)
                        
                        if title_m:
                            result["title"] = html.unescape(title_m.group(1))
                            
                        if desc_m:
                            desc = html.unescape(desc_m.group(1))
                            parts = desc.split('·')
                            if len(parts) > 0:
                                result["performer"] = parts[0].strip()
                                
                        if img_m:
                            result["thumbnail_url"] = img_m.group(1)
            except Exception as get_err:
                print(f"Error fetching spotify html: {get_err}")

            if result["title"] == "Spotify Track" or not result["thumbnail_url"]:
                try:
                    oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url, safe='')}"
                    async with session.get(oembed_url, timeout=aiohttp.ClientTimeout(total=10)) as oembed_resp:
                        if oembed_resp.status == 200:
                            data = await oembed_resp.json()
                            oembed_title = data.get("title", "")
                            if oembed_title:
                                if " - " in oembed_title:
                                    parts = oembed_title.split(" - ", 1)
                                    if result["performer"] == "Unknown Artist":
                                        result["performer"] = parts[0].strip()
                                    if result["title"] == "Spotify Track":
                                        result["title"] = parts[1].strip()
                                else:
                                    if result["title"] == "Spotify Track":
                                        result["title"] = oembed_title.strip()
                            if not result["thumbnail_url"] and data.get("thumbnail_url"):
                                result["thumbnail_url"] = data.get("thumbnail_url")
                except Exception as oe_err:
                    print(f"Error fetching spotify oembed: {oe_err}")

            title_clean = re.sub(r'\(feat\..*?\)', '', result["title"], flags=re.IGNORECASE).strip()
            performer_clean = result["performer"].split(',')[0].strip() if result["performer"] != "Unknown Artist" else ""
            result["search_query"] = f'{title_clean} {performer_clean}'.strip()
    except Exception as e:
        print(f"Error extracting spotify info: {e}")
        
    return result

