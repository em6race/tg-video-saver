import aiohttp
import re

async def get_spotify_track_name(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            html = await resp.text()
            match = re.search(r'<title>(.*?)</title>', html)
            if match:
                title = match.group(1)
                # Usually: "Song Name - song and lyrics by Artist | Spotify"
                clean = title.split(" - song and lyrics by ")[0]
                if " | Spotify" in title:
                    clean = clean.split(" | Spotify")[0]
                return clean
            return "Spotify Track"
