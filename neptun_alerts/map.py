import os
import time
import math
import aiohttp
import logging
from datetime import datetime
import pytz
from PIL import Image, ImageDraw

from .geocoding import _normalize_apostrophe

logger = logging.getLogger(__name__)

NEPTUN_OBLASTS_GEOJSON_URL = "https://neptun.in.ua/oblasts.geojson"
NEPTUN_RAIONS_GEOJSON_URL = "https://neptun.in.ua/raions.geojson"

_GEOJSON_CACHE = {
    "oblasts": None,
    "raions": None
}

async def _ensure_geojson_loaded():
    """Завантажує контури областей та районів України з NEPTUN при першому виклику."""
    if _GEOJSON_CACHE["oblasts"] and _GEOJSON_CACHE["raions"]:
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TGAlertBot/1.0; +https://neptun.in.ua)"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        if not _GEOJSON_CACHE["oblasts"]:
            async with session.get(NEPTUN_OBLASTS_GEOJSON_URL, timeout=15) as r1:
                if r1.status == 200:
                    _GEOJSON_CACHE["oblasts"] = await r1.json()
        if not _GEOJSON_CACHE["raions"]:
            async with session.get(NEPTUN_RAIONS_GEOJSON_URL, timeout=15) as r2:
                if r2.status == 200:
                    _GEOJSON_CACHE["raions"] = await r2.json()

async def generate_alerts_map_image(alerts_data: dict, threats_data: dict, user_sub: dict = None) -> tuple[str, str]:
    """
    Генерує актуальне зображення карти повітряних тривог та цілей України у стилі NEPTUN.
    Повертає (шлях_до_файлу_png, текстовий_опис_карти).
    """
    await _ensure_geojson_loaded()
    oblasts_geo = _GEOJSON_CACHE["oblasts"] or {}
    raions_geo = _GEOJSON_CACHE["raions"] or {}

    width, height = 1440, 960
    pad_x, pad_y = 55, 65
    min_lon, max_lon = 22.05, 40.30
    min_lat, max_lat = 44.25, 52.45

    def project(lon: float, lat: float) -> tuple[float, float]:
        x = pad_x + (lon - min_lon) / (max_lon - min_lon) * (width - 2 * pad_x)
        y = height - pad_y - (lat - min_lat) / (max_lat - min_lat) * (height - 2 * pad_y)
        return (x, y)

    img = Image.new("RGB", (width, height), color=(14, 19, 27))
    draw = ImageDraw.Draw(img)

    raions_list = alerts_data.get("raions", [])
    oblasts_list = alerts_data.get("oblasts", [])

    red_raions = set(_normalize_apostrophe(r.get("key", "")).lower() for r in raions_list if r.get("level") == "red")
    yellow_raions = set(_normalize_apostrophe(r.get("key", "")).lower() for r in raions_list if r.get("level") == "yellow")

    red_oblasts = set(_normalize_apostrophe(o.get("key", "")).lower() for o in oblasts_list if o.get("level") == "red")
    yellow_oblasts = set(_normalize_apostrophe(o.get("key", "")).lower() for o in oblasts_list if o.get("level") == "yellow")

    # 1. Спочатку замальовуємо всі області базовим кольором (або червоним/жовтим, якщо вся область у тривозі)
    for feat in oblasts_geo.get("features", []):
        props = feat.get("properties", {})
        ob_key = _normalize_apostrophe(props.get("key", "")).lower()
        fill_col = (26, 35, 48)
        out_col = (42, 55, 76)
        if ob_key in red_oblasts:
            fill_col = (180, 36, 42)
            out_col = (245, 75, 75)
        elif ob_key in yellow_oblasts:
            fill_col = (205, 145, 20)
            out_col = (245, 185, 45)

        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        polys = coords if gtype == "MultiPolygon" else [coords] if gtype == "Polygon" else []
        for poly in polys:
            if poly and poly[0]:
                pts = [project(pt[0], pt[1]) for pt in poly[0]]
                if len(pts) >= 3:
                    draw.polygon(pts, fill=fill_col, outline=out_col)

    # 2. Малюємо всі райони (raions.geojson) з їхніми актуальними рівнями (red / yellow)
    for feat in raions_geo.get("features", []):
        props = feat.get("properties", {})
        r_key = _normalize_apostrophe(props.get("key", "")).lower()

        if r_key in red_raions:
            fill_col = (188, 38, 45)
            out_col = (255, 85, 85)
        elif r_key in yellow_raions:
            fill_col = (214, 152, 22)
            out_col = (248, 190, 50)
        else:
            # Якщо район спокійний, малюємо лише тонку сітку району
            fill_col = None
            out_col = (38, 50, 68)

        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        polys = coords if gtype == "MultiPolygon" else [coords] if gtype == "Polygon" else []
        for poly in polys:
            if poly and poly[0]:
                pts = [project(pt[0], pt[1]) for pt in poly[0]]
                if len(pts) >= 3:
                    if fill_col:
                        draw.polygon(pts, fill=fill_col, outline=out_col)
                    else:
                        draw.line(pts, fill=out_col, width=1)

    # 3. Поверх районів малюємо чіткі межі областей
    for feat in oblasts_geo.get("features", []):
        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        polys = coords if gtype == "MultiPolygon" else [coords] if gtype == "Polygon" else []
        for poly in polys:
            if poly and poly[0]:
                pts = [project(pt[0], pt[1]) for pt in poly[0]]
                if len(pts) >= 3:
                    draw.line(pts, fill=(88, 112, 148), width=2)

    # 4. Малюємо активні повітряні цілі (threats) та вектори їх руху
    active_threats_count = 0
    for t in threats_data.get("threats", []):
        if t.get("status") != "active":
            continue
        active_threats_count += 1
        t_lat = t.get("lat")
        t_lon = t.get("lon")
        if t_lat is None or t_lon is None or t.get("areaOnly"):
            continue

        tx, ty = project(t_lon, t_lat)
        t_type = t.get("type", "uav")

        # Колір маркера цілі: ракети/балістика — яскраво-пурпуровий/білий, БпЛА — помаранчево-жовтий
        raw_t_title = (t.get("explanationShort") or t.get("title") or "").lower()
        if t_type in ("missile", "ballistic", "kab", "mig31k", "jet_uav") or "реактивн" in raw_t_title:
            marker_col = (255, 50, 220)
        elif t_type == "recon":
            marker_col = (80, 200, 255)
        else:
            marker_col = (255, 210, 40)

        # Зовнішній ореол та точка
        r_outer = 9
        r_inner = 5
        draw.ellipse((tx - r_outer, ty - r_outer, tx + r_outer, ty + r_outer), outline=marker_col, width=2)
        draw.ellipse((tx - r_inner, ty - r_inner, tx + r_inner, ty + r_inner), fill=marker_col)

        # Напрямок польоту (стрілка за курсом heading)
        heading = t.get("heading")
        if heading is not None:
            rad = math.radians(float(heading))
            arrow_len = 24
            ex = tx + arrow_len * math.sin(rad)
            ey = ty - arrow_len * math.cos(rad)
            draw.line([(tx, ty), (ex, ey)], fill=marker_col, width=3)

    # 5. Якщо у користувача збережені власні координати — малюємо його точку (блакитно-зелений пін)
    if user_sub and user_sub.get("lat") is not None and user_sub.get("lon") is not None:
        ux, uy = project(user_sub["lon"], user_sub["lat"])
        draw.ellipse((ux - 11, uy - 11, ux + 11, uy + 11), fill=(0, 220, 130), outline=(255, 255, 255), width=3)
        draw.ellipse((ux - 4, uy - 4, ux + 4, uy + 4), fill=(255, 255, 255))

    # 6. Кольорові індикатори легенди внизу зліва
    legend_items = [
        ((188, 38, 45), (255, 85, 85)),   # Червоний
        ((214, 152, 22), (248, 190, 50)), # Жовтий
        ((26, 35, 48), (88, 112, 148)),   # Спокійно
        ((255, 210, 40), (255, 210, 40)), # Цілі
    ]
    lx = 40
    ly = height - 45
    for idx, (f_c, o_c) in enumerate(legend_items):
        bx = lx + idx * 42
        draw.rectangle((bx, ly, bx + 26, ly + 18), fill=f_c, outline=o_c, width=2)

    os.makedirs("downloads", exist_ok=True)
    map_path = f"downloads/neptun_map_{int(time.time())}.png"
    img.save(map_path, "PNG")

    # Підготовка текстового опису до карти
    tz = pytz.timezone("Europe/Kiev")
    now_str = datetime.now(tz).strftime("%H:%M:%S (%d.%m.%Y)")
    red_count = len(red_raions) + len(red_oblasts)
    yellow_count = len(yellow_raions) + len(yellow_oblasts)

    caption_lines = [
        f"🗺 **Карта повітряних тривог України**",
        f"🕒 Станом на: **{now_str}**\n",
        f"🔴 Червоний рівень (тривога): **{red_count}** р-н/обл.",
        f"🟡 Жовтий рівень (підвищена небезпека): **{yellow_count}** р-н/обл.",
        f"🛸 Повітряних цілей у небі: **{active_threats_count}**"
    ]
    if user_sub and user_sub.get("lat") is not None:
        caption_lines.append("🟢 *Ваша геопозиція позначена зелено-білою точкою на карті*")

    caption_lines.append("\n🔗 [Дані: Карта повітряних тривог — NEPTUN](https://neptun.in.ua/)")
    return map_path, "\n".join(caption_lines)
