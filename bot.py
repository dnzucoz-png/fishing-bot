import asyncio
import io
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, Location, BufferedInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageDraw, ImageFont


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "-1004434293069"))
GROUP_URL = os.getenv("GROUP_URL", "https://t.me/+rKxYkNg85aAwNzFi")
DB_FILE = os.getenv("DB_FILE", "fishing_forecast.db")
PORT = int(os.getenv("PORT", "10000"))

CACHE_TTL = 2 * 60 * 60
RATE_LIMIT_COOLDOWN = 10 * 60
HTTP_TIMEOUT = 20

# Области используются только как fallback/география.
REGIONS = {
    "Дніпропетровська": {"lat": 48.4647, "lon": 35.0462},
    "Київська": {"lat": 50.4501, "lon": 30.5234},
    "Полтавська": {"lat": 49.5895, "lon": 34.5514},
    "Запорізька": {"lat": 47.8388, "lon": 35.1396},
    "Черкаська": {"lat": 49.4444, "lon": 32.0598},
}

# ВАЖНО:
# Прогноз теперь берётся по координатам выбранной ВОДОЁМЫ,
# а не по центру области.
#
# Кам'янське водосховище:
# 48.81421, 34.09400 — координата из актуальной записи
# по использованию водных биоресурсов в Днепропетровской области.
#
# Для Каховского водохранилища старые координаты 47.5,34.2
# здесь намеренно НЕ используются как координата Кам'янского.
WATER_BODIES = {
    "Дніпропетровська": [
        {
            "name": "Кам'янське водосховище",
            "lat": 48.81421,
            "lon": 34.09400,
            "description": "район Кам'янського водосховища",
        },
        {
            "name": "Дніпро",
            "lat": 48.51716,
            "lon": 34.60617,
            "description": "акваторія Дніпра в межах Кам'янського",
        },
        {
            "name": "Самарська затока",
            "lat": 48.60000,
            "lon": 35.20000,
            "description": "Самарська затока",
        },
        {
            "name": "Каховське водосховище",
            "lat": 47.56200,
            "lon": 34.89900,
            "description": "історична акваторія; координати не є Кам'янським водосховищем",
        },
    ],
    "Київська": [
        {"name": "Київське водосховище", "lat": 50.8000, "lon": 30.5000, "description": ""},
        {"name": "Дніпро біля Києва", "lat": 50.4501, "lon": 30.5234, "description": ""},
        {"name": "Озеро Святе", "lat": 50.5000, "lon": 30.7000, "description": ""},
    ],
    "Полтавська": [
        {"name": "Кременчуцьке водосховище", "lat": 49.1000, "lon": 33.4000, "description": ""},
        {"name": "Річка Псел", "lat": 49.6000, "lon": 34.5000, "description": ""},
        {"name": "Кам'янське водосховище", "lat": 49.0000, "lon": 33.6000, "description": ""},
    ],
    "Запорізька": [
        {"name": "Дніпровське водосховище", "lat": 47.8000, "lon": 35.1000, "description": ""},
        {"name": "Дніпро", "lat": 47.8388, "lon": 35.1396, "description": ""},
        {"name": "Річка Молочна", "lat": 47.2000, "lon": 35.3000, "description": ""},
    ],
    "Черкаська": [
        {"name": "Кременчуцьке водосховище", "lat": 49.3000, "lon": 32.0000, "description": ""},
        {"name": "Дніпро біля Черкас", "lat": 49.4000, "lon": 32.0600, "description": ""},
    ],
}

FISH_LIST = ["Лящ", "Карась", "Короп", "Щука", "Окунь", "Сом", "Плотва"]
PREDATORS = {"Щука", "Окунь", "Сом"}

SPAWNING = {
    "Лящ": (4, 6),
    "Щука": (3, 4),
    "Окунь": (4, 5),
    "Сом": (5, 7),
    "Короп": (5, 6),
    "Карась": (5, 7),
    "Плотва": (4, 6),
}


# ============================================================
# TEXTS
# ============================================================

LANG = {
    "uk": {
        "start": (
            "🎣 <b>Fishing Forecast</b>\n\n"
            "Оберіть область. Потім оберіть конкретну водойму — "
            "прогноз погоди буде рахуватися саме для її координат."
        ),
        "help": (
            "<b>Як рахується прогноз:</b>\n\n"
            "• тиск і його зміна;\n"
            "• стабільність тиску за останні 48 годин;\n"
            "• температура повітря;\n"
            "• орієнтовна температура води;\n"
            "• вітер, напрямок, хмарність та опади;\n"
            "• час доби та місячна фаза;\n"
            "• окремі коефіцієнти для хижака і мирної риби.\n\n"
            "Дані погоди: Open-Meteo.\n"
            "Кеш погоди: 2 години, щоб не ловити 429."
        ),
        "processing": "⏳ Аналізую погоду саме для обраної водойми...",
        "rate": (
            "❌ Open-Meteo тимчасово обмежив запити.\n"
            "Старий кеш буде використаний автоматично, якщо він є.\n"
            "Спробуйте трохи пізніше."
        ),
        "history_empty": "У вас поки немає збережених прогнозів.",
        "subscribe_done": "✅ Підписку збережено.",
    },
    "ru": {
        "start": (
            "🎣 <b>Fishing Forecast</b>\n\n"
            "Выберите область. Затем конкретный водоём — "
            "погода будет рассчитываться именно по его координатам."
        ),
        "help": (
            "<b>Как считается прогноз:</b>\n\n"
            "• давление и его изменение;\n"
            "• стабильность давления за 48 часов;\n"
            "• температура воздуха;\n"
            "• ориентировочная температура воды;\n"
            "• ветер, направление, облачность и осадки;\n"
            "• время суток и фаза Луны;\n"
            "• отдельные коэффициенты для хищника и мирной рыбы.\n\n"
            "Источник погоды: Open-Meteo.\n"
            "Кэш погоды: 2 часа, чтобы не получать 429."
        ),
        "processing": "⏳ Анализирую погоду именно для выбранного водоёма...",
        "rate": (
            "❌ Open-Meteo временно ограничил запросы.\n"
            "Если есть старый кэш — он будет использован автоматически.\n"
            "Попробуйте немного позже."
        ),
        "history_empty": "У вас пока нет сохранённых прогнозов.",
        "subscribe_done": "✅ Подписка сохранена.",
    },
}


def T(user_id: int, key: str) -> str:
    lang = get_user_lang(user_id)
    return LANG.get(lang, LANG["uk"]).get(key, key)


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            region TEXT,
            water_body TEXT,
            latitude REAL,
            longitude REAL,
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            forecast_id INTEGER,
            rating TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            region TEXT,
            water_body TEXT,
            latitude REAL,
            longitude REAL,
            fish_type TEXT,
            hour INTEGER DEFAULT 7,
            enabled INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_language (
            user_id INTEGER PRIMARY KEY,
            lang TEXT DEFAULT 'uk'
        )
    """)

    cur.execute("""
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

    # Миграция старой базы.
    existing = {row["name"] for row in cur.execute("PRAGMA table_info(forecasts)")}
    for name, sql in {
        "water_body": "ALTER TABLE forecasts ADD COLUMN water_body TEXT",
        "latitude": "ALTER TABLE forecasts ADD COLUMN latitude REAL",
        "longitude": "ALTER TABLE forecasts ADD COLUMN longitude REAL",
    }.items():
        if name not in existing:
            try:
                cur.execute(sql)
            except sqlite3.OperationalError:
                pass

    existing = {row["name"] for row in cur.execute("PRAGMA table_info(subscriptions)")}
    for name, sql in {
        "water_body": "ALTER TABLE subscriptions ADD COLUMN water_body TEXT",
        "latitude": "ALTER TABLE subscriptions ADD COLUMN latitude REAL",
        "longitude": "ALTER TABLE subscriptions ADD COLUMN longitude REAL",
    }.items():
        if name not in existing:
            try:
                cur.execute(sql)
            except sqlite3.OperationalError:
                pass

    conn.commit()
    conn.close()


def get_user_lang(user_id: int) -> str:
    conn = db()
    row = conn.execute(
        "SELECT lang FROM user_language WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row["lang"] if row else "uk"


def set_user_lang(user_id: int, lang: str):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO user_language(user_id,lang) VALUES(?,?)",
        (user_id, lang),
    )
    conn.commit()
    conn.close()


def save_forecast(user_id, region, body, fish, result):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO forecasts
        (user_id, region, water_body, latitude, longitude, fish_type,
         forecast_day, hour, pressure, wind, temp, stars)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id, region, body["name"], body["lat"], body["lon"], fish,
        result["forecast_day"], result["hour"], result["pressure_mm"],
        result["wind_ms"], result["temperature"], result["stars"]
    ))
    fid = cur.lastrowid
    conn.commit()
    conn.close()
    return fid


def save_feedback(user_id, forecast_id, rating):
    conn = db()
    conn.execute(
        "INSERT INTO feedback(user_id,forecast_id,rating) VALUES(?,?,?)",
        (user_id, forecast_id, rating),
    )
    conn.commit()
    conn.close()


def get_history(user_id):
    conn = db()
    rows = conn.execute("""
        SELECT region, water_body, fish_type, forecast_day, hour, stars, timestamp
        FROM forecasts
        WHERE user_id=?
        ORDER BY id DESC LIMIT 10
    """, (user_id,)).fetchall()
    conn.close()
    return rows


def save_subscription(user_id, region, body, fish, hour):
    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO subscriptions
        (user_id, region, water_body, latitude, longitude, fish_type, hour, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    """, (
        user_id, region, body["name"], body["lat"], body["lon"], fish, hour
    ))
    conn.commit()
    conn.close()


def get_subscriptions():
    conn = db()
    rows = conn.execute("""
        SELECT user_id, region, water_body, latitude, longitude, fish_type, hour
        FROM subscriptions WHERE enabled=1
    """).fetchall()
    conn.close()
    return rows


def save_catch(user_id, fish, weight, length, location, photo_id):
    conn = db()
    conn.execute("""
        INSERT INTO user_fish_catches
        (user_id, fish_type, weight, length, location, photo_file_id, date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id, fish, weight, length, location, photo_id,
        datetime.now().date().isoformat()
    ))
    conn.commit()
    conn.close()


def get_catches(user_id):
    conn = db()
    rows = conn.execute("""
        SELECT fish_type, weight, length, location, photo_file_id, date
        FROM user_fish_catches
        WHERE user_id=?
        ORDER BY date DESC LIMIT 20
    """, (user_id,)).fetchall()
    conn.close()
    return rows


# ============================================================
# FSM
# ============================================================

class ForecastStates(StatesGroup):
    region = State()
    water_body = State()
    fish = State()
    day = State()
    hour = State()
    manual_hour = State()


class SubscribeStates(StatesGroup):
    region = State()
    water_body = State()
    fish = State()
    hour = State()


class LanguageStates(StatesGroup):
    choose = State()


class TrophyStates(StatesGroup):
    fish = State()
    weight = State()
    length = State()
    location = State()
    photo = State()


# ============================================================
# HELPERS
# ============================================================

def get_wind_direction(deg) -> str:
    if deg is None:
        return "Н/Д"
    dirs = ["Пн", "Пн-Сх", "Сх", "Пд-Сх", "Пд", "Пд-Зх", "Зх", "Пн-Зх"]
    return dirs[round(float(deg) / 45) % 8]


def moon_phase(dt: datetime):
    known = datetime(2024, 1, 11)
    age = (dt.date() - known.date()).days % 29.53
    if age < 1.8:
        return "🌑 Новомісяць", -6
    if age < 7.4:
        return "🌒 Зростаючий", 4
    if age < 11.1:
        return "🌓 Перша чверть", 6
    if age < 16.5:
        return "🌕 Повня", 10
    if age < 22.1:
        return "🌖 Спадаючий", 5
    if age < 25.8:
        return "🌗 Остання чверть", 3
    return "🌘 Старий місяць", -4


def sun_activity(hour):
    if 4 <= hour <= 7:
        return "🌅 Світанок", "ранкова активність", 16
    if 19 <= hour <= 21:
        return "🌇 Захід сонця", "вечірня активність", 14
    if hour >= 22 or hour < 4:
        return "🌙 Ніч", "можливий нічний кльов", 4
    return "☀️ День", "звичайна денна активність", 0


def nearest_region(lat, lon):
    best = None
    best_d = float("inf")
    for name, p in REGIONS.items():
        d = (p["lat"] - lat) ** 2 + (p["lon"] - lon) ** 2
        if d < best_d:
            best_d = d
            best = name
    return best


def body_by_name(region, name):
    for b in WATER_BODIES.get(region, []):
        if b["name"] == name:
            return b
    return None


def bait(fish, water_temp, wind):
    values = {
        "Лящ": ["мотиль", "опариш", "черв'як", "перловка", "кукурудза"],
        "Карась": ["черв'як", "мотиль", "хліб", "горох"],
        "Короп": ["кукурудза", "горох", "бойли", "картопля"],
        "Щука": ["блешня", "воблер", "живець", "силікон"],
        "Окунь": ["вертушка", "твістер", "живець", "черв'як"],
        "Сом": ["живець", "великий черв'як", "м'ясо", "жаба"],
        "Плотва": ["мотиль", "опариш", "тісто", "хліб"],
    }
    result = list(values.get(fish, ["черв'як"]))
    if water_temp < 10:
        result.append("тваринна наживка")
    elif water_temp > 22:
        result.append("рослинна наживка")
    if wind > 6:
        result.append("важча оснастка")
    return ", ".join(dict.fromkeys(result))


# ============================================================
# OPEN-METEO
# ============================================================

weather_cache = {}
rate_limit_until = 0.0


class WeatherClient:
    def __init__(self, lat, lon):
        self.lat = round(float(lat), 5)
        self.lon = round(float(lon), 5)
        self.cache_key = f"{self.lat}:{self.lon}"

    async def fetch(self, session):
        global rate_limit_until

        now = time.time()
        if now < rate_limit_until:
            return None

        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "hourly": (
                "temperature_2m,relative_humidity_2m,"
                "surface_pressure,wind_speed_10m,wind_direction_10m,"
                "cloud_cover,precipitation,apparent_temperature"
            ),
            "past_days": 2,
            "forecast_days": 4,
            "timezone": "auto",
            "wind_speed_unit": "ms",  # ВАЖНО: по умолчанию API отдаёт км/ч
        }

        url = "https://api.open-meteo.com/v1/forecast"

        for attempt in range(2):
            try:
                timeout = aiohttp.ClientTimeout(
                    total=HTTP_TIMEOUT, connect=8
                )
                async with session.get(url, params=params, timeout=timeout) as r:
                    if r.status == 200:
                        return await r.json()

                    if r.status == 429:
                        rate_limit_until = time.time() + RATE_LIMIT_COOLDOWN
                        logging.warning("Open-Meteo 429: включён cooldown")
                        return None

                    logging.warning("Open-Meteo HTTP %s", r.status)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.warning("Open-Meteo error: %s", e)

            if attempt == 0:
                await asyncio.sleep(1.5)

        return None

    async def get(self):
        now = time.time()

        cached = weather_cache.get(self.cache_key)
        if cached and now - cached["timestamp"] < CACHE_TTL:
            return cached["data"]

        if now < rate_limit_until:
            return cached["data"] if cached else None

        async with aiohttp.ClientSession() as session:
            data = await self.fetch(session)

        if data:
            weather_cache[self.cache_key] = {
                "timestamp": now,
                "data": data,
            }
            return data

        return cached["data"] if cached else None

    @staticmethod
    def pressure_score(mm, predator):
        optimum = 748 if predator else 752
        diff = abs(mm - optimum)
        if diff <= 3:
            return 14
        if diff <= 6:
            return 8
        if diff <= 10:
            return 0
        if diff <= 15:
            return -10
        return -18

    @staticmethod
    def pressure_trend(pressures, idx):
        if idx < 24:
            return "Н/Д", 0

        recent = [x for x in pressures[idx - 12:idx + 1] if x is not None]
        old = [x for x in pressures[idx - 24:idx - 12] if x is not None]
        if len(recent) < 5 or len(old) < 5:
            return "Н/Д", 0

        delta = (sum(recent) / len(recent) - sum(old) / len(old)) * 0.75006

        if delta < -2.5:
            return "📉 Сильно падает", 12
        if delta < -0.8:
            return "📉 Медленно падает", 8
        if delta > 2.5:
            return "📈 Сильно растёт", -6
        if delta > 0.8:
            return "📈 Медленно растёт", 2
        return "✅ Стабильный", 10

    @staticmethod
    def pressure_stability(pressures, idx):
        if idx < 48:
            return "Н/Д", 0

        values = [x for x in pressures[idx - 48:idx + 1] if x is not None]
        if len(values) < 20:
            return "Н/Д", 0

        spread = max(values) - min(values)
        if spread <= 4:
            return "✅ Очень стабильный", 12
        if spread <= 7:
            return "✅ Стабильный", 6
        if spread <= 11:
            return "⚠️ Меняется", -4
        return "❌ Резко меняется", -16

    @staticmethod
    def temp_score(water, predator):
        if predator:
            if 8 <= water <= 16:
                return 12
            if 5 <= water <= 20:
                return 6
            if water > 24 or water < 3:
                return -10
            return 0

        if 16 <= water <= 23:
            return 12
        if 12 <= water <= 26:
            return 6
        if water > 28 or water < 8:
            return -8
        return 0

    @staticmethod
    def wind_score(wind, direction, predator):
        # wind уже в м/с благодаря wind_speed_unit=ms
        if wind < 1.5:
            score = -4 if predator else 2
        elif 2 <= wind <= 5.5:
            score = 10
        elif wind <= 7.5:
            score = 2
        elif wind > 9:
            score = -22
        else:
            score = -8

        if direction in {"Пд", "Пд-Зх", "Зх", "Пд-Сх"}:
            score += 4
        elif direction in {"Пн", "Пн-Сх"}:
            score -= 3

        return score

    @staticmethod
    def precip_score(mm, predator):
        if mm <= 0.1:
            return 0
        if mm <= 1.8:
            return 7 if predator else 4
        if mm <= 3.5:
            return -6
        return -16

    @staticmethod
    def cloud_score(cloud, predator):
        if predator:
            if cloud >= 70:
                return 9
            if cloud >= 40:
                return 4
            return -3

        if cloud >= 80:
            return 2
        if cloud <= 30:
            return 3
        return 0

    @staticmethod
    def star_score(score):
        if score >= 84:
            return 5
        if score >= 68:
            return 4
        if score >= 50:
            return 3
        if score >= 32:
            return 2
        if score > 12:
            return 1
        return 0

    @staticmethod
    def nearest_hour_index(times, target):
        if not times:
            return None
        best_i = 0
        best_d = float("inf")
        for i, s in enumerate(times):
            try:
                dt = datetime.fromisoformat(s)
                d = abs((dt - target).total_seconds())
                if d < best_d:
                    best_d = d
                    best_i = i
            except Exception:
                continue
        return best_i

    async def evaluate(self, fish, hour, day_offset):
        data = await self.get()
        if not data:
            return None

        h = data.get("hourly", {})
        times = h.get("time", [])
        if not times:
            return None

        target_date = datetime.now() + timedelta(days=day_offset)
        target = target_date.replace(hour=hour, minute=0, second=0, microsecond=0)
        idx = self.nearest_hour_index(times, target)
        if idx is None:
            return None

        def val(key, default):
            arr = h.get(key, [])
            if idx >= len(arr) or arr[idx] is None:
                return default
            return arr[idx]

        pressure_hpa = float(val("surface_pressure", 1013.25))
        pressure_mm = pressure_hpa * 0.75006
        wind = float(val("wind_speed_10m", 2.5))
        wind_deg = val("wind_direction_10m", 0)
        temp = float(val("temperature_2m", 18))
        humidity = float(val("relative_humidity_2m", 55))
        cloud = float(val("cloud_cover", 40))
        precip = float(val("precipitation", 0))

        direction = get_wind_direction(wind_deg)

        # Open-Meteo не даёт надёжную температуру воды для обычного
        # внутреннего водохранилища через стандартный forecast endpoint.
        # Поэтому показываем ОЦЕНКУ, а не выдаём её за измерение.
        water_temp = round(max(0, min(30, temp * 0.82 + 3.2)), 1)

        predator = fish in PREDATORS

        score = 48
        trend_text, trend_pts = self.pressure_trend(
            h.get("surface_pressure", []), idx
        )
        stability_text, stability_pts = self.pressure_stability(
            h.get("surface_pressure", []), idx
        )

        score += trend_pts
        score += stability_pts
        score += self.pressure_score(pressure_mm, predator)
        score += self.temp_score(water_temp, predator)
        score += self.wind_score(wind, direction, predator)
        score += self.precip_score(precip, predator)
        score += self.cloud_score(cloud, predator)

        sun_title, sun_desc, sun_pts = sun_activity(hour)
        score += sun_pts

        moon_text, moon_pts = moon_phase(target_date)
        score += moon_pts if predator else int(moon_pts * 0.5)

        score = max(0, min(100, int(score)))
        stars = self.star_score(score)

        comfort = 50
        if 15 <= temp <= 25:
            comfort += 20
        elif 10 <= temp <= 30:
            comfort += 10
        else:
            comfort -= 10

        if wind <= 5:
            comfort += 15
        elif wind <= 8:
            comfort += 5
        else:
            comfort -= 15

        if precip < 1:
            comfort += 15
        elif precip < 3:
            comfort += 5
        else:
            comfort -= 10

        comfort = max(0, min(100, comfort))

        if day_offset == 0:
            day_name = "Сьогодні"
        elif day_offset == 1:
            day_name = "Завтра"
        elif day_offset == 2:
            day_name = "Післязавтра"
        else:
            day_name = target.strftime("%d.%m.%Y")

        commentary = []
        commentary.append(
            f"⏱ <b>{sun_title}:</b> {sun_desc}."
        )
        commentary.append(
            f"🌀 <b>Тиск:</b> {pressure_mm:.1f} мм | "
            f"{trend_text} | {stability_text}"
        )
        commentary.append(
            f"🌡 <b>Температура:</b> повітря {temp:.1f}°C, "
            f"вода орієнтовно ~{water_temp:.1f}°C"
        )

        if water_temp > 25:
            commentary.append("• Спека — ищите глубину, тень и течение.")
        elif water_temp < 9:
            commentary.append("• Холодная вода — медленная подача и мелкая насадка.")

        if wind < 2:
            commentary.append(f"💨 Штиль: {wind:.1f} м/с ({direction}).")
        elif wind <= 6:
            commentary.append(f"💨 Ветер: {wind:.1f} м/с ({direction}) — рабочий диапазон.")
        else:
            commentary.append(f"💨 Сильный ветер: {wind:.1f} м/с ({direction}).")

        if precip > 1.5:
            commentary.append(f"🌧 Осадки: {precip:.1f} мм.")
        else:
            commentary.append(f"☁️ Облачность: {cloud:.0f}%.")

        commentary.append(f"🌕 {moon_text}")
        commentary.append(f"🌤 Комфорт: {comfort}/100")
        commentary.append(f"🎣 Насадка: {bait(fish, water_temp, wind)}")

        if predator:
            commentary.append(
                f"🎯 Для {fish}: ищите бровки, перепады глубины, течение."
            )
        else:
            commentary.append(
                f"🎯 Для {fish}: точечная прикормка и аккуратная подача."
            )

        if score >= 78:
            verdict = "🏆 Отличные условия."
        elif score >= 55:
            verdict = "⚖️ Хорошие условия."
        elif score >= 40:
            verdict = "🟠 Средние условия."
        else:
            verdict = "🔴 Сложные условия."

        commentary.append(f"\n{verdict}")

        return {
            "fish": fish,
            "forecast_day": f"{day_name} ({target.strftime('%d.%m.%Y')})",
            "hour": hour,
            "pressure_mm": round(pressure_mm, 1),
            "pressure_trend": trend_text,
            "pressure_stability": stability_text,
            "wind_ms": round(wind, 1),
            "wind_dir": direction,
            "humidity": round(humidity),
            "cloud_cover": round(cloud),
            "precipitation": round(precip, 1),
            "temperature": round(temp, 1),
            "water_temp": water_temp,
            "moon_phase": moon_text,
            "stars": stars,
            "score_100": score,
            "comfort_index": comfort,
            "sun_activity": f"{sun_title} — {sun_desc}",
            "expert_commentary": "\n".join(commentary),
        }


# ============================================================
# IMAGE
# ============================================================

def _load_fonts():
    """Пытаемся загрузить нормальные шрифты, работающие и на Linux."""
    candidates = [
        # Windows
        ("arial.ttf", "arialbd.ttf"),
        # Linux common
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/freefont/FreeSans.ttf",
         "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
    ]
    for regular, bold in candidates:
        try:
            font = ImageFont.truetype(regular, 24)
            bold_f = ImageFont.truetype(bold, 30)
            small = ImageFont.truetype(regular, 18)
            return font, bold_f, small
        except Exception:
            continue
    # fallback
    default = ImageFont.load_default()
    return default, default, default


def make_image(result, region, body_name, fish):
    try:
        img = Image.new("RGB", (1000, 650), (240, 248, 255))
        draw = ImageDraw.Draw(img)

        font, bold, small = _load_fonts()

        draw.text((30, 20), "Fishing Forecast", font=bold, fill=(0, 0, 120))
        draw.text(
            (30, 65),
            f"{body_name} | {fish}",
            font=font,
            fill=(50, 50, 50),
        )
        draw.text(
            (30, 105),
            f"{result['forecast_day']} | {result['hour']:02d}:00",
            font=font,
            fill=(50, 50, 50),
        )

        stars = "⭐" * result["stars"] + "☆" * (5 - result["stars"])
        draw.text(
            (30, 150),
            f"Оценка: {result['stars']}/5 {stars}  ({result['score_100']}/100)",
            font=bold,
            fill=(180, 100, 0),
        )

        rows = [
            f"Температура воздуха: {result['temperature']}°C",
            f"Вода: ~{result['water_temp']}°C (оценка)",
            f"Давление: {result['pressure_mm']} мм",
            f"Ветер: {result['wind_ms']} м/с ({result['wind_dir']})",
            f"Влажность: {result['humidity']}%",
            f"Облачность: {result['cloud_cover']}%",
            f"Осадки: {result['precipitation']} мм",
            f"Комфорт: {result['comfort_index']}/100",
        ]

        y = 205
        for row in rows:
            draw.text((30, y), row, font=font, fill=(0, 0, 0))
            y += 38

        draw.line((30, y + 5, 970, y + 5), fill=(180, 180, 180), width=2)
        y += 25

        verdict = (
            "ОТЛИЧНЫЕ УСЛОВИЯ" if result["score_100"] >= 78
            else "ХОРОШИЕ УСЛОВИЯ" if result["score_100"] >= 55
            else "СРЕДНИЕ УСЛОВИЯ" if result["score_100"] >= 40
            else "СЛОЖНЫЕ УСЛОВИЯ"
        )
        draw.text((30, y), verdict, font=bold, fill=(0, 100, 0))

        out = io.BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        return out.getvalue()

    except Exception as e:
        logging.exception("Ошибка генерации изображения: %s", e)
        return None


# ============================================================
# KEYBOARDS
# ============================================================

def regions_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Дніпропетровська"), KeyboardButton(text="Київська")],
            [KeyboardButton(text="Полтавська"), KeyboardButton(text="Запорізька")],
            [KeyboardButton(text="Черкаська")],
            [KeyboardButton(text="📍 Моє місце"), KeyboardButton(text="🗺️ Водойми")],
            [KeyboardButton(text="📜 Моя історія"), KeyboardButton(text="ℹ️ Допомога")],
            [KeyboardButton(text="🔔 Підписка"), KeyboardButton(text="🎯 Мої трофеї")],
            [KeyboardButton(text="🌐 Змінити мову")],
        ],
        resize_keyboard=True,
    )


def fish_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Лящ"), KeyboardButton(text="Карась"), KeyboardButton(text="Короп")],
            [KeyboardButton(text="Щука"), KeyboardButton(text="Окунь"), KeyboardButton(text="Сом")],
            [KeyboardButton(text="Плотва"), KeyboardButton(text="◀️ Область")],
        ],
        resize_keyboard=True,
    )


def water_keyboard(region, prefix="water"):
    builder = InlineKeyboardBuilder()
    for i, body in enumerate(WATER_BODIES.get(region, [])):
        builder.button(text=f"🗺 {body['name']}", callback_data=f"{prefix}_{i}")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"{prefix}_back")
    )
    return builder.as_markup()


def day_keyboard():
    today = datetime.now()
    builder = InlineKeyboardBuilder()
    names = ["Сьогодні", "Завтра", "Післязавтра"]
    for i, name in enumerate(names):
        d = today + timedelta(days=i)
        builder.button(
            text=f"📅 {name} ({d:%d.%m})",
            callback_data=f"day_{i}",
        )
    builder.adjust(1)
    builder.button(text="▶️ Своя година", callback_data="manual_hour")
    builder.button(text="◀️ Назад", callback_data="back_to_fish")
    builder.adjust(1)
    return builder.as_markup()


def hour_keyboard(back="back_to_day"):
    builder = InlineKeyboardBuilder()
    for hour in [5, 6, 7, 8, 9, 10, 12, 14, 16, 18, 19, 20, 21, 22, 23]:
        builder.button(text=f"{hour:02d}:00", callback_data=f"hour_{hour}")
    builder.adjust(3)
    builder.button(text="◀️ Назад", callback_data=back)
    return builder.as_markup()


def language_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang_uk"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
        ]
    ])


def fish_keyboard_inline():
    builder = InlineKeyboardBuilder()
    for fish in FISH_LIST:
        builder.button(text=fish, callback_data=f"subfish_{fish}")
    builder.adjust(2)
    return builder.as_markup()


# ============================================================
# BOT
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


async def start_forecast(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ForecastStates.region)
    await message.answer(
        T(message.from_user.id, "start"),
        reply_markup=regions_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await start_forecast(message, state)


@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Допомога")
async def help_handler(message: Message):
    await message.answer(T(message.from_user.id, "help"), parse_mode="HTML")


@dp.message(F.text == "🏠 Головне меню")
async def menu_handler(message: Message, state: FSMContext):
    await start_forecast(message, state)


# ---------------- REGION ----------------

@dp.message(F.text.in_(REGIONS.keys()))
async def region_handler(message: Message, state: FSMContext):
    region = message.text
    await state.clear()
    await state.update_data(region=region)
    await state.set_state(ForecastStates.water_body)

    await message.answer(
        f"📍 <b>{region}</b>\n\nВыберите конкретный водоём:",
        reply_markup=water_keyboard(region),
        parse_mode="HTML",
    )


@dp.message(F.text == "◀️ Область")
async def back_region(message: Message, state: FSMContext):
    await start_forecast(message, state)


# ---------------- LOCATION ----------------

@dp.message(F.text == "📍 Моє місце")
async def location_request(message: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text="📍 Надіслати геолокацію",
            request_location=True
        )]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Надішліть геолокацію — визначу найближчу область.",
        reply_markup=kb,
    )


@dp.message(F.location)
async def location_handler(message: Message, state: FSMContext):
    loc: Location = message.location
    region = nearest_region(loc.latitude, loc.longitude)

    await state.clear()
    await state.update_data(region=region)
    await state.set_state(ForecastStates.water_body)

    await message.answer(
        f"📍 Найближча область: <b>{region}</b>\n\n"
        "Теперь выберите водоём. Прогноз будет рассчитан по его координатам.",
        reply_markup=water_keyboard(region),
        parse_mode="HTML",
    )


# ---------------- WATER BODY ----------------

@dp.message(F.text == "🗺️ Водойми")
async def water_menu(message: Message, state: FSMContext):
    data = await state.get_data()
    region = data.get("region")

    if not region:
        await message.answer(
            "Спочатку оберіть область.",
            reply_markup=regions_keyboard(),
        )
        return

    await state.set_state(ForecastStates.water_body)
    await message.answer(
        f"🗺️ <b>Водойми: {region}</b>\nОберіть водойму:",
        reply_markup=water_keyboard(region),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("water_"))
async def water_selected(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    region = data.get("region")

    if callback.data == "water_back":
        await state.set_state(ForecastStates.region)
        await callback.message.edit_text("Оберіть область.")
        await callback.answer()
        return

    if not region:
        await callback.answer("Сначала выберите область", show_alert=True)
        return

    try:
        idx = int(callback.data.split("_")[1])
        bodies = WATER_BODIES.get(region, [])
        if idx < 0 or idx >= len(bodies):
            raise IndexError
        body = bodies[idx]
    except Exception:
        await callback.answer("Ошибка выбора водоёма", show_alert=True)
        return

    await state.update_data(
        water_body=body["name"],
        latitude=body["lat"],
        longitude=body["lon"],
    )
    await state.set_state(ForecastStates.fish)

    maps_url = f"https://www.google.com/maps?q={body['lat']},{body['lon']}"

    await callback.message.edit_text(
        f"🗺 <b>{body['name']}</b>\n"
        f"📍 {body['lat']:.5f}, {body['lon']:.5f}\n\n"
        f"<a href='{maps_url}'>Открыть точку на карте</a>\n\n"
        "Теперь выберите рыбу:",
        reply_markup=None,
        parse_mode="HTML",
    )
    await callback.message.answer(
        "🐟 Выберите рыбу:",
        reply_markup=fish_keyboard(),
    )
    await callback.answer()


# ---------------- FISH ----------------

@dp.message(F.text.in_(FISH_LIST))
async def fish_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("region") or not data.get("water_body"):
        await message.answer("Сначала выберите область и водоём.")
        return

    await state.update_data(fish=message.text)
    await state.set_state(ForecastStates.day)

    body = data["water_body"]
    await message.answer(
        f"🎣 Рыба: <b>{message.text}</b>\n"
        f"🗺 Водоём: <b>{body}</b>\n\n"
        "Выберите день:",
        reply_markup=day_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "back_to_fish")
async def back_to_fish(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ForecastStates.fish)
    await callback.message.edit_text("🐟 Выберите рыбу кнопками ниже.")
    await callback.answer()


# ---------------- DAY/HOUR ----------------

@dp.callback_query(F.data.startswith("day_"))
async def day_selected(callback: CallbackQuery, state: FSMContext):
    offset = int(callback.data.split("_")[1])
    await state.update_data(day_offset=offset)
    await state.set_state(ForecastStates.hour)

    await callback.message.edit_text(
        "⏰ Выберите час:",
        reply_markup=hour_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "manual_hour")
async def manual_hour(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ForecastStates.manual_hour)
    await callback.message.edit_text(
        "Введите час числом от 0 до 23.\nНапример: 17",
    )
    await callback.answer()


@dp.message(ForecastStates.manual_hour)
async def manual_hour_text(message: Message, state: FSMContext):
    try:
        hour = int(message.text.strip())
        if not 0 <= hour <= 23:
            raise ValueError
    except ValueError:
        await message.answer("Введите число от 0 до 23.")
        return

    await state.update_data(hour=hour)
    await run_forecast(message, state, hour)


@dp.callback_query(F.data.startswith("hour_"))
async def hour_selected(callback: CallbackQuery, state: FSMContext):
    hour = int(callback.data.split("_")[1])
    await state.update_data(hour=hour)

    await run_forecast(
        callback.message, state, hour,
        callback_user_id=callback.from_user.id
    )
    await callback.answer()


@dp.callback_query(F.data == "back_to_day")
async def back_to_day(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ForecastStates.day)
    await callback.message.edit_text(
        "Выберите день:",
        reply_markup=day_keyboard(),
    )
    await callback.answer()


async def run_forecast(message: Message, state: FSMContext, hour: int, callback_user_id=None):
    user_id = callback_user_id or message.from_user.id
    data = await state.get_data()

    region = data.get("region")
    body_name = data.get("water_body")
    fish = data.get("fish")
    day_offset = int(data.get("day_offset", 0))

    if not region or not body_name or not fish:
        await message.answer("Не хватает данных. Нажмите /start.")
        await state.clear()
        return

    body = body_by_name(region, body_name)
    if not body:
        await message.answer("Водоём не найден. Нажмите /start.")
        await state.clear()
        return

    await message.answer(T(user_id, "processing"))

    client = WeatherClient(body["lat"], body["lon"])
    result = await client.evaluate(fish, hour, day_offset)

    if not result:
        await message.answer(T(user_id, "rate"), reply_markup=regions_keyboard())
        await state.clear()
        return

    forecast_id = save_forecast(
        user_id, region, body, fish, result
    )

    stars = "⭐" * result["stars"] + "☆" * (5 - result["stars"])

    if result["score_100"] >= 80:
        grade = "🟢 Отлично"
    elif result["score_100"] >= 60:
        grade = "🟡 Хорошо"
    elif result["score_100"] >= 40:
        grade = "🟠 Средне"
    else:
        grade = "🔴 Плохо"

    text = (
        f"🎣 <b>ПРОГНОЗ КЛЁВА</b>\n\n"
        f"🗺 <b>Водоём:</b> {body['name']}\n"
        f"📍 <b>Координаты:</b> {body['lat']:.5f}, {body['lon']:.5f}\n"
        f"📅 {result['forecast_day']}\n"
        f"⏰ {result['hour']:02d}:00\n"
        f"🐟 <b>{fish}</b>\n\n"
        f"⭐ <b>{result['stars']}/5</b> {stars}\n"
        f"📊 {grade} — {result['score_100']}/100\n\n"
        f"🌡 Воздух: {result['temperature']}°C\n"
        f"💧 Вода: ~{result['water_temp']}°C <i>(расчётная оценка)</i>\n"
        f"🌀 Давление: {result['pressure_mm']} мм\n"
        f"   {result['pressure_trend']}\n"
        f"   {result['pressure_stability']}\n"
        f"💨 Ветер: {result['wind_ms']} м/с, {result['wind_dir']}\n"
        f"💧 Влажность: {result['humidity']}%\n"
        f"☁️ Облачность: {result['cloud_cover']}%\n"
        f"🌧 Осадки: {result['precipitation']} мм\n"
        f"🌙 {result['moon_phase']}\n"
        f"🌤 Комфорт: {result['comfort_index']}/100\n\n"
        f"💡 <b>Рекомендации:</b>\n{result['expert_commentary']}\n\n"
        f"📦 <i>Погода: Open-Meteo. Координаты — выбранный водоём.</i>"
    )

    maps_url = f"https://www.google.com/maps?q={body['lat']},{body['lon']}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Открыть водоём", url=maps_url)],
        [InlineKeyboardButton(
            text="📢 Поделиться",
            callback_data=f"share_{result['stars']}_{fish}"
        )],
        [
            InlineKeyboardButton(text="👍 Точный", callback_data=f"fb_good_{forecast_id}"),
            InlineKeyboardButton(text="👎 Хибный", callback_data=f"fb_bad_{forecast_id}"),
        ],
        [InlineKeyboardButton(text="💬 Чат клуба", url=GROUP_URL)],
    ])

    image = make_image(result, region, body["name"], fish)

    if image:
        await message.answer_photo(
            photo=BufferedInputFile(image, filename="forecast.png"),
            caption=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

    await state.clear()


# ---------------- FEEDBACK/SHARE ----------------

@dp.callback_query(F.data.startswith("fb_"))
async def feedback_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.answer("Ошибка", show_alert=True)
        return
    rating = parts[1]
    try:
        fid = int(parts[2])
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return

    save_feedback(callback.from_user.id, fid, rating)
    await callback.answer(
        "Спасибо за обратную связь 👍" if rating == "good"
        else "Спасибо за обратную связь 👎",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("share_"))
async def share_handler(callback: CallbackQuery):
    try:
        _, stars, fish = callback.data.split("_", 2)
        graphic = "⭐" * int(stars) + "☆" * (5 - int(stars))
        text = (
            f"📢 <b>{callback.from_user.first_name} поделился прогнозом!</b>\n"
            f"🎣 {fish}\n"
            f"⭐ {stars}/5 {graphic}\n"
            f"💬 Присоединяйтесь к рыболовному клубу!"
        )
        await bot.send_message(GROUP_CHAT_ID, text, parse_mode="HTML")
        await callback.answer("✅ Отправлено в чат!", show_alert=True)
    except Exception as e:
        logging.exception("Share error: %s", e)
        await callback.answer("❌ Ошибка отправки", show_alert=True)


# ---------------- HISTORY ----------------

@dp.message(F.text == "📜 Моя історія")
async def history_handler(message: Message):
    rows = get_history(message.from_user.id)
    if not rows:
        await message.answer(T(message.from_user.id, "history_empty"))
        return

    text = "📜 <b>Последние прогнозы:</b>\n\n"
    for r in rows:
        stars = "⭐" * (r["stars"] or 0) + "☆" * (5 - (r["stars"] or 0))
        text += (
            f"🗺 {r['water_body'] or r['region']}\n"
            f"🐟 {r['fish_type']} | {r['forecast_day']} | "
            f"{(r['hour'] or 0):02d}:00\n"
            f"{stars}\n"
            f"🕒 {r['timestamp']}\n\n"
        )

    await message.answer(text, parse_mode="HTML")


# ---------------- SUBSCRIPTION ----------------

@dp.message(F.text == "🔔 Підписка")
async def subscription_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(SubscribeStates.region)
    await message.answer("Выберите область:", reply_markup=regions_keyboard())


@dp.message(SubscribeStates.region, F.text.in_(REGIONS.keys()))
async def subscription_region(message: Message, state: FSMContext):
    await state.update_data(region=message.text)
    await state.set_state(SubscribeStates.water_body)
    await message.answer(
        "Выберите водоём:",
        reply_markup=water_keyboard(message.text, prefix="subwater"),
    )


@dp.callback_query(F.data.startswith("subwater_"))
async def subscription_water(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    region = data.get("region")

    if callback.data == "subwater_back":
        await state.set_state(SubscribeStates.region)
        await callback.message.edit_text("Выберите область.")
        await callback.answer()
        return

    if not region:
        await callback.answer("Сначала выберите область", show_alert=True)
        return

    try:
        idx = int(callback.data.split("_")[1])
        bodies = WATER_BODIES.get(region, [])
        body = bodies[idx]
    except Exception:
        await callback.answer("Ошибка выбора водоёма", show_alert=True)
        return

    await state.update_data(
        water_body=body["name"],
        latitude=body["lat"],
        longitude=body["lon"],
    )
    await state.set_state(SubscribeStates.fish)

    await callback.message.edit_text(
        f"🗺 {body['name']}\n\nВыберите рыбу:",
        reply_markup=fish_keyboard_inline(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("subfish_"))
async def subscription_fish(callback: CallbackQuery, state: FSMContext):
    fish = callback.data[len("subfish_"):]
    await state.update_data(fish=fish)
    await state.set_state(SubscribeStates.hour)

    builder = InlineKeyboardBuilder()
    for hour in [6, 7, 8, 12, 18, 19, 20]:
        builder.button(text=f"{hour:02d}:00", callback_data=f"subhour_{hour}")
    builder.adjust(3)

    await callback.message.edit_text(
        "Выберите время ежедневной рассылки:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("subhour_"))
async def subscription_hour(callback: CallbackQuery, state: FSMContext):
    hour = int(callback.data.split("_")[1])
    data = await state.get_data()

    body = body_by_name(data["region"], data["water_body"])
    if not body:
        await callback.answer("Ошибка водоёма", show_alert=True)
        await state.clear()
        return

    save_subscription(
        callback.from_user.id,
        data["region"],
        body,
        data["fish"],
        hour,
    )

    await state.clear()
    await callback.message.edit_text(
        T(callback.from_user.id, "subscribe_done") +
        f"\n🗺 {body['name']}\n🐟 {data['fish']}\n⏰ {hour:02d}:00"
    )
    await callback.answer()


# ---------------- SEASON ----------------

@dp.message(F.text == "🗓 Сезон")
async def season_handler(message: Message):
    text = "🗓 <b>Сезонність:</b>\n\n"
    for fish, (start, end) in SPAWNING.items():
        text += f"🐟 {fish}: {start}–{end} місяці\n"
    text += (
        "\n⚠️ Это справочная информация. Перед рыбалкой "
        "проверяйте действующие местные ограничения."
    )
    await message.answer(text, parse_mode="HTML")


# ---------------- TROPHIES ----------------

@dp.message(F.text == "🎯 Мої трофеї")
async def trophies(message: Message):
    rows = get_catches(message.from_user.id)
    if not rows:
        await message.answer(
            "У вас пока нет трофеев.\nИспользуйте /add_catch."
        )
        return

    text = "🏆 <b>Ваши трофеи:</b>\n\n"
    for r in rows:
        text += (
            f"🐟 {r['fish_type']} — {r['weight']} г, {r['length']} см\n"
            f"📍 {r['location']} | {r['date']}\n\n"
        )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("add_catch"))
async def catch_start(message: Message, state: FSMContext):
    await state.set_state(TrophyStates.fish)
    await message.answer("Введите название рыбы:")


@dp.message(TrophyStates.fish)
async def catch_fish(message: Message, state: FSMContext):
    await state.update_data(fish=message.text)
    await state.set_state(TrophyStates.weight)
    await message.answer("Введите вес в граммах:")


@dp.message(TrophyStates.weight)
async def catch_weight(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число.")
        return

    await state.update_data(weight=value)
    await state.set_state(TrophyStates.length)
    await message.answer("Введите длину в сантиметрах:")


@dp.message(TrophyStates.length)
async def catch_length(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число.")
        return

    await state.update_data(length=value)
    await state.set_state(TrophyStates.location)
    await message.answer("Введите место ловли:")


@dp.message(TrophyStates.location)
async def catch_location(message: Message, state: FSMContext):
    await state.update_data(location=message.text)
    await state.set_state(TrophyStates.photo)
    await message.answer(
        "Отправьте фото или напишите /skip_photo."
    )


@dp.message(Command("skip_photo"), TrophyStates.photo)
async def catch_skip_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    save_catch(
        message.from_user.id,
        data["fish"], data["weight"], data["length"],
        data["location"], None,
    )
    await state.clear()
    await message.answer("✅ Трофей сохранён!")


@dp.message(TrophyStates.photo, F.photo)
async def catch_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    save_catch(
        message.from_user.id,
        data["fish"], data["weight"], data["length"],
        data["location"], message.photo[-1].file_id,
    )
    await state.clear()
    await message.answer("✅ Трофей сохранён с фото!")


# ---------------- LANGUAGE ----------------

@dp.message(F.text == "🌐 Змінити мову")
async def language_start(message: Message, state: FSMContext):
    await state.set_state(LanguageStates.choose)
    await message.answer(
        "Оберіть мову / Выберите язык:",
        reply_markup=language_keyboard(),
    )


@dp.callback_query(F.data.startswith("lang_"))
async def language_set(callback: CallbackQuery, state: FSMContext):
    lang = callback.data.split("_")[1]
    if lang not in {"uk", "ru"}:
        await callback.answer("Ошибка", show_alert=True)
        return

    set_user_lang(callback.from_user.id, lang)
    await state.clear()

    text = "Мову змінено на українську." if lang == "uk" else "Язык изменён на русский."
    await callback.message.edit_text(text)
    await callback.answer()


# ============================================================
# BACKGROUND TASKS
# ============================================================

async def send_daily_forecasts():
    for row in get_subscriptions():
        try:
            body = {
                "name": row["water_body"],
                "lat": row["latitude"],
                "lon": row["longitude"],
            }
            result = await WeatherClient(
                body["lat"], body["lon"]
            ).evaluate(
                row["fish_type"],
                row["hour"],
                0,
            )

            if not result:
                continue

            text = (
                f"🌅 <b>Ежедневный прогноз</b>\n\n"
                f"🗺 {body['name']}\n"
                f"🐟 {row['fish_type']}\n"
                f"⏰ {row['hour']:02d}:00\n"
                f"⭐ {result['stars']}/5\n"
                f"📊 {result['score_100']}/100\n"
                f"🌡 {result['temperature']}°C\n"
                f"💨 {result['wind_ms']} м/с\n"
                f"🌀 {result['pressure_mm']} мм\n\n"
                f"{result['expert_commentary']}"
            )

            await bot.send_message(
                row["user_id"],
                text,
                parse_mode="HTML",
            )

        except Exception as e:
            logging.exception(
                "Ошибка daily forecast user=%s: %s",
                row["user_id"], e
            )


async def check_extreme_weather():
    for row in get_subscriptions():
        try:
            client = WeatherClient(row["latitude"], row["longitude"])
            data = await client.get()
            if not data:
                continue

            pressures = data.get("hourly", {}).get("surface_pressure", [])
            if len(pressures) < 12:
                continue

            a = pressures[-12]
            b = pressures[-1]
            if a is None or b is None:
                continue

            delta = (b - a) * 0.75006

            if delta < -5:
                await bot.send_message(
                    row["user_id"],
                    f"⚠️ <b>Резкое падение давления</b>\n"
                    f"🗺 {row['water_body']}\n"
                    f"Изменение: {delta:.1f} мм рт.ст.\n"
                    f"Клёв может стать нестабильным.",
                    parse_mode="HTML",
                )

        except Exception as e:
            logging.exception(
                "Extreme weather error user=%s: %s",
                row["user_id"], e
            )


# ============================================================
# FALLBACK + HEALTH
# ============================================================

@dp.message()
async def fallback(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer(
            "Нажмите /start",
            reply_markup=regions_keyboard(),
        )


async def health(_):
    return web.Response(text="Fishing Forecast bot is running OK")


# ============================================================
# MAIN
# ============================================================

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    init_db()

    app = web.Application()
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logging.info("Health server started on port %s", PORT)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_daily_forecasts,
        CronTrigger(hour=7, minute=0),
        id="daily_forecast",
        replace_existing=True,
    )
    scheduler.add_job(
        check_extreme_weather,
        CronTrigger(hour=12, minute=0),
        id="extreme_weather",
        replace_existing=True,
    )
    scheduler.start()

    logging.info("Start polling")
    logging.info("Water-body coordinates are used for weather requests")
    logging.info("Open-Meteo cache TTL: %s seconds", CACHE_TTL)
    logging.info("Wind speed unit forced to m/s")

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
