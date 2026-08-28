import asyncio
import logging
import os
import signal
import sqlite3
import math
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, Location
)
import aiohttp
from aiohttp import web

# ====================== НАСТРОЙКИ ======================
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не встановлено! Додайте змінну оточення BOT_TOKEN.")

GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "-1004434293069"))
GROUP_URL = os.getenv("GROUP_URL", "https://t.me/+rKxYkNg85aAwNzFi")

REGIONS = {
    "Дніпропетровська": {"lat": 48.4647, "lon": 35.0462},
    "Київська": {"lat": 50.4501, "lon": 30.5234},
    "Полтавська": {"lat": 49.5895, "lon": 34.5514},
    "Запорізька": {"lat": 47.8388, "lon": 35.1396},
    "Черкаська": {"lat": 49.4444, "lon": 32.0598},
}

FISH_LIST = ["Лящ", "Карась", "Короп", "Щука", "Окунь", "Сом", "Плотва"]
PREDATOR_FISH = ["Щука", "Окунь", "Сом"]

# ====================== КЭШ И RATE-LIMIT ======================
weather_cache = OrderedDict()
CACHE_MAX_ENTRIES = 20
CACHE_TTL = 12 * 60 * 60          # 12 годин
RATE_LIMIT_UNTIL = 0

# Семафор для сериализации запросов к Open-Meteo
weather_semaphore = asyncio.Semaphore(1)
# Время последнего запроса для минимального интервала
last_weather_request_time = 0
MIN_REQUEST_INTERVAL = 2.0  # секунды

# Кэш геокодирования
geocode_cache = OrderedDict()
GEOCODE_CACHE_TTL = 30 * 24 * 60 * 60  # 30 днів

class ForecastStates(StatesGroup):
    choosing_region = State()
    choosing_fish = State()
    choosing_day = State()
    choosing_hour = State()

# ====================== БАЗА ДАННЫХ ======================
def _init_db_sync():
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            region TEXT,
            fish_type TEXT,
            forecast_day TEXT,
            hour INTEGER,
            pressure REAL,
            wind REAL,
            temp REAL,
            stars INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            forecast_id INTEGER,
            rating TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cursor.execute("ALTER TABLE forecasts ADD COLUMN hour INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def _save_forecast_sync(user_id, region, fish_type, forecast_day, hour,
                        pressure, wind, temp, stars):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO forecasts (user_id, region, fish_type, forecast_day, hour,
                               pressure, wind, temp, stars)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, region, fish_type, forecast_day, hour, pressure, wind, temp, stars))
    forecast_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return forecast_id

def _save_feedback_sync(user_id, forecast_id, rating):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO feedback (user_id, forecast_id, rating) VALUES (?, ?, ?)",
        (user_id, forecast_id, rating)
    )
    conn.commit()
    conn.close()

def _get_history_sync(user_id, limit=5):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT region, fish_type, forecast_day, hour, stars, timestamp
        FROM forecasts WHERE user_id = ? ORDER BY id DESC LIMIT ?
    """, (user_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows

async def init_db():
    await asyncio.to_thread(_init_db_sync)

async def save_forecast_to_db(*args):
    return await asyncio.to_thread(_save_forecast_sync, *args)

async def save_feedback_to_db(*args):
    await asyncio.to_thread(_save_feedback_sync, *args)

async def get_user_history_from_db(user_id, limit=5):
    return await asyncio.to_thread(_get_history_sync, user_id, limit)

# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c

def get_wind_direction_text(degrees) -> str:
    if degrees is None:
        return "Н/Д"
    directions = ["Пн", "Пн-Сх", "Сх", "Пд-Сх", "Пд", "Пд-Зх", "Зх", "Пн-Зх"]
    return directions[round(degrees / 45) % 8]

def get_moon_phase_info(date_obj: datetime) -> Tuple[str, int]:
    known_new_moon = datetime(2024, 1, 11)
    phase_days = (date_obj - known_new_moon).days % 29.53
    if phase_days < 1.8:
        return "Новомісяць 🌑", -6
    elif phase_days < 7.4:
        return "Зростаючий місяць 🌒", 4
    elif phase_days < 11.1:
        return "Перша чверть 🌓", 6
    elif phase_days < 16.5:
        return "Повня 🌕", 10
    elif phase_days < 22.1:
        return "Спадаючий місяць 🌖", 5
    elif phase_days < 25.8:
        return "Остання чверть 🌗", 3
    else:
        return "Старий місяць 🌘", -4

def check_sun_activity(hour: int) -> Tuple[str, str, int]:
    if 4 <= hour <= 7:
        return "🌅 Світанок (золота година)", "Максимальна ранкова активність.", 16
    elif 19 <= hour <= 21:
        return "🌇 Захід сонця", "Вихід хижака та ляща на мілководдя.", 14
    elif hour >= 22 or hour < 4:
        return "🌙 Ніч", "Можливий кльов сома та великого ляща.", 4
    else:
        return "☀️ День", "Стандартна активність.", 0

def get_season_advice(month: int, fish_type: str) -> str:
    if fish_type == "Лящ":
        if month in [5, 6]:
            return "🐟 Лящ активно годується після нересту. Шукайте його на глибинах 3-5 м."
        elif month in [7, 8]:
            return "🐟 Літній лящ тримається на ямах і брівках. Використовуйте донні снасті."
        elif month in [9, 10]:
            return "🐟 Осінній лящ збирається в зграї. Ловіть на великі насадки."
        else:
            return "🐟 Лящ у холодній воді пасивний. Використовуйте дрібні насадки."
    elif fish_type == "Карась":
        if month in [5, 6]:
            return "🐟 Карась нереститься, клює у зарослих місцях."
        elif month in [7, 8]:
            return "🐟 Карась віддає перевагу теплій мілководній зоні. Ловіть на поплавок."
        else:
            return "🐟 Карась бере лише на дуже делікатну снасть."
    elif fish_type == "Короп":
        if month in [6, 7, 8]:
            return "🐟 Короп активний, шукайте його біля очерету та корчів."
        else:
            return "🐟 Короп переходить на глибокі ділянки. Використовуйте бойли."
    elif fish_type == "Щука":
        if month in [3, 4]:
            return "🐟 Переднерестовий жор щуки. Ловіть на живця."
        elif month in [9, 10, 11]:
            return "🐟 Осінній жор щуки. Найкращі результати на великі воблери."
        else:
            return "🐟 Щука тримається біля підводних укриттів. Спробуйте джиг."
    elif fish_type == "Окунь":
        if month in [6, 7, 8]:
            return "🐟 Окунь ганяє малька біля поверхні. Використовуйте вертушки."
        else:
            return "🐟 Окунь опускається на глибину. Ловіть на джиг або блешню."
    elif fish_type == "Сом":
        if month in [6, 7, 8]:
            return "🐟 Сом виходить на мілководдя вночі. Ловіть на квок або донку."
        else:
            return "🐟 Сом пасивний, шукайте його на ямах."
    elif fish_type == "Плотва":
        if month in [5, 6]:
            return "🐟 Плотва активно годується на течії. Використовуйте легку снасть."
        elif month in [7, 8]:
            return "🐟 Плотва тримається в середніх шарах води."
        else:
            return "🐟 Плотва клює обережно, потрібна тонка оснастка."
    return ""

# ====================== ОБРАТНОЕ ГЕОКОДИРОВАНИЕ ======================
async def get_location_name(lat: float, lon: float) -> str:
    cache_key = f"{lat:.5f},{lon:.5f}"
    now = datetime.now().timestamp()
    if cache_key in geocode_cache:
        name, ts = geocode_cache[cache_key]
        if now - ts < GEOCODE_CACHE_TTL:
            return name

    url = (
        "https://nominatim.openstreetmap.org/reverse"
        f"?lat={lat}&lon={lon}&format=json&zoom=14"
        "&addressdetails=1&accept-language=uk"
    )
    headers = {"User-Agent": "FishingForecastBot/1.0 (contact@example.com)"}  # Змініть на свій email
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    address = data.get("address", {})
                    name = (
                        address.get("city") or
                        address.get("town") or
                        address.get("village") or
                        address.get("suburb") or
                        address.get("hamlet") or
                        address.get("municipality") or
                        data.get("display_name", "Ваша геолокація")
                    )
                    if len(name) > 50:
                        name = name[:47] + "..."
                    geocode_cache[cache_key] = (name, now)
                    if len(geocode_cache) > 100:
                        geocode_cache.popitem(last=False)
                    return name
    except Exception as e:
        logging.warning(f"Geocoding error: {e}")
    return "Ваша геолокація"

# ====================== ПОГОДНЫЙ КЛИЕНТ ======================
class MultiSourceWeatherClient:
    def __init__(self, lat: float, lon: float):
        self.lat = lat
        self.lon = lon
        self.cache_key = f"{round(lat, 2)}_{round(lon, 2)}"

    async def fetch_open_meteo(self, session, model: Optional[str] = None):
        global RATE_LIMIT_UNTIL, last_weather_request_time

        async with weather_semaphore:
            now = datetime.now().timestamp()
            if now < RATE_LIMIT_UNTIL:
                logging.warning("Rate-limit cooldown активний — запит пропущено")
                return None

            # Витримуємо мінімальний інтервал між запитами
            time_since_last = now - last_weather_request_time
            if time_since_last < MIN_REQUEST_INTERVAL:
                await asyncio.sleep(MIN_REQUEST_INTERVAL - time_since_last)

            model_param = f"&models={model}" if model else ""
            url = (
                f"https://api.open-meteo.com/v1/forecast?latitude={self.lat}&longitude={self.lon}"
                f"&hourly=temperature_2m,apparent_temperature,relative_humidity_2m,surface_pressure,"
                f"wind_speed_10m,wind_direction_10m,cloud_cover,precipitation,sea_surface_temperature"
                f"{model_param}&timezone=auto&past_days=2&forecast_days=3"
            )

            for attempt in range(2):
                try:
                    timeout = aiohttp.ClientTimeout(total=30, connect=10)
                    last_weather_request_time = datetime.now().timestamp()
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status == 200:
                            return await resp.json()

                        if resp.status == 429:
                            cooldown = 15 * 60  # 15 хвилин
                            RATE_LIMIT_UNTIL = datetime.now().timestamp() + cooldown
                            logging.error(f"Open-Meteo 429 → cooldown {cooldown // 60} хв")
                            await asyncio.sleep(5)
                            return None

                        logging.error(f"Open-Meteo статус {resp.status} (model={model})")
                        await asyncio.sleep(2)

                except asyncio.TimeoutError:
                    logging.warning(f"Timeout Open-Meteo (model={model})")
                    await asyncio.sleep(1)
                except Exception as e:
                    logging.warning(f"Помилка Open-Meteo (model={model}): {e}")
                    await asyncio.sleep(2)

            return None

    async def get_averaged_weather(self):
        global RATE_LIMIT_UNTIL
        now = datetime.now().timestamp()

        if self.cache_key in weather_cache:
            data, ts = weather_cache[self.cache_key]
            if now - ts < CACHE_TTL:
                logging.info("Використовую свіжий кеш погоди")
                return data

        if now < RATE_LIMIT_UNTIL:
            if self.cache_key in weather_cache:
                logging.info("Cooldown активний → віддаю застарілий кеш")
                return weather_cache[self.cache_key][0]
            logging.warning("Cooldown активний і кешу немає")
            return None

        async with aiohttp.ClientSession() as session:
            res = await self.fetch_open_meteo(session)

            if not res:
                if self.cache_key in weather_cache:
                    logging.info("API недоступне → віддаю застарілий кеш")
                    return weather_cache[self.cache_key][0]
                return None

            weather_cache[self.cache_key] = (res, now)
            if len(weather_cache) > CACHE_MAX_ENTRIES:
                weather_cache.popitem(last=False)
            return res

    def _pressure_score(self, pressure_mm: float, is_predator: bool) -> int:
        optimum = 748 if is_predator else 752
        diff = abs(pressure_mm - optimum)
        if diff <= 3:
            return 14
        elif diff <= 6:
            return 8
        elif diff <= 10:
            return 0
        elif diff <= 15:
            return -10
        return -18

    def _pressure_trend_score(self, pressures: list, idx: int) -> Tuple[str, int]:
        if idx < 24:
            return "Недостатньо даних", 0
        recent = [p for p in pressures[idx - 12:idx + 1] if p is not None]
        older = [p for p in pressures[idx - 24:idx - 12] if p is not None]
        if len(recent) < 5 or len(older) < 5:
            return "Недостатньо даних", 0

        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older)
        delta = (avg_recent - avg_older) * 0.75006

        if delta < -2.5:
            return "Сильно падає 📉 (перед фронтом)", 12
        elif delta < -0.8:
            return "Повільно падає 📉", 8
        elif delta > 2.5:
            return "Сильно росте 📈", -6
        elif delta > 0.8:
            return "Повільно росте 📈", 2
        return "Стабільний ✅", 10

    def _stability_score(self, pressures: list, idx: int) -> Tuple[str, int]:
        if idx < 48:
            return "Недостатньо історії", 0
        valid = [p for p in pressures[idx - 48:idx + 1] if p is not None]
        if len(valid) < 20:
            return "Недостатньо даних", 0
        diff = max(valid) - min(valid)
        if diff <= 4:
            return "Дуже стабільний ✅", 12
        elif diff <= 7:
            return "Стабільний", 6
        elif diff <= 11:
            return "Помірно мінливий ⚠️", -4
        return "Стрибкоподібний ❌", -16

    def _temperature_score(self, temp: float, water_temp: float, is_predator: bool) -> int:
        if is_predator:
            if 8 <= water_temp <= 16:
                return 12
            elif 5 <= water_temp <= 20:
                return 6
            elif water_temp > 24 or water_temp < 3:
                return -10
            return 0
        else:
            if 16 <= water_temp <= 23:
                return 12
            elif 12 <= water_temp <= 26:
                return 6
            elif water_temp > 28 or water_temp < 8:
                return -8
            return 0

    def _wind_score(self, wind_ms: float, wind_dir: str, is_predator: bool) -> int:
        if wind_ms < 1.5:
            score = -4 if is_predator else 2
        elif 2.0 <= wind_ms <= 5.5:
            score = 10
        elif 5.5 < wind_ms <= 7.5:
            score = 2
        elif wind_ms > 9:
            score = -22
        else:
            score = -8

        if wind_dir in {"Пд", "Пд-Зх", "Зх", "Пд-Сх"}:
            score += 4
        elif wind_dir in {"Пн", "Пн-Сх"}:
            score -= 3
        return score

    def _precip_score(self, precip: float, is_predator: bool) -> int:
        if precip <= 0.1:
            return 0
        elif 0.2 <= precip <= 1.8:
            return 7 if is_predator else 4
        elif precip <= 3.5:
            return -6
        return -16

    def _cloud_score(self, cloud: float, is_predator: bool) -> int:
        if is_predator:
            if cloud >= 70:
                return 9
            elif cloud >= 40:
                return 4
            return -3
        else:
            if cloud >= 80:
                return 2
            elif cloud <= 30:
                return 3
            return 0

    def calculate_star_score(self, score_100: float) -> int:
        if score_100 >= 84:
            return 5
        elif score_100 >= 68:
            return 4
        elif score_100 >= 50:
            return 3
        elif score_100 >= 32:
            return 2
        elif score_100 > 12:
            return 1
        return 0

    def generate_expert_commentary(
        self, fish_type, pressure_mm, trend_text, stability_text,
        wind_ms, wind_dir, precip, sun_title, sun_desc,
        temp, water_temp, score, humidity, cloud_cover, moon_text
    ):
        current_month = datetime.now().month
        season_advice = get_season_advice(current_month, fish_type)

        comments = [
            "📋 <b>ДЕТАЛЬНИЙ АНАЛІЗ УМОВ</b>",
            f"{season_advice}",
            "",
            f"⏰ <b>Період доби:</b> {sun_title}",
            f"   {sun_desc}",
            f"🌙 <b>Фаза місяця:</b> {moon_text}",
            "",
            f"🌡 <b>Температура:</b> повітря {temp}°C, вода ~{water_temp}°C",
        ]

        if water_temp > 25:
            comments.append("   🔥 Спека — шукайте тінь, глибину, течію. Риба пасивна вдень.")
        elif water_temp < 9:
            comments.append("   ❄️ Холодна вода — повільна подача, дрібні наживки.")
        else:
            comments.append("   ✅ Комфортна температура для активності риби.")

        comments.append("")
        comments.append(f"🌀 <b>Атмосферний тиск:</b> {pressure_mm} мм")
        comments.append(f"   Тренд: {trend_text}")
        comments.append(f"   Стабільність: {stability_text}")

        if "падає" in trend_text:
            comments.append("   💡 При падінні тиску хижак активізується, мирна риба відходить на глибину.")
        elif "росте" in trend_text:
            comments.append("   💡 При рості тиску мирна риба виходить на мілководдя.")

        comments.append("")
        comments.append(f"💨 <b>Вітер:</b> {wind_ms} м/с, {wind_dir}")

        if wind_ms < 2:
            comments.append("   🔇 Штиль — обережна риба, використовуйте тонкі снасті.")
        elif 2 <= wind_ms <= 5.5:
            comments.append("   👍 Сприятливий вітер, ряб на воді маскує рибалку.")
        elif wind_ms > 7:
            comments.append("   ⚠️ Сильний вітер — тримайтесь підвітряного берега.")
        else:
            comments.append("   💨 Помірний вітер, можлива ловля на спінінг.")

        comments.append("")
        comments.append(f"🌧 <b>Опади:</b> {precip} мм")
        comments.append(f"☁️ <b>Хмарність:</b> {cloud_cover}%")

        if precip > 1.5:
            comments.append("   🎣 Дощ покращує кльов хижака та великого ляща.")
        elif cloud_cover > 65:
            comments.append("   🎣 Хмарно — гарний час для хижої риби.")

        comments.append("")
        comments.append(f"🎯 <b>Рекомендації по {fish_type}:</b>")
        if fish_type in PREDATOR_FISH:
            comments.append("   • Використовуйте активні проводки на брівках і перепадах.")
            comments.append("   • Шукайте хижака біля укриттів: корчі, каміння, водорості.")
            if fish_type == "Щука":
                comments.append("   • Найкращі приманки: великі воблери, блешні, живці.")
            elif fish_type == "Окунь":
                comments.append("   • Ефективні вертушки, мікроджиг, відвідний поводок.")
            elif fish_type == "Сом":
                comments.append("   • Ловіть на квок, донку з живцем або великі бойли.")
        else:
            comments.append("   • Використовуйте дрібну фракцію + мотиль/опариш/кукурудза.")
            comments.append("   • Підгодовуйте місце ловлі, але не перегодовуйте.")
            if fish_type == "Лящ":
                comments.append("   • Ловіть на донку або фідер на відстані 20-40 м від берега.")
            elif fish_type == "Карась":
                comments.append("   • Поплавкова вудка з легким поплавком біля очерету.")
            elif fish_type == "Короп":
                comments.append("   • Потрібна потужна снасть, бойли або велика кукурудза.")
            elif fish_type == "Плотва":
                comments.append("   • Легка махова вудка, тонка оснастка, дрібний гачок.")

        comments.append("")
        if score >= 78:
            comments.append("🏆 <b>ВИСНОВОК:</b> Відмінні умови! Вирушайте на водойму негайно.")
        elif score >= 55:
            comments.append("⚖️ <b>ВИСНОВОК:</b> Гарні умови. Успіх залежить від правильного місця та приманки.")
        else:
            comments.append("⚠️ <b>ВИСНОВОК:</b> Складні умови. Потрібні майстерність і терпіння.")

        comments.append("")
        comments.append("📊 <i>Прогноз базується на даних Open-Meteo та експертних алгоритмах.</i>")

        return "\n".join(comments)

    async def evaluate_biting(self, fish_type: str, region: str, target_hour: int, day_offset: int = 0):
        data = await self.get_averaged_weather()
        if not data:
            return None

        hourly = data.get("hourly")
        if not hourly or "surface_pressure" not in hourly:
            logging.error("Некоректні дані від API")
            return None

        pressures = hourly["surface_pressure"]
        if not pressures:
            logging.error("Пустий масив тиску")
            return None

        max_idx = len(pressures) - 1
        target_index = 48 + day_offset * 24 + target_hour
        if target_index > max_idx:
            target_index = max_idx

        def safe(val, default):
            return val if val is not None else default

        pressure_hpa = safe(pressures[target_index], 1013.25)
        pressure_mm = pressure_hpa * 0.75006
        wind_ms = safe(hourly.get("wind_speed_10m", [None])[target_index], 2.5)
        temp = safe(hourly.get("temperature_2m", [None])[target_index], 18.0)
        precip = safe(hourly.get("precipitation", [None])[target_index], 0.0)
        wind_dir = get_wind_direction_text(hourly.get("wind_direction_10m", [None])[target_index])
        humidity = safe(hourly.get("relative_humidity_2m", [None])[target_index], 55)
        cloud_cover = safe(hourly.get("cloud_cover", [None])[target_index], 40)

        water_list = hourly.get("sea_surface_temperature", [None] * len(pressures))
        water_temp = safe(water_list[target_index] if target_index < len(water_list) else None,
                          round(temp * 0.82 + 3.2, 1))

        is_predator = fish_type in PREDATOR_FISH

        score = 48
        stab_text, stab_pts = self._stability_score(pressures, target_index)
        score += stab_pts
        trend_text, trend_pts = self._pressure_trend_score(pressures, target_index)
        score += trend_pts
        score += self._pressure_score(pressure_mm, is_predator)
        score += self._temperature_score(temp, water_temp, is_predator)
        score += self._wind_score(wind_ms, wind_dir, is_predator)
        score += self._precip_score(precip, is_predator)
        score += self._cloud_score(cloud_cover, is_predator)

        sun_title, sun_desc, sun_pts = check_sun_activity(target_hour)
        score += sun_pts

        target_date = datetime.now() + timedelta(days=day_offset)
        moon_text, moon_pts = get_moon_phase_info(target_date)
        score += moon_pts if is_predator else int(moon_pts * 0.5)

        final_score = min(100, max(0, score))
        stars = self.calculate_star_score(final_score)

        date_str = target_date.strftime("%d.%m.%Y")
        if day_offset == 0:
            day_text = f"Сьогодні ({date_str})"
        elif day_offset == 1:
            day_text = f"Завтра ({date_str})"
        else:
            day_text = f"Післязавтра ({date_str})"

        commentary = self.generate_expert_commentary(
            fish_type, round(pressure_mm, 1), trend_text, stab_text,
            round(wind_ms, 1), wind_dir, round(precip, 1),
            sun_title, sun_desc, round(temp, 1), round(water_temp, 1),
            final_score, round(humidity), round(cloud_cover), moon_text
        )

        return {
            "fish": fish_type,
            "forecast_day": day_text,
            "hour": target_hour,
            "pressure_mm": round(pressure_mm, 1),
            "pressure_stability": stab_text,
            "pressure_trend": trend_text,
            "wind_ms": round(wind_ms, 1),
            "wind_dir": wind_dir,
            "humidity": round(humidity),
            "cloud_cover": round(cloud_cover),
            "precipitation": round(precip, 1),
            "temperature": round(temp, 1),
            "water_temp": round(water_temp, 1),
            "moon_phase": moon_text,
            "stars": stars,
            "stars_graphic": "⭐" * stars + "☆" * (5 - stars),
            "expert_commentary": commentary,
            "sources_used": "Open-Meteo (GFS)",
            "score_100": final_score,
        }

# ====================== БОТ ======================
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

def get_regions_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Дніпропетровська"), KeyboardButton(text="Київська")],
            [KeyboardButton(text="Полтавська"), KeyboardButton(text="Запорізька")],
            [KeyboardButton(text="Черкаська")],
            [KeyboardButton(text="📍 Надіслати геолокацію", request_location=True)],
            [KeyboardButton(text="📜 Моя історія"), KeyboardButton(text="ℹ️ Допомога")],
        ],
        resize_keyboard=True,
    )

def get_fish_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Лящ"), KeyboardButton(text="Карась"), KeyboardButton(text="Короп")],
            [KeyboardButton(text="Щука"), KeyboardButton(text="Окунь"), KeyboardButton(text="Сом")],
            [KeyboardButton(text="Плотва"), KeyboardButton(text="◀️ Змінити область")],
        ],
        resize_keyboard=True,
    )

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ForecastStates.choosing_region)
    await message.answer(
        "🎣 <b>Вітаємо!</b>\n\n"
        "Оберіть область зі списку або надішліть свою геолокацію для точного прогнозу.",
        reply_markup=get_regions_keyboard(),
        parse_mode="HTML",
    )

@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Допомога")
async def cmd_help(message: Message):
    text = (
        "📖 <b>Допомога</b>\n\n"
        "<b>Як користуватися:</b>\n"
        "1. Оберіть область або надішліть геолокацію.\n"
        "2. Виберіть рибу.\n"
        "3. Вкажіть день та час.\n"
        "4. Отримайте детальний прогноз з порадами.\n\n"
        "<b>Що враховує прогноз:</b>\n"
        "• Температура повітря та води\n"
        "• Атмосферний тиск та його тренд\n"
        "• Вітер, опади, хмарність\n"
        "• Фаза місяця та час доби\n"
        "• Сезонні особливості поведінки риби\n\n"
        "<b>Джерела даних:</b> Open-Meteo (GFS), кеш 12 годин."
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📜 Моя історія")
async def show_history(message: Message):
    rows = await get_user_history_from_db(message.from_user.id)
    if not rows:
        await message.answer("У вас поки немає збережених прогнозів.")
        return

    text = "<b>📜 Ваші останні прогнози:</b>\n\n"
    for row in rows:
        region, fish, day, hour, stars, ts = row
        graphic = "⭐" * (stars or 0) + "☆" * (5 - (stars or 0))
        hour_str = f"{hour:02d}:00" if hour is not None else "—"
        text += f"📍 {region} | 🎣 {fish}\n{day} о {hour_str}\nОцінка: {graphic}\n🕒 {ts}\n\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.location)
async def handle_location(message: Message, state: FSMContext):
    loc = message.location
    lat, lon = loc.latitude, loc.longitude

    location_name = await get_location_name(lat, lon)

    await state.update_data(lat=lat, lon=lon, location_name=location_name)
    await state.set_state(ForecastStates.choosing_fish)
    await message.answer(
        f"📍 <b>Місце прогнозу:</b> {location_name}\n\n"
        f"Оберіть рибу:",
        reply_markup=get_fish_keyboard(),
        parse_mode="HTML",
    )

@dp.message(F.text.in_(REGIONS.keys()))
async def handle_region(message: Message, state: FSMContext):
    await state.update_data(region=message.text)
    await state.set_state(ForecastStates.choosing_fish)
    await message.answer(
        f"Область: <b>{message.text}</b>\nОберіть рибу:",
        reply_markup=get_fish_keyboard(),
        parse_mode="HTML",
    )

@dp.message(F.text == "◀️ Змінити область")
async def change_region(message: Message, state: FSMContext):
    await cmd_start(message, state)

@dp.message(F.text.in_(FISH_LIST))
async def handle_fish(message: Message, state: FSMContext):
    data = await state.get_data()
    if "region" not in data and "lat" not in data:
        await message.answer("Спочатку оберіть область або надішліть геолокацію через /start")
        return

    await state.update_data(fish=message.text)
    await state.set_state(ForecastStates.choosing_day)

    today = datetime.now()
    buttons = []
    for i in range(3):
        d = today + timedelta(days=i)
        label = {
            0: f"Сьогодні ({d.strftime('%d.%m')})",
            1: f"Завтра ({d.strftime('%d.%m')})",
            2: f"Післязавтра ({d.strftime('%d.%m')})",
        }[i]
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"day_{i}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад (до вибору риби)", callback_data="back_to_fish")])

    await message.answer(
        f"Риба: <b>{message.text}</b>\nОберіть день:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )

@dp.callback_query(F.data == "back_to_fish")
async def handle_back_to_fish(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ForecastStates.choosing_fish)
    await callback.message.edit_text("Оберіть рибу за допомогою кнопок нижче 👇")
    await callback.answer()

@dp.callback_query(F.data.startswith("day_"))
async def handle_day(callback: CallbackQuery, state: FSMContext):
    day_offset = int(callback.data.split("_")[1])
    await state.update_data(day_offset=day_offset)
    await state.set_state(ForecastStates.choosing_hour)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌅 Світанок (06:00)", callback_data="hour_6")],
        [InlineKeyboardButton(text="☀️ День (12:00)", callback_data="hour_12")],
        [InlineKeyboardButton(text="🌇 Захід (20:00)", callback_data="hour_20")],
        [InlineKeyboardButton(text="◀️ Назад (до вибору дня)", callback_data="back_to_day")],
    ])
    await callback.message.edit_text("Оберіть час доби:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "back_to_day")
async def handle_back_to_day(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ForecastStates.choosing_day)
    data = await state.get_data()
    fish_type = data.get("fish", "Рибу")

    today = datetime.now()
    buttons = []
    for i in range(3):
        d = today + timedelta(days=i)
        label = {
            0: f"Сьогодні ({d.strftime('%d.%m')})",
            1: f"Завтра ({d.strftime('%d.%m')})",
            2: f"Післязавтра ({d.strftime('%d.%m')})",
        }[i]
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"day_{i}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад (до вибору риби)", callback_data="back_to_fish")])

    await callback.message.edit_text(
        f"Риба: <b>{fish_type}</b>\nОберіть день:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("hour_"))
async def handle_hour(callback: CallbackQuery, state: FSMContext):
    hour = int(callback.data.split("_")[1])
    data = await state.get_data()

    fish_type = data.get("fish", "Лящ")
    day_offset = data.get("day_offset", 0)

    if "lat" in data and "lon" in data:
        lat, lon = data["lat"], data["lon"]
        region_display = data.get("location_name", "Ваша геолокація")
    elif "region" in data:
        region = data["region"]
        if region in REGIONS:
            coords = REGIONS[region]
            lat, lon = coords["lat"], coords["lon"]
            region_display = region
        else:
            await callback.message.edit_text("Помилка: некоректна область.")
            return
    else:
        await callback.message.edit_text("Помилка: не вказано місце.")
        return

    client = MultiSourceWeatherClient(lat, lon)

    await callback.message.edit_text("⏳ Аналізую погоду та розраховую кльов...")

    result = await client.evaluate_biting(fish_type, region_display, hour, day_offset)

    if not result:
        error_text = (
            "❌ Open-Meteo тимчасово обмежив запити (rate limit).\n\n"
            "Це нормально на безкоштовному API.\n"
            "Спробуйте через <b>15 хвилин</b>.\n"
            "Дані кешуються на 12 годин."
        )
        await callback.message.edit_text(error_text, parse_mode="HTML")
        await state.clear()
        await callback.answer()
        return

    forecast_id = await save_forecast_to_db(
        callback.from_user.id, region_display, fish_type,
        result["forecast_day"], result["hour"],
        result["pressure_mm"], result["wind_ms"],
        result["temperature"], result["stars"],
    )

    response = (
        f"📊 <b>ПРОГНОЗ КЛЬОВУ</b>\n"
        f"📍 <b>Місце:</b> {region_display}\n"
        f"📅 <b>Час:</b> {result['forecast_day']} о {result['hour']:02d}:00\n"
        f"🎣 <b>Риба:</b> {fish_type}\n"
        f"⭐ <b>Оцінка:</b> {result['stars']}/5 {result['stars_graphic']}\n"
        f"📈 <b>Бал:</b> {result['score_100']}/100\n"
        f"──────────────────\n"
        f"{result['expert_commentary']}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Поділитися в чаті", callback_data=f"share_{result['stars']}_{fish_type}_{region_display}")],
        [InlineKeyboardButton(text="💬 Перейти в чат", url=GROUP_URL)],
        [
            InlineKeyboardButton(text="👍 Точний", callback_data=f"fb_good_{forecast_id}"),
            InlineKeyboardButton(text="👎 Хибний", callback_data=f"fb_bad_{forecast_id}"),
        ],
    ])

    await callback.message.answer(response, reply_markup=kb, parse_mode="HTML")
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.startswith("fb_"))
async def handle_feedback(callback: CallbackQuery):
    try:
        parts = callback.data.split("_")
        rating = parts[1]
        forecast_id = int(parts[2])
        await save_feedback_to_db(callback.from_user.id, forecast_id, rating)
        msg = "Дякуємо! Відгук допоможе покращити прогнози 👍" if rating == "good" else "Дякуємо за зворотний зв’язок 👎"
        await callback.answer(msg, show_alert=True)
    except Exception as e:
        logging.error(f"Feedback error: {e}")
        await callback.answer("Помилка збереження відгуку", show_alert=True)

@dp.callback_query(F.data.startswith("share_"))
async def handle_share(callback: CallbackQuery):
    try:
        _, stars, fish, region = callback.data.split("_", 3)
        graphic = "⭐" * int(stars) + "☆" * (5 - int(stars))
        text = (
            f"📢 <b>{callback.from_user.first_name} поділився прогнозом!</b>\n"
            f"📍 {region} | 🎣 <b>{fish}</b>\n"
            f"⭐ {stars}/5 ({graphic})\n"
            f"💬 Приєднуйтесь!"
        )
        await bot.send_message(GROUP_CHAT_ID, text, parse_mode="HTML")
        await callback.answer("✅ Надіслано в чат клубу!", show_alert=True)
    except Exception as e:
        logging.error(f"Share error: {e}")
        await callback.answer("❌ Помилка відправки", show_alert=True)

@dp.message()
async def fallback(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Натисніть /start", reply_markup=get_regions_keyboard())

# ====================== ЗАПУСК ======================
async def health(_):
    return web.Response(text="Fishing bot is running ✅")

async def main():
    await init_db()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Health server started on port {port}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    while not stop_event.is_set():
        try:
            logging.info("Starting polling...")
            await dp.start_polling(bot, stop_event=stop_event)
        except Exception as e:
            logging.error(f"Polling crashed: {e}")
            await asyncio.sleep(5)
        else:
            break

    logging.info("Shutting down...")
    await dp.stop_polling()
    await bot.session.close()
    await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
