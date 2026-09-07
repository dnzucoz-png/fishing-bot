мimport asyncio
import logging
import os
import sqlite3
import math
import io
from datetime import datetime, timedelta
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, Location, BufferedInputFile
)
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiohttp import web
import aiohttp

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from PIL import Image, ImageDraw, ImageFont

# ====================== НАЛАШТУВАННЯ ======================
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не встановлено!")

GROUP_CHAT_ID = -1004434293069
GROUP_URL = "https://t.me/+rKxYkNg85aAwNzFi"

REGIONS = {
    "Дніпропетровська": {"lat": 48.4647, "lon": 35.0462},
    "Київська": {"lat": 50.4501, "lon": 30.5234},
    "Полтавська": {"lat": 49.5895, "lon": 34.5514},
    "Запорізька": {"lat": 47.8388, "lon": 35.1396},
    "Черкаська": {"lat": 49.4444, "lon": 32.0598},
}

FISH_LIST = ["Лящ", "Карась", "Короп", "Щука", "Окунь", "Сом", "Плотва"]

SPAWNING = {
    "Лящ": {"start": 4, "end": 6},
    "Щука": {"start": 3, "end": 4},
    "Окунь": {"start": 4, "end": 5},
    "Сом": {"start": 5, "end": 7},
    "Короп": {"start": 5, "end": 6},
    "Карась": {"start": 5, "end": 7},
    "Плотва": {"start": 4, "end": 6},
}

WATER_BODIES = {
    "Дніпропетровська": [
        {"name": "Каховське водосховище", "lat": 47.5, "lon": 34.2},
        {"name": "Річка Дніпро", "lat": 48.45, "lon": 35.05},
        {"name": "Самарська затока", "lat": 48.6, "lon": 35.2},
    ],
    "Київська": [
        {"name": "Київське водосховище", "lat": 50.8, "lon": 30.5},
        {"name": "Річка Дніпро", "lat": 50.45, "lon": 30.52},
        {"name": "Озеро Святе", "lat": 50.5, "lon": 30.7},
    ],
    "Полтавська": [
        {"name": "Кременчуцьке водосховище", "lat": 49.1, "lon": 33.4},
        {"name": "Річка Псел", "lat": 49.6, "lon": 34.5},
    ],
    "Запорізька": [
        {"name": "Дніпровське водосховище", "lat": 47.8, "lon": 35.1},
        {"name": "Річка Молочна", "lat": 47.2, "lon": 35.3},
    ],
    "Черкаська": [
        {"name": "Кременчуцьке водосховище", "lat": 49.3, "lon": 32.0},
        {"name": "Річка Дніпро", "lat": 49.4, "lon": 32.06},
    ],
}

LANGUAGES = {
    "uk": {
        "start": "🎣 <b>Вітаю у Fishing Forecast!</b>\n\nОберіть область для прогнозу кльову або натисніть «📍 Моє місце», щоб визначити автоматично.",
        "help": "<b>Як працює оцінка кльову:</b>\n\n🔹 Стабільність тиску (48 год)\n🔹 Тренд тиску (падає/росте)\n🔹 Абсолютний тиск\n🔹 Температура повітря і води\n🔹 Вітер, опади, хмарність\n🔹 Золоті години та фаза місяця\n🔹 Індекс комфортності погоди\n\n📊 <b>Шкала оцінок:</b>\n⭐ 5/5 – ідеальні умови\n⭐ 4/5 – дуже добре\n⭐ 3/5 – непогано, але нюанси\n⭐ 2/5 – посередньо\n⭐ 1/5 – краще вдома\n\n📦 <b>Джерело даних:</b> Open-Meteo (GFS, ECMWF)\n⏳ Кеш погоди оновлюється кожні 2 години.",
        "history_empty": "У вас поки немає збережених прогнозів.",
        "choose_region": "Оберіть область:",
        "choose_fish": "Оберіть рибу:",
        "choose_day": "Оберіть день:",
        "choose_hour": "Оберіть час доби або введіть будь-яку годину (0-23):",
        "processing": "⏳ Аналізую погоду та розраховую кльов...",
        "rate_limit": "❌ Open-Meteo тимчасово обмежив запити (rate limit).\n\nЦе нормально на безкоштовному API.\nСпробуйте через <b>8–12 хвилин</b>.\nДані кешуються на 2 години.",
        "share": "📢 <b>{name} поділився прогнозом!</b>\n📍 {region} | 🎣 <b>{fish}</b>\n⭐ {stars}/5 ({graphic})\n💬 Приєднуйтесь!",
        "feedback_good": "Дякуємо! Відгук допоможе покращити прогнози 👍",
        "feedback_bad": "Дякуємо за зворотний зв’язок 👎",
        "subscribe_done": "✅ Підписку налаштовано! Прогноз для {region} на {fish} о {hour:02d}:00 надсилатиметься щодня.",
        "season_info": "🗓 <b>Сезон для {fish}:</b>\nНерест: {season}\nСтатус: {status}",
        "no_data": "Дані відсутні.",
        "weather_alert": "⚠️ <b>Штормове попередження!</b>\nТиск різко падає ({delta:.1f} мм). Риба може погано кльовити.",
    },
    "ru": {
        "start": "🎣 <b>Добро пожаловать в Fishing Forecast!</b>\n\nВыберите область для прогноза клёва или нажмите «📍 Моё место», чтобы определить автоматически.",
        "help": "<b>Как работает оценка клёва:</b>\n\n🔹 Стабильность давления (48 ч)\n🔹 Тренд давления (падает/растёт)\n🔹 Абсолютное давление\n🔹 Температура воздуха и воды\n🔹 Ветер, осадки, облачность\n🔹 Золотые часы и фаза луны\n🔹 Индекс комфортности погоды\n\n📊 <b>Шкала оценок:</b>\n⭐ 5/5 – идеальные условия\n⭐ 4/5 – очень хорошо\n⭐ 3/5 – неплохо, но нюансы\n⭐ 2/5 – посредственно\n⭐ 1/5 – лучше дома\n\n📦 <b>Источник данных:</b> Open-Meteo (GFS, ECMWF)\n⏳ Кэш погоды обновляется каждые 2 часа.",
        "history_empty": "У вас пока нет сохранённых прогнозов.",
        "choose_region": "Выберите область:",
        "choose_fish": "Выберите рыбу:",
        "choose_day": "Выберите день:",
        "choose_hour": "Выберите время или введите любой час (0-23):",
        "processing": "⏳ Анализирую погоду и рассчитываю клёв...",
        "rate_limit": "❌ Open-Meteo временно ограничил запросы (rate limit).\n\nЭто нормально на бесплатном API.\nПопробуйте через <b>8–12 минут</b>.\nДанные кешируются на 2 часа.",
        "share": "📢 <b>{name} поделился прогнозом!</b>\n📍 {region} | 🎣 <b>{fish}</b>\n⭐ {stars}/5 ({graphic})\n💬 Присоединяйтесь!",
        "feedback_good": "Спасибо! Отзыв поможет улучшить прогнозы 👍",
        "feedback_bad": "Спасибо за обратную связь 👎",
        "subscribe_done": "✅ Подписка настроена! Прогноз для {region} на {fish} в {hour:02d}:00 будет приходить ежедневно.",
        "season_info": "🗓 <b>Сезон для {fish}:</b>\nНерест: {season}\nСтатус: {status}",
        "no_data": "Данные отсутствуют.",
        "weather_alert": "⚠️ <b>Штормовое предупреждение!</b>\nДавление резко падает ({delta:.1f} мм). Рыба может плохо клевать.",
    },
}

weather_cache = {}
CACHE_TTL = 2 * 60 * 60
RATE_LIMIT_UNTIL = 0

class ForecastStates(StatesGroup):
    choosing_region = State()
    choosing_fish = State()
    choosing_day = State()
    choosing_hour = State()
    choosing_hour_manual = State()

class SubscribeStates(StatesGroup):
    region = State()
    fish = State()
    hour = State()

class LanguageStates(StatesGroup):
    choose_language = State()

class TrophyStates(StatesGroup):
    waiting_fish = State()
    waiting_weight = State()
    waiting_length = State()
    waiting_location = State()
    waiting_photo = State()

# ====================== БАЗА ДАНИХ ======================
def init_db():
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            region TEXT,
            fish_type TEXT,
            hour INTEGER DEFAULT 7,
            enabled BOOLEAN DEFAULT 1
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS forecast_accuracy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_id INTEGER,
            user_id INTEGER,
            actual_stars INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (forecast_id) REFERENCES forecasts(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_fish_catches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            fish_type TEXT,
            weight REAL,
            length REAL,
            location TEXT,
            photo_file_id TEXT,
            date DATE,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_language (
            user_id INTEGER PRIMARY KEY,
            lang TEXT DEFAULT 'uk'
        )
    """)
    try:
        cursor.execute("ALTER TABLE forecasts ADD COLUMN hour INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

def get_user_lang(user_id: int) -> str:
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("SELECT lang FROM user_language WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "uk"

def set_user_lang(user_id: int, lang: str):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO user_language (user_id, lang) VALUES (?, ?)", (user_id, lang))
    conn.commit()
    conn.close()

def get_lang_dict(user_id: int) -> dict:
    lang = get_user_lang(user_id)
    return LANGUAGES.get(lang, LANGUAGES["uk"])

def save_forecast_to_db(user_id, region, fish_type, forecast_day, hour, pressure, wind, temp, stars):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO forecasts (user_id, region, fish_type, forecast_day, hour, pressure, wind, temp, stars)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, region, fish_type, forecast_day, hour, pressure, wind, temp, stars))
    forecast_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return forecast_id

def save_feedback_to_db(user_id, forecast_id, rating):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO feedback (user_id, forecast_id, rating) VALUES (?, ?, ?)",
        (user_id, forecast_id, rating)
    )
    conn.commit()
    conn.close()

def get_user_history_from_db(user_id):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT region, fish_type, forecast_day, hour, stars, timestamp
        FROM forecasts WHERE user_id = ? ORDER BY id DESC LIMIT 5
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_subscriptions():
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, region, fish_type, hour FROM subscriptions WHERE enabled=1")
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_subscription(user_id, region, fish_type, hour):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO subscriptions (user_id, region, fish_type, hour) VALUES (?, ?, ?, ?)",
                   (user_id, region, fish_type, hour))
    conn.commit()
    conn.close()

def get_forecast_accuracy(user_id):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT f.fish_type,
               COUNT(CASE WHEN fb.rating='good' THEN 1 END) as good,
               COUNT(CASE WHEN fb.rating='bad' THEN 1 END) as bad
        FROM feedback fb
        JOIN forecasts f ON fb.forecast_id = f.id
        WHERE fb.user_id = ?
        GROUP BY f.fish_type
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def add_catch(user_id, fish_type, weight, length, location, photo_file_id, date):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_fish_catches (user_id, fish_type, weight, length, location, photo_file_id, date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, fish_type, weight, length, location, photo_file_id, date))
    conn.commit()
    conn.close()

def get_user_catches(user_id):
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("SELECT fish_type, weight, length, location, photo_file_id, date FROM user_fish_catches WHERE user_id = ? ORDER BY date DESC LIMIT 20", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# ====================== ДОПОМІЖНІ ФУНКЦІЇ ======================
def get_wind_direction_text(degrees) -> str:
    if degrees is None:
        return "Н/Д"
    directions = ["Пн", "Пн-Сх", "Сх", "Пд-Сх", "Пд", "Пд-Зх", "Зх", "Пн-Зх"]
    return directions[round(degrees / 45) % 8]

def get_moon_phase_info(date_obj: datetime) -> Tuple[str, int]:
    known_new_moon = datetime(2024, 1, 11)
    phase_days = (date_obj - known_new_moon).days % 29.53
    if phase_days < 1.8:
        return "🌑 Новомісяць", -6
    elif phase_days < 7.4:
        return "🌒 Зростаючий", 4
    elif phase_days < 11.1:
        return "🌓 Перша чверть", 6
    elif phase_days < 16.5:
        return "🌕 Повня", 10
    elif phase_days < 22.1:
        return "🌖 Спадаючий", 5
    elif phase_days < 25.8:
        return "🌗 Остання чверть", 3
    else:
        return "🌘 Старий місяць", -4

def check_sun_activity(hour: int) -> Tuple[str, str, int]:
    if 4 <= hour <= 7:
        return "🌅 Світанок (золота година)", "Максимальна ранкова активність.", 16
    elif 19 <= hour <= 21:
        return "🌇 Захід сонця", "Вихід хижака та ляща на мілководдя.", 14
    elif hour >= 22 or hour < 4:
        return "🌙 Ніч", "Можливий кльов сома та великого ляща.", 4
    else:
        return "☀️ День", "Стандартна активність.", 0

def find_nearest_region(lat: float, lon: float) -> str:
    best = None
    min_dist = float("inf")
    for name, coords in REGIONS.items():
        dx = coords["lat"] - lat
        dy = coords["lon"] - lon
        dist = math.hypot(dx, dy)
        if dist < min_dist:
            min_dist = dist
            best = name
    return best

def get_bait_recommendation(fish_type, water_temp, wind_ms, is_predator):
    baits = {
        "Лящ": ["мотиль", "опариш", "черв'як", "перловка", "кукурудза"],
        "Карась": ["черв'як", "мотиль", "хліб", "горох"],
        "Короп": ["кукурудза", "горох", "бойли", "картопля"],
        "Щука": ["блешня", "воблер", "живець", "силікон"],
        "Окунь": ["вертушка", "твістер", "живець", "черв'як"],
        "Сом": ["живець", "великий черв'як", "м'ясо", "жаба"],
        "Плотва": ["мотиль", "опариш", "тісто", "хліб"]
    }
    base = baits.get(fish_type, ["черв'як"])
    if water_temp < 10:
        base.append("животна наживка (мотиль, черв'як)")
    elif water_temp > 22:
        base.append("рослинна наживка (кукурудза, горох)")
    if wind_ms > 6:
        base.append("важка оснастка для дальнього закиду")
    return ", ".join(set(base))

def get_season_status(fish_type):
    month = datetime.now().month
    if fish_type in SPAWNING:
        s = SPAWNING[fish_type]
        if s["start"] <= month <= s["end"]:
            return "🔴 <b>Заборонено</b> (нерест)", "нерест"
        else:
            return "🟢 <b>Дозволено</b>", "дозволено"
    return "❓ Немає даних", "немає даних"

# ====================== ПОГОДНИЙ КЛІЄНТ ======================
class MultiSourceWeatherClient:
    def __init__(self, lat: float, lon: float):
        self.lat = lat
        self.lon = lon
        self.cache_key = f"{lat}_{lon}"

    async def fetch_open_meteo(self, session, model: Optional[str] = None):
        global RATE_LIMIT_UNTIL
        now = datetime.now().timestamp()
        if now < RATE_LIMIT_UNTIL:
            return None
        model_param = f"&models={model}" if model else ""
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={self.lat}&longitude={self.lon}"
            f"&hourly=temperature_2m,apparent_temperature,relative_humidity_2m,surface_pressure,"
            f"wind_speed_10m,wind_direction_10m,cloud_cover,precipitation,sea_surface_temperature"
            f"{model_param}&timezone=auto&past_days=2&forecast_days=3"
        )
        for attempt in range(2):
            try:
                timeout = aiohttp.ClientTimeout(total=15, connect=7)
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 429:
                        cooldown = 10 * 60 + attempt * 120
                        RATE_LIMIT_UNTIL = datetime.now().timestamp() + cooldown
                        return None
                    await asyncio.sleep(2)
            except:
                await asyncio.sleep(2)
        return None

    async def get_averaged_weather(self):
        global RATE_LIMIT_UNTIL
        now = datetime.now().timestamp()
        if self.cache_key in weather_cache:
            data, ts = weather_cache[self.cache_key]
            if now - ts < CACHE_TTL:
                return data
        if now < RATE_LIMIT_UNTIL:
            if self.cache_key in weather_cache:
                return weather_cache[self.cache_key][0]
            return None
        async with aiohttp.ClientSession() as session:
            res = await self.fetch_open_meteo(session)
            if not res and datetime.now().timestamp() >= RATE_LIMIT_UNTIL:
                res = await self.fetch_open_meteo(session, "ecmwf_ifs04")
            if not res:
                if self.cache_key in weather_cache:
                    return weather_cache[self.cache_key][0]
                return None
            weather_cache[self.cache_key] = (res, now)
            return res

    def _pressure_score(self, pressure_mm: float, is_predator: bool) -> int:
        optimum = 748 if is_predator else 752
        diff = abs(pressure_mm - optimum)
        if diff <= 3: return 14
        elif diff <= 6: return 8
        elif diff <= 10: return 0
        elif diff <= 15: return -10
        return -18

    def _pressure_trend_score(self, pressures: list, idx: int) -> Tuple[str, int]:
        if idx < 24: return "Недостатньо даних", 0
        recent = [p for p in pressures[idx - 12:idx + 1] if p is not None]
        older = [p for p in pressures[idx - 24:idx - 12] if p is not None]
        if len(recent) < 5 or len(older) < 5: return "Недостатньо даних", 0
        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older)
        delta = (avg_recent - avg_older) * 0.75006
        if delta < -2.5: return "📉 Сильно падає", 12
        elif delta < -0.8: return "📉 Повільно падає", 8
        elif delta > 2.5: return "📈 Сильно росте", -6
        elif delta > 0.8: return "📈 Повільно росте", 2
        return "✅ Стабільний", 10

    def _stability_score(self, pressures: list, idx: int) -> Tuple[str, int]:
        if idx < 48: return "Недостатньо історії", 0
        valid = [p for p in pressures[idx - 48:idx + 1] if p is not None]
        if len(valid) < 20: return "Недостатньо даних", 0
        diff = max(valid) - min(valid)
        if diff <= 4: return "✅ Дуже стабільний", 12
        elif diff <= 7: return "✅ Стабільний", 6
        elif diff <= 11: return "⚠️ Помірно мінливий", -4
        return "❌ Стрибкоподібний", -16

    def _temperature_score(self, temp: float, water_temp: float, is_predator: bool) -> int:
        if is_predator:
            if 8 <= water_temp <= 16: return 12
            elif 5 <= water_temp <= 20: return 6
            elif water_temp > 24 or water_temp < 3: return -10
            return 0
        else:
            if 16 <= water_temp <= 23: return 12
            elif 12 <= water_temp <= 26: return 6
            elif water_temp > 28 or water_temp < 8: return -8
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
        if precip <= 0.1: return 0
        elif 0.2 <= precip <= 1.8: return 7 if is_predator else 4
        elif precip <= 3.5: return -6
        return -16

    def _cloud_score(self, cloud: float, is_predator: bool) -> int:
        if is_predator:
            if cloud >= 70: return 9
            elif cloud >= 40: return 4
            return -3
        else:
            if cloud >= 80: return 2
            elif cloud <= 30: return 3
            return 0

    def calculate_star_score(self, score_100: float) -> int:
        if score_100 >= 84: return 5
        elif score_100 >= 68: return 4
        elif score_100 >= 50: return 3
        elif score_100 >= 32: return 2
        elif score_100 > 12: return 1
        return 0

    def generate_expert_commentary(
        self, fish_type, pressure_mm, trend_text, stability_text,
        wind_ms, wind_dir, precip, sun_title, sun_desc,
        temp, water_temp, score, humidity, cloud_cover, moon_text,
        comfort_index
    ):
        is_pred = fish_type in ["Щука", "Окунь", "Сом"]
        comments = []
        comments.append(f"⏱ <b>Час:</b> {sun_title}. {sun_desc}")
        comments.append(f"🌕 <b>Місяць:</b> {moon_text}")
        comments.append(f"🌀 <b>Тиск:</b> {pressure_mm} мм | {trend_text} | {stability_text}")
        comments.append(f"🌡 <b>Температура:</b> повітря {temp}°C, вода ~{water_temp}°C")
        if water_temp > 25:
            comments.append("   • Спека — шукайте тінь, глибину, течію.")
        elif water_temp < 9:
            comments.append("   • Холодна вода — дрібні наживки, повільна подача.")
        if wind_ms < 2:
            comments.append(f"💨 <b>Вітер:</b> штиль ({wind_ms} м/с, {wind_dir}). Делікатне оснащення.")
        elif wind_ms <= 6:
            comments.append(f"💨 <b>Вітер:</b> сприятливий ({wind_ms} м/с, {wind_dir}).")
        else:
            comments.append(f"💨 <b>Вітер:</b> сильний ({wind_ms} м/с, {wind_dir}). Шукайте підвітряний берег.")
        if precip > 1.5:
            comments.append(f"🌧 <b>Опади:</b> {precip} мм — добре для сома, щуки, великого ляща.")
        elif cloud_cover > 65:
            comments.append(f"☁️ <b>Хмарність:</b> {cloud_cover}% — сприятливо для хижака.")
        comfort_emoji = "🟢" if comfort_index >= 70 else "🟡" if comfort_index >= 40 else "🔴"
        comments.append(f"🌤 <b>Індекс комфорту:</b> {comfort_index}/100 {comfort_emoji}")

        bait_rec = get_bait_recommendation(fish_type, water_temp, wind_ms, is_pred)
        comments.append(f"🎣 <b>Рекомендовані наживки:</b> {bait_rec}")

        if is_pred:
            comments.append(f"🎯 <b>Для {fish_type}:</b> активні проводки на брівках і перепадах.")
            if water_temp < 10:
                comments.append("   • Холодна вода – уповільніть проводку.")
            elif water_temp > 22:
                comments.append("   • Спека – шукайте глибокі ями, тінь.")
            if wind_ms > 6:
                comments.append("   • Вітер – використовуйте коливання на межі течії.")
        else:
            comments.append(f"🎯 <b>Для {fish_type}:</b> дрібна фракція + мотиль/опариш/кукурудза.")
            if water_temp < 12:
                comments.append("   • Холодна вода – мінімальна активність, годувати точково.")
            elif water_temp > 24:
                comments.append("   • Спека – годуйте на глибині, використовуйте рослинні наживки.")
            if wind_ms < 2:
                comments.append("   • Штиль – обережна риба, легке оснащення.")

        if score >= 78:
            comments.append("\n🏆 <b>Підсумок:</b> Відмінні умови! Вирушайте на водойму.")
        elif score >= 55:
            comments.append("\n⚖️ <b>Підсумок:</b> Добрі умови. Успіх залежить від місця і наживки.")
        else:
            comments.append("\n⚠️ <b>Підсумок:</b> Складні умови. Потрібні майстерність і терпіння.")

        return "\n".join(comments)

    async def evaluate_biting(self, fish_type: str, region: str, target_hour: int, day_offset: int = 0):
        data = await self.get_averaged_weather()
        if not data:
            return None

        hourly = data["hourly"]
        pressures = hourly["surface_pressure"]
        max_idx = len(pressures) - 1
        target_index = min(48 + day_offset * 24 + target_hour, max_idx)

        def safe(val, default):
            return val if val is not None else default

        pressure_hpa = safe(pressures[target_index], 1013.25)
        pressure_mm = pressure_hpa * 0.75006
        wind_ms = safe(hourly["wind_speed_10m"][target_index], 2.5)
        temp = safe(hourly["temperature_2m"][target_index], 18.0)
        precip = safe(hourly["precipitation"][target_index], 0.0)
        wind_dir = get_wind_direction_text(hourly["wind_direction_10m"][target_index])
        humidity = safe(hourly["relative_humidity_2m"][target_index], 55)
        cloud_cover = safe(hourly["cloud_cover"][target_index], 40)

        water_list = hourly.get("sea_surface_temperature", [None] * len(pressures))
        water_temp = safe(water_list[target_index], round(temp * 0.82 + 3.2, 1))

        is_predator = fish_type in ["Щука", "Окунь", "Сом"]

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

        comfort = 50
        if 15 <= temp <= 25:
            comfort += 20
        elif 10 <= temp <= 30:
            comfort += 10
        else:
            comfort -= 10
        if wind_ms <= 5:
            comfort += 15
        elif wind_ms <= 8:
            comfort += 5
        else:
            comfort -= 15
        if precip < 1:
            comfort += 15
        elif precip < 3:
            comfort += 5
        else:
            comfort -= 10
        comfort_index = min(100, max(0, comfort))

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
            final_score, round(humidity), round(cloud_cover), moon_text,
            comfort_index
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
            "sun_activity": f"{sun_title} – {sun_desc}",
            "comfort_index": comfort_index,
        }

# ====================== ГЕНЕРАЦІЯ ЗОБРАЖЕННЯ ======================
async def generate_forecast_image(result, fish_type, region):
    try:
        img = Image.new('RGB', (800, 500), color=(240, 248, 255))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arial.ttf", 20)
            font_bold = ImageFont.truetype("arialbd.ttf", 24)
            font_small = ImageFont.truetype("arial.ttf", 16)
        except:
            font = ImageFont.load_default()
            font_bold = font
            font_small = font

        draw.rectangle([(0, 0), (800, 500)], fill=(240, 248, 255))
        draw.text((30, 20), f"🎣 Прогноз кльову", font=font_bold, fill=(0, 0, 139))
        draw.text((30, 55), f"📍 {region} | {result['forecast_day']} | {result['hour']:02d}:00", font=font, fill=(70, 70, 70))

        stars_text = "⭐" * result['stars'] + "☆" * (5 - result['stars'])
        draw.text((30, 95), f"Оцінка: {result['stars']}/5 {stars_text}", font=font_bold, fill=(255, 140, 0))

        y = 140
        draw.text((30, y), f"🌡 Температура: {result['temperature']}°C", font=font, fill=(0, 0, 0))
        y += 30
        draw.text((30, y), f"💧 Вода: ~{result['water_temp']}°C", font=font, fill=(0, 0, 0))
        y += 30
        draw.text((30, y), f"🌀 Тиск: {result['pressure_mm']} мм", font=font, fill=(0, 0, 0))
        y += 30
        draw.text((30, y), f"💨 Вітер: {result['wind_ms']} м/с ({result['wind_dir']})", font=font, fill=(0, 0, 0))
        y += 30
        draw.text((30, y), f"☁️ Хмарність: {result['cloud_cover']}%  |  🌧 Опади: {result['precipitation']} мм", font=font, fill=(0, 0, 0))
        y += 30
        draw.text((30, y), f"🌙 {result['moon_phase']}", font=font, fill=(0, 0, 0))
        y += 30
        draw.text((30, y), f"🌤 Комфорт: {result['comfort_index']}/100", font=font, fill=(0, 0, 0))

        y += 40
        draw.text((30, y), f"🎯 Рекомендовані наживки: {get_bait_recommendation(fish_type, result['water_temp'], result['wind_ms'], fish_type in ['Щука','Окунь','Сом'])}", font=font_small, fill=(50, 50, 50))

        draw.line([(30, y+30), (770, y+30)], fill=(200, 200, 200), width=2)

        y += 50
        score = result['score_100']
        if score >= 80:
            verdict = "🏆 Відмінні умови! Вирушайте!"
            color = (0, 128, 0)
        elif score >= 60:
            verdict = "⚖️ Добрі умови. Успіх залежить від місця."
            color = (255, 165, 0)
        else:
            verdict = "⚠️ Складні умови. Потрібна майстерність."
            color = (255, 0, 0)
        draw.text((30, y), verdict, font=font_bold, fill=color)

        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        return img_bytes.getvalue()
    except Exception as e:
        logging.error(f"Помилка генерації зображення: {e}")
        return None

# ====================== КЛАВІАТУРИ ======================
def get_regions_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Дніпропетровська"), KeyboardButton(text="Київська")],
            [KeyboardButton(text="Полтавська"), KeyboardButton(text="Запорізька")],
            [KeyboardButton(text="Черкаська")],
            [KeyboardButton(text="📜 Моя історія"), KeyboardButton(text="ℹ️ Допомога")],
            [KeyboardButton(text="📍 Моє місце"), KeyboardButton(text="🏠 Головне меню")],
            [KeyboardButton(text="🗺️ Водойми"), KeyboardButton(text="🎯 Мої трофеї")],
            [KeyboardButton(text="🔔 Підписка"), KeyboardButton(text="🗓 Сезон")],
            [KeyboardButton(text="🌐 Змінити мову")],
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

# ВИПРАВЛЕНО: правильне створення клавіатури з рядками по 4 кнопки
def get_hour_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    row = []
    for i in range(24):
        row.append(InlineKeyboardButton(text=str(i), callback_data=f"hour_{i}"))
        if len(row) == 4:
            kb.inline_keyboard.append(row)
            row = []
    if row:
        kb.inline_keyboard.append(row)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад (до дня)", callback_data="back_to_day")])
    return kb

def get_language_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang_uk")],
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
    ])

# ====================== ОБРОБНИКИ ======================
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
    await state.clear()
    await state.set_state(ForecastStates.choosing_region)
    await message.answer(
        _["start"],
        reply_markup=get_regions_keyboard(),
        parse_mode="HTML",
    )

@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Допомога")
async def cmd_help(message: Message):
    _ = get_lang_dict(message.from_user.id)
    await message.answer(_["help"], parse_mode="HTML")

@dp.message(F.text == "📜 Моя історія")
async def show_history(message: Message):
    _ = get_lang_dict(message.from_user.id)
    rows = get_user_history_from_db(message.from_user.id)
    if not rows:
        await message.answer(_["history_empty"])
        return
    text = "<b>📜 Ваші останні прогнози:</b>\n\n"
    for row in rows:
        region, fish, day, hour, stars, ts = row
        graphic = "⭐" * (stars or 0) + "☆" * (5 - (stars or 0))
        hour_str = f"{hour:02d}:00" if hour is not None else "—"
        text += f"📍 {region} | 🎣 {fish}\n{day} о {hour_str}\n{graphic}\n🕒 {ts}\n\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "🏠 Головне меню")
async def main_menu(message: Message, state: FSMContext):
    await cmd_start(message, state)

@dp.message(F.text == "📍 Моє місце")
async def ask_location(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Надіслати геолокацію", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Будь ласка, надішліть вашу геолокацію, натиснувши кнопку нижче.",
        reply_markup=kb,
    )

@dp.message(F.location)
async def handle_location(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
    location: Location = message.location
    lat, lon = location.latitude, location.longitude
    region = find_nearest_region(lat, lon)
    if not region:
        await message.answer(
            "❌ Не вдалося визначити область. Спробуйте вибрати вручну.",
            reply_markup=get_regions_keyboard(),
        )
        return
    await state.update_data(region=region)
    await state.set_state(ForecastStates.choosing_fish)
    await message.answer(
        f"📍 Ваша область: <b>{region}</b>\nТепер оберіть рибу:",
        reply_markup=get_fish_keyboard(),
        parse_mode="HTML",
    )

@dp.message(F.text.in_(REGIONS.keys()))
async def handle_region(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
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
    _ = get_lang_dict(message.from_user.id)
    data = await state.get_data()
    if "region" not in data:
        await message.answer("Спочатку оберіть область через /start")
        return

    await state.update_data(fish=message.text)
    await state.set_state(ForecastStates.choosing_day)

    today = datetime.now()
    buttons = []
    for i in range(3):
        d = today + timedelta(days=i)
        label = {
            0: f"📅 Сьогодні ({d.strftime('%d.%m')})",
            1: f"📅 Завтра ({d.strftime('%d.%m')})",
            2: f"📅 Післязавтра ({d.strftime('%d.%m')})",
        }[i]
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"day_{i}")])
    buttons.append([InlineKeyboardButton(text="▶️ Ввести будь-яку годину", callback_data="manual_hour")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад (до риби)", callback_data="back_to_fish")])

    await message.answer(
        f"🎣 Риба: <b>{message.text}</b>\nОберіть день або введіть будь-яку годину:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )

@dp.callback_query(F.data == "back_to_fish")
async def handle_back_to_fish(callback: CallbackQuery, state: FSMContext):
    _ = get_lang_dict(callback.from_user.id)
    await state.set_state(ForecastStates.choosing_fish)
    await callback.message.edit_text("Оберіть рибу за допомогою кнопок нижче 👇")
    await callback.answer()

@dp.callback_query(F.data == "manual_hour")
async def manual_hour_start(callback: CallbackQuery, state: FSMContext):
    _ = get_lang_dict(callback.from_user.id)
    await state.set_state(ForecastStates.choosing_hour_manual)
    await callback.message.edit_text(
        _["choose_hour"],
        reply_markup=get_hour_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("day_"))
async def handle_day(callback: CallbackQuery, state: FSMContext):
    _ = get_lang_dict(callback.from_user.id)
    day_offset = int(callback.data.split("_")[1])
    await state.update_data(day_offset=day_offset)
    await state.set_state(ForecastStates.choosing_hour)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌅 Світанок (06:00)", callback_data="hour_6")],
        [InlineKeyboardButton(text="☀️ День (12:00)", callback_data="hour_12")],
        [InlineKeyboardButton(text="🌇 Захід (20:00)", callback_data="hour_20")],
        [InlineKeyboardButton(text="▶️ Ввести будь-яку годину", callback_data="manual_hour")],
        [InlineKeyboardButton(text="◀️ Назад (до дня)", callback_data="back_to_day")],
    ])
    await callback.message.edit_text(_["choose_hour"], reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "back_to_day")
async def handle_back_to_day(callback: CallbackQuery, state: FSMContext):
    _ = get_lang_dict(callback.from_user.id)
    await state.set_state(ForecastStates.choosing_day)
    data = await state.get_data()
    fish_type = data.get("fish", "Рибу")

    today = datetime.now()
    buttons = []
    for i in range(3):
        d = today + timedelta(days=i)
        label = {
            0: f"📅 Сьогодні ({d.strftime('%d.%m')})",
            1: f"📅 Завтра ({d.strftime('%d.%m')})",
            2: f"📅 Післязавтра ({d.strftime('%d.%m')})",
        }[i]
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"day_{i}")])
    buttons.append([InlineKeyboardButton(text="▶️ Ввести будь-яку годину", callback_data="manual_hour")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад (до риби)", callback_data="back_to_fish")])

    await callback.message.edit_text(
        f"🎣 Риба: <b>{fish_type}</b>\nОберіть день:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("hour_"))
async def handle_hour(callback: CallbackQuery, state: FSMContext):
    _ = get_lang_dict(callback.from_user.id)
    hour = int(callback.data.split("_")[1])
    data = await state.get_data()

    region = data.get("region", "Дніпропетровська")
    fish_type = data.get("fish", "Лящ")
    day_offset = data.get("day_offset", 0)

    coords = REGIONS[region]
    client = MultiSourceWeatherClient(coords["lat"], coords["lon"])

    await callback.message.edit_text(_["processing"])

    result = await client.evaluate_biting(fish_type, region, hour, day_offset)

    if not result:
        await callback.message.answer(
            _["rate_limit"],
            reply_markup=get_regions_keyboard(),
            parse_mode="HTML"
        )
        await state.clear()
        await callback.answer()
        return

    forecast_id = save_forecast_to_db(
        callback.from_user.id, region, fish_type,
        result["forecast_day"], result["hour"],
        result["pressure_mm"], result["wind_ms"],
        result["temperature"], result["stars"],
    )

    image_data = await generate_forecast_image(result, fish_type, region)

    stars_bar = "⭐" * result["stars"] + "☆" * (5 - result["stars"])
    score = result["score_100"]
    if score >= 80:
        grade = "🟢 Відмінно"
    elif score >= 60:
        grade = "🟡 Добре"
    elif score >= 40:
        grade = "🟠 Посередньо"
    else:
        grade = "🔴 Погано"

    response = (
        f"🎣 <b>Прогноз кльову</b>\n"
        f"📍 {region} | {result['forecast_day']}\n"
        f"⏰ {result['hour']:02d}:00\n"
        f"🐟 <b>{fish_type}</b>\n\n"
        f"⭐ <b>Оцінка:</b> {result['stars']}/5 {stars_bar}\n"
        f"📊 {grade} (бал: {score}/100)\n\n"
        f"🌡 <b>Погода:</b>\n"
        f"• Температура повітря: {result['temperature']}°C\n"
        f"• Вода: ~{result['water_temp']}°C\n"
        f"• Тиск: {result['pressure_mm']} мм рт.ст.\n"
        f"  {result['pressure_trend']} | {result['pressure_stability']}\n"
        f"• Вітер: {result['wind_ms']} м/с ({result['wind_dir']})\n"
        f"• Вологість: {result['humidity']}% | Хмарність: {result['cloud_cover']}%\n"
        f"• Опади: {result['precipitation']} мм\n"
        f"🌙 {result['moon_phase']}\n"
        f"☀️ {result['sun_activity']}\n"
        f"🌤 Комфорт: {result['comfort_index']}/100\n\n"
        f"💡 <b>Рекомендації:</b>\n{result['expert_commentary']}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Поділитися в чаті", callback_data=f"share_{result['stars']}_{fish_type}_{region}")],
        [InlineKeyboardButton(text="💬 Перейти в чат", url=GROUP_URL)],
        [
            InlineKeyboardButton(text="👍 Точний", callback_data=f"fb_good_{forecast_id}"),
            InlineKeyboardButton(text="👎 Хибний", callback_data=f"fb_bad_{forecast_id}"),
        ],
    ])

    # ВИПРАВЛЕНО: використання BufferedInputFile для передачі байтів
    if image_data:
        photo_file = BufferedInputFile(image_data, filename="forecast.png")
        await callback.message.answer_photo(photo=photo_file, caption=response, reply_markup=kb, parse_mode="HTML")
    else:
        await callback.message.answer(response, reply_markup=kb, parse_mode="HTML")

    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.startswith("fb_"))
async def handle_feedback(callback: CallbackQuery):
    _ = get_lang_dict(callback.from_user.id)
    parts = callback.data.split("_")
    rating = parts[1]
    forecast_id = int(parts[2])
    save_feedback_to_db(callback.from_user.id, forecast_id, rating)
    msg = _["feedback_good"] if rating == "good" else _["feedback_bad"]
    await callback.answer(msg, show_alert=True)

@dp.callback_query(F.data.startswith("share_"))
async def handle_share(callback: CallbackQuery):
    _ = get_lang_dict(callback.from_user.id)
    try:
        _, stars, fish, region = callback.data.split("_", 3)
        graphic = "⭐" * int(stars) + "☆" * (5 - int(stars))
        text = _["share"].format(
            name=callback.from_user.first_name,
            region=region,
            fish=fish,
            stars=stars,
            graphic=graphic
        )
        await bot.send_message(GROUP_CHAT_ID, text, parse_mode="HTML")
        await callback.answer("✅ Надіслано в чат клубу!", show_alert=True)
    except Exception as e:
        logging.error(f"Share error: {e}")
        await callback.answer("❌ Помилка відправки", show_alert=True)

# ------ ПІДПИСКА ------
@dp.message(F.text == "🔔 Підписка")
async def subscribe_start(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
    await state.set_state(SubscribeStates.region)
    await message.answer("Оберіть область для підписки:", reply_markup=get_regions_keyboard())

@dp.message(SubscribeStates.region, F.text.in_(REGIONS.keys()))
async def subscribe_region(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
    await state.update_data(region=message.text)
    await state.set_state(SubscribeStates.fish)
    await message.answer("Оберіть рибу:", reply_markup=get_fish_keyboard())

@dp.message(SubscribeStates.fish, F.text.in_(FISH_LIST))
async def subscribe_fish(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
    await state.update_data(fish=message.text)
    await state.set_state(SubscribeStates.hour)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌅 6:00", callback_data="sub_hour_6"),
         InlineKeyboardButton(text="☀️ 8:00", callback_data="sub_hour_8"),
         InlineKeyboardButton(text="🌇 19:00", callback_data="sub_hour_19")]
    ])
    await message.answer("Оберіть час надсилання:", reply_markup=kb)

@dp.callback_query(F.data.startswith("sub_hour_"))
async def subscribe_hour(callback: CallbackQuery, state: FSMContext):
    _ = get_lang_dict(callback.from_user.id)
    hour = int(callback.data.split("_")[2])
    data = await state.get_data()
    add_subscription(callback.from_user.id, data['region'], data['fish'], hour)
    await callback.message.edit_text(
        _["subscribe_done"].format(region=data['region'], fish=data['fish'], hour=hour)
    )
    await state.clear()
    await callback.answer()

# ------ ВОДОЙМИ ------
@dp.message(F.text == "🗺️ Водойми")
async def show_water_bodies(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
    data = await state.get_data()
    region = data.get("region")
    if not region:
        await message.answer("Спочатку оберіть область через /start або натисніть «📍 Моє місце».")
        return
    bodies = WATER_BODIES.get(region, [])
    if not bodies:
        await message.answer("Немає даних про водойми в цій області.")
        return
    text = f"🗺️ <b>Водойми в {region}:</b>\n"
    for b in bodies:
        url = f"https://www.google.com/maps?q={b['lat']},{b['lon']}"
        text += f"• <a href='{url}'>{b['name']}</a>\n"
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

# ------ СЕЗОН ------
@dp.message(F.text == "🗓 Сезон")
async def show_season(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
    data = await state.get_data()
    fish = data.get("fish")
    if not fish:
        await message.answer("Спочатку оберіть рибу в меню прогнозу.")
        return
    status_text, season_desc = get_season_status(fish)
    season_info = f"{SPAWNING[fish]['start']}-{SPAWNING[fish]['end']} місяці" if fish in SPAWNING else "немає даних"
    await message.answer(
        _["season_info"].format(fish=fish, season=season_info, status=status_text),
        parse_mode="HTML"
    )

# ------ ТРОФЕЇ ------
@dp.message(F.text == "🎯 Мої трофеї")
async def my_trophies(message: Message, state: FSMContext):
    _ = get_lang_dict(message.from_user.id)
    rows = get_user_catches(message.from_user.id)
    if not rows:
        await message.answer("У вас поки немає записаних уловів.\nНадішліть /add_catch або скористайтеся кнопкою нижче.")
        return
    text = "🏆 <b>Ваші трофеї:</b>\n\n"
    for fish, weight, length, location, photo, date in rows:
        text += f"• {fish} | {weight} г, {length} см\n"
        text += f"  📍 {location} | 🗓 {date}\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("add_catch"))
async def add_catch_start(message: Message, state: FSMContext):
    await state.set_state(TrophyStates.waiting_fish)
    await message.answer("Введіть назву риби:")

@dp.message(TrophyStates.waiting_fish)
async def add_catch_fish(message: Message, state: FSMContext):
    await state.update_data(fish=message.text)
    await state.set_state(TrophyStates.waiting_weight)
    await message.answer("Введіть вагу (в грамах):")

@dp.message(TrophyStates.waiting_weight)
async def add_catch_weight(message: Message, state: FSMContext):
    try:
        weight = float(message.text)
        await state.update_data(weight=weight)
        await state.set_state(TrophyStates.waiting_length)
        await message.answer("Введіть довжину (в см):")
    except ValueError:
        await message.answer("Будь ласка, введіть число.")

@dp.message(TrophyStates.waiting_length)
async def add_catch_length(message: Message, state: FSMContext):
    try:
        length = float(message.text)
        await state.update_data(length=length)
        await state.set_state(TrophyStates.waiting_location)
        await message.answer("Введіть місце ловлі (назва водойми):")
    except ValueError:
        await message.answer("Будь ласка, введіть число.")

@dp.message(TrophyStates.waiting_location)
async def add_catch_location(message: Message, state: FSMContext):
    await state.update_data(location=message.text)
    await state.set_state(TrophyStates.waiting_photo)
    await message.answer("Надішліть фото (або натисніть /skip_photo)")

@dp.message(Command("skip_photo"))
async def skip_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    add_catch(message.from_user.id, data['fish'], data['weight'], data['length'], data['location'], None, datetime.now().date().isoformat())
    await state.clear()
    await message.answer("✅ Трофей збережено!")

@dp.message(TrophyStates.waiting_photo, F.photo)
async def add_catch_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    file_id = photo.file_id
    data = await state.get_data()
    add_catch(message.from_user.id, data['fish'], data['weight'], data['length'], data['location'], file_id, datetime.now().date().isoformat())
    await state.clear()
    await message.answer("✅ Трофей збережено з фото!")

# ------ МОВА ------
@dp.message(F.text == "🌐 Змінити мову")
async def change_language(message: Message, state: FSMContext):
    await state.set_state(LanguageStates.choose_language)
    await message.answer("Оберіть мову / Выберите язык:", reply_markup=get_language_keyboard())

@dp.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery, state: FSMContext):
    lang = callback.data.split("_")[1]
    set_user_lang(callback.from_user.id, lang)
    await callback.message.edit_text(f"Мову змінено на {'українську' if lang=='uk' else 'російську'}.")
    await state.clear()
    await callback.answer()

# ------ ЗАГАЛЬНИЙ ОБРОБНИК (fallback) ------
@dp.message()
async def fallback(message: Message, state: FSMContext):
    if await state.get_state() is None:
        _ = get_lang_dict(message.from_user.id)
        await message.answer("Натисніть /start", reply_markup=get_regions_keyboard())

# ====================== ФОНОВІ ЗАВДАННЯ ======================
async def check_extreme_weather():
    subs = get_subscriptions()
    for user_id, region, fish_type, _ in subs:
        try:
            coords = REGIONS[region]
            client = MultiSourceWeatherClient(coords["lat"], coords["lon"])
            data = await client.get_averaged_weather()
            if not data:
                continue
            pressures = data["hourly"]["surface_pressure"]
            if len(pressures) >= 12:
                recent = pressures[-12:]
                delta = (recent[-1] - recent[0]) * 0.75006 if recent[-1] and recent[0] else 0
                if delta < -5:
                    _ = get_lang_dict(user_id)
                    await bot.send_message(user_id, _["weather_alert"].format(delta=delta), parse_mode="HTML")
        except Exception as e:
            logging.error(f"Помилка штормового попередження для {user_id}: {e}")

async def send_daily_forecasts():
    subs = get_subscriptions()
    for user_id, region, fish_type, hour in subs:
        try:
            coords = REGIONS[region]
            client = MultiSourceWeatherClient(coords["lat"], coords["lon"])
            result = await client.evaluate_biting(fish_type, region, hour, day_offset=0)
            if result:
                _ = get_lang_dict(user_id)
                text = f"🌅 <b>Ранковий прогноз</b>\n{region} | {fish_type}\n⭐ {result['stars']}/5\nПогода: {result['temperature']}°C, вітер {result['wind_ms']} м/с\nСьогодні о {hour:02d}:00"
                await bot.send_message(user_id, text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Помилка розсилки підписки {user_id}: {e}")

# ====================== ЗАПУСК ======================
async def health(_):
    return web.Response(text="Fishing bot is running ✅")

async def main():
    init_db()
    logging.basicConfig(level=logging.INFO)

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Health server started on port {port}")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_daily_forecasts, CronTrigger(hour=7, minute=0))
    scheduler.add_job(check_extreme_weather, CronTrigger(hour=12, minute=0))
    scheduler.start()

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
