import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN.\n"
        "Render -> Environment -> Add Environment Variable -> "
        "BOT_TOKEN = токен твоего Telegram-бота"
    )

GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "-1004434293069"))
GROUP_URL = os.getenv(
    "GROUP_URL",
    "https://t.me/+rKxYkNg85aAwNzFi"
)

PORT = int(os.getenv("PORT", "10000"))

DB_FILE = os.getenv(
    "DB_FILE",
    "fishing_forecast.db"
)

# Кэш погоды.
# 60 минут более чем достаточно для прогноза.
WEATHER_CACHE_TTL = 60 * 60

# Если получили 429, не делаем повторные запросы сразу.
RATE_LIMIT_COOLDOWN = 15 * 60

# Максимальный возраст старых данных, которыми разрешаем
# воспользоваться при временной недоступности API.
STALE_WEATHER_TTL = 6 * 60 * 60


# ============================================================
# РЕГИОНЫ
# ============================================================

REGIONS = {
    "Дніпропетровська": {
        "lat": 48.4647,
        "lon": 35.0462,
    },
    "Київська": {
        "lat": 50.4501,
        "lon": 30.5234,
    },
    "Полтавська": {
        "lat": 49.5895,
        "lon": 34.5514,
    },
    "Запорізька": {
        "lat": 47.8388,
        "lon": 35.1396,
    },
    "Черкаська": {
        "lat": 49.4444,
        "lon": 32.0598,
    },
}


FISH_LIST = [
    "Лящ",
    "Карась",
    "Короп",
    "Щука",
    "Окунь",
    "Сом",
    "Плотва",
]


PREDATOR_FISH = {
    "Щука",
    "Окунь",
    "Сом",
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("fishing_bot")


# ============================================================
# FSM
# ============================================================

class ForecastStates(StatesGroup):
    choosing_region = State()
    choosing_fish = State()
    choosing_day = State()
    choosing_hour = State()


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
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
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                forecast_id INTEGER,
                rating TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_cache (
                cache_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                timestamp REAL NOT NULL,
                data TEXT NOT NULL
            )
            """
        )

        conn.commit()

    finally:
        conn.close()


def save_forecast_to_db(
    user_id,
    region,
    fish_type,
    forecast_day,
    hour,
    pressure,
    wind,
    temp,
    stars,
):
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO forecasts
            (
                user_id,
                region,
                fish_type,
                forecast_day,
                hour,
                pressure,
                wind,
                temp,
                stars
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                region,
                fish_type,
                forecast_day,
                hour,
                pressure,
                wind,
                temp,
                stars,
            ),
        )

        forecast_id = cur.lastrowid
        conn.commit()

        return forecast_id

    finally:
        conn.close()


def save_feedback_to_db(
    user_id,
    forecast_id,
    rating,
):
    conn = get_db()

    try:
        conn.execute(
            """
            INSERT INTO feedback
            (
                user_id,
                forecast_id,
                rating
            )
            VALUES (?, ?, ?)
            """,
            (
                user_id,
                forecast_id,
                rating,
            ),
        )

        conn.commit()

    finally:
        conn.close()


def get_user_history_from_db(user_id):
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                region,
                fish_type,
                forecast_day,
                hour,
                stars,
                timestamp
            FROM forecasts
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (user_id,),
        )

        return cur.fetchall()

    finally:
        conn.close()


# ============================================================
# WEATHER CACHE
# ============================================================

def get_cached_weather(region):
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                source,
                timestamp,
                data
            FROM weather_cache
            WHERE cache_key = ?
            """,
            (region,),
        )

        row = cur.fetchone()

        if not row:
            return None

        source, timestamp_value, data = row

        try:
            parsed = json.loads(data)
        except Exception:
            return None

        return {
            "source": source,
            "timestamp": float(timestamp_value),
            "data": parsed,
        }

    finally:
        conn.close()


def save_cached_weather(
    region,
    source,
    data,
):
    conn = get_db()

    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO weather_cache
            (
                cache_key,
                source,
                timestamp,
                data
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                region,
                source,
                time.time(),
                json.dumps(
                    data,
                    ensure_ascii=False,
                ),
            ),
        )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def safe_float(value, default):
    try:
        if value is None:
            return default

        return float(value)

    except (
        ValueError,
        TypeError,
    ):
        return default


def get_wind_direction_text(degrees) -> str:

    if degrees is None:
        return "Н/Д"

    directions = [
        "Пн",
        "Пн-Сх",
        "Сх",
        "Пд-Сх",
        "Пд",
        "Пд-Зх",
        "Зх",
        "Пн-Зх",
    ]

    try:
        index = round(float(degrees) / 45) % 8
        return directions[index]
    except Exception:
        return "Н/Д"


def get_moon_phase_info(
    date_obj: datetime,
) -> Tuple[str, int]:

    known_new_moon = datetime(
        2024,
        1,
        11,
    )

    phase_days = (
        date_obj - known_new_moon
    ).total_seconds() / 86400

    phase_days %= 29.530588

    if phase_days < 1.8:
        return "Новомісяць 🌑", -6

    if phase_days < 7.4:
        return "Зростаючий місяць 🌒", 4

    if phase_days < 11.1:
        return "Перша чверть 🌓", 6

    if phase_days < 16.5:
        return "Повня 🌕", 10

    if phase_days < 22.1:
        return "Спадаючий місяць 🌖", 5

    if phase_days < 25.8:
        return "Остання чверть 🌗", 3

    return "Старий місяць 🌘", -4


def check_sun_activity(
    hour: int,
) -> Tuple[str, str, int]:

    if 4 <= hour <= 7:
        return (
            "🌅 Світанок",
            "Максимальна ранкова активність.",
            16,
        )

    if 19 <= hour <= 21:
        return (
            "🌇 Захід сонця",
            "Висока активність хижака та мирної риби.",
            14,
        )

    if hour >= 22 or hour < 4:
        return (
            "🌙 Ніч",
            "Можливий нічний кльов сома та великого ляща.",
            4,
        )

    return (
        "☀️ День",
        "Стандартна денна активність.",
        0,
    )


# ============================================================
# WEATHER SERVICE
# ============================================================

class WeatherService:

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

        # Не допускаем одновременных запросов.
        self.lock = asyncio.Lock()

        # Время последнего 429.
        self.last_rate_limit = {}

    async def start(self):

        if self.session is None or self.session.closed:

            timeout = aiohttp.ClientTimeout(
                total=20,
                connect=8,
            )

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": (
                        "FishingForecastBot/1.0 "
                        "(Telegram fishing forecast)"
                    )
                },
            )

        logger.info("Weather HTTP session started")

    async def close(self):

        if self.session and not self.session.closed:
            await self.session.close()

        self.session = None

        logger.info("Weather HTTP session closed")

    # --------------------------------------------------------
    # OPEN-METEO
    # --------------------------------------------------------

    async def fetch_open_meteo(
        self,
        lat,
        lon,
    ):

        if self.session is None:
            await self.start()

        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}"
            f"&longitude={lon}"
            "&hourly="
            "temperature_2m,"
            "relative_humidity_2m,"
            "surface_pressure,"
            "wind_speed_10m,"
            "wind_direction_10m,"
            "cloud_cover,"
            "precipitation"
            "&timezone=auto"
            "&past_days=2"
            "&forecast_days=3"
        )

        try:

            async with self.session.get(url) as response:

                if response.status == 200:

                    data = await response.json()

                    logger.info(
                        "Open-Meteo: weather received"
                    )

                    return data

                if response.status == 429:

                    retry_after = response.headers.get(
                        "Retry-After"
                    )

                    logger.warning(
                        "Open-Meteo 429. "
                        f"Retry-After={retry_after}"
                    )

                    return None

                text = await response.text()

                logger.error(
                    "Open-Meteo HTTP %s: %s",
                    response.status,
                    text[:300],
                )

                return None

        except asyncio.TimeoutError:

            logger.warning(
                "Open-Meteo timeout"
            )

            return None

        except aiohttp.ClientError as e:

            logger.warning(
                "Open-Meteo connection error: %s",
                e,
            )

            return None

        except Exception as e:

            logger.exception(
                "Open-Meteo unexpected error: %s",
                e,
            )

            return None

    # --------------------------------------------------------
    # MET NORWAY FALLBACK
    # --------------------------------------------------------

    async def fetch_met_norway(
        self,
        lat,
        lon,
    ):

        if self.session is None:
            await self.start()

        url = (
            "https://api.met.no/weatherapi/"
            "locationforecast/2.0/compact"
            f"?lat={lat}"
            f"&lon={lon}"
        )

        try:

            async with self.session.get(url) as response:

                if response.status != 200:

                    logger.warning(
                        "MET Norway HTTP %s",
                        response.status,
                    )

                    return None

                data = await response.json()

                logger.info(
                    "MET Norway: weather received"
                )

                return data

        except Exception as e:

            logger.warning(
                "MET Norway error: %s",
                e,
            )

            return None

    # --------------------------------------------------------
    # MET -> OUR INTERNAL FORMAT
    # --------------------------------------------------------

    def convert_met_data(
        self,
        data,
    ):

        try:

            timeseries = (
                data["properties"]["timeseries"]
            )

            hourly = {
                "time": [],
                "temperature_2m": [],
                "relative_humidity_2m": [],
                "surface_pressure": [],
                "wind_speed_10m": [],
                "wind_direction_10m": [],
                "cloud_cover": [],
                "precipitation": [],
            }

            for item in timeseries:

                instant = (
                    item["data"]
                    .get("instant", {})
                    .get("details", {})
                )

                next_1h = (
                    item["data"]
                    .get("next_1_hours", {})
                    .get("details", {})
                )

                time_string = item["time"]

                hourly["time"].append(
                    time_string
                )

                hourly["temperature_2m"].append(
                    instant.get(
                        "air_temperature"
                    )
                )

                hourly["relative_humidity_2m"].append(
                    instant.get(
                        "relative_humidity"
                    )
                )

                hourly["surface_pressure"].append(
                    instant.get(
                        "air_pressure_at_sea_level"
                    )
                )

                hourly["wind_speed_10m"].append(
                    instant.get(
                        "wind_speed"
                    )
                )

                hourly["wind_direction_10m"].append(
                    instant.get(
                        "wind_from_direction"
                    )
                )

                hourly["cloud_cover"].append(
                    instant.get(
                        "cloud_area_fraction"
                    )
                )

                hourly["precipitation"].append(
                    next_1h.get(
                        "precipitation_amount"
                    )
                )

            return {
                "hourly": hourly
            }

        except Exception as e:

            logger.exception(
                "MET conversion error: %s",
                e,
            )

            return None

    # --------------------------------------------------------
    # GET WEATHER
    # --------------------------------------------------------

    async def get_weather(
        self,
        region,
    ):

        now = time.time()

        cached = get_cached_weather(region)

        if cached:

            age = now - cached["timestamp"]

            if age < WEATHER_CACHE_TTL:

                logger.info(
                    "Weather cache HIT: %s | age=%ss",
                    region,
                    int(age),
                )

                return (
                    cached["data"],
                    cached["source"],
                )

        async with self.lock:

            # Проверяем кэш ещё раз после блокировки.
            cached = get_cached_weather(region)

            if cached:

                age = now - cached["timestamp"]

                if age < WEATHER_CACHE_TTL:

                    return (
                        cached["data"],
                        cached["source"],
                    )

            # Защита от повторного 429.
            last_429 = self.last_rate_limit.get(
                region,
                0,
            )

            if (
                now - last_429
                < RATE_LIMIT_COOLDOWN
            ):

                logger.warning(
                    "Weather API cooldown: %s",
                    region,
                )

                # Используем старые данные.
                if cached:

                    age = now - cached["timestamp"]

                    if age < STALE_WEATHER_TTL:

                        return (
                            cached["data"],
                            cached["source"]
                            + " (кэш)",
                        )

                return None

            coords = REGIONS[region]

            # =================================================
            # 1. OPEN-METEO
            # =================================================

            data = await self.fetch_open_meteo(
                coords["lat"],
                coords["lon"],
            )

            if data:

                save_cached_weather(
                    region,
                    "Open-Meteo",
                    data,
                )

                return (
                    data,
                    "Open-Meteo",
                )

            # Ставим cooldown.
            self.last_rate_limit[region] = time.time()

            # =================================================
            # 2. MET NORWAY
            # =================================================

            met_data = await self.fetch_met_norway(
                coords["lat"],
                coords["lon"],
            )

            if met_data:

                converted = self.convert_met_data(
                    met_data
                )

                if converted:

                    save_cached_weather(
                        region,
                        "MET Norway",
                        converted,
                    )

                    return (
                        converted,
                        "MET Norway",
                    )

            # =================================================
            # 3. STALE CACHE
            # =================================================

            cached = get_cached_weather(region)

            if cached:

                age = time.time() - cached["timestamp"]

                if age < STALE_WEATHER_TTL:

                    logger.warning(
                        "Using stale weather cache: %s",
                        region,
                    )

                    return (
                        cached["data"],
                        cached["source"]
                        + " (старі дані)",
                    )

            return None


# ============================================================
# FISHING CALCULATOR
# ============================================================

class FishingCalculator:

    @staticmethod
    def pressure_score(
        pressure_mm,
        predator,
    ):

        optimum = (
            748
            if predator
            else 752
        )

        diff = abs(
            pressure_mm - optimum
        )

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
    def pressure_trend(
        pressures,
        idx,
    ):

        if idx < 24:
            return (
                "Недостатньо даних",
                0,
            )

        recent = [
            p
            for p in pressures[
                idx - 12:idx + 1
            ]
            if p is not None
        ]

        older = [
            p
            for p in pressures[
                idx - 24:idx - 12
            ]
            if p is not None
        ]

        if (
            len(recent) < 5
            or len(older) < 5
        ):
            return (
                "Недостатньо даних",
                0,
            )

        avg_recent = (
            sum(recent)
            / len(recent)
        )

        avg_old = (
            sum(older)
            / len(older)
        )

        delta = (
            avg_recent - avg_old
        ) * 0.75006

        if delta < -2.5:
            return (
                "Сильно падає 📉",
                12,
            )

        if delta < -0.8:
            return (
                "Повільно падає 📉",
                8,
            )

        if delta > 2.5:
            return (
                "Сильно росте 📈",
                -6,
            )

        if delta > 0.8:
            return (
                "Повільно росте 📈",
                2,
            )

        return (
            "Стабільний ✅",
            10,
        )

    @staticmethod
    def pressure_stability(
        pressures,
        idx,
    ):

        if idx < 48:
            return (
                "Недостатньо історії",
                0,
            )

        values = [
            p
            for p in pressures[
                idx - 48:idx + 1
            ]
            if p is not None
        ]

        if len(values) < 20:
            return (
                "Недостатньо даних",
                0,
            )

        diff = (
            max(values)
            - min(values)
        )

        if diff <= 4:
            return (
                "Дуже стабільний ✅",
                12,
            )

        if diff <= 7:
            return (
                "Стабільний",
                6,
            )

        if diff <= 11:
            return (
                "Помірно мінливий ⚠️",
                -4,
            )

        return (
            "Стрибкоподібний ❌",
            -16,
        )

    @staticmethod
    def temperature_score(
        water_temp,
        predator,
    ):

        if predator:

            if 8 <= water_temp <= 16:
                return 12

            if 5 <= water_temp <= 20:
                return 6

            if (
                water_temp > 24
                or water_temp < 3
            ):
                return -10

            return 0

        else:

            if 16 <= water_temp <= 23:
                return 12

            if 12 <= water_temp <= 26:
                return 6

            if (
                water_temp > 28
                or water_temp < 8
            ):
                return -8

            return 0

    @staticmethod
    def wind_score(
        wind,
        direction,
        predator,
    ):

        if wind < 1.5:

            score = (
                -4
                if predator
                else 2
            )

        elif 2 <= wind <= 5.5:

            score = 10

        elif 5.5 < wind <= 7.5:

            score = 2

        elif wind > 9:

            score = -22

        else:

            score = -8

        if direction in {
            "Пд",
            "Пд-Зх",
            "Зх",
            "Пд-Сх",
        }:

            score += 4

        elif direction in {
            "Пн",
            "Пн-Сх",
        }:

            score -= 3

        return score

    @staticmethod
    def precipitation_score(
        precip,
        predator,
    ):

        if precip <= 0.1:
            return 0

        if 0.2 <= precip <= 1.8:

            return (
                7
                if predator
                else 4
            )

        if precip <= 3.5:
            return -6

        return -16

    @staticmethod
    def cloud_score(
        cloud,
        predator,
    ):

        if predator:

            if cloud >= 70:
                return 9

            if cloud >= 40:
                return 4

            return -3

        else:

            if cloud >= 80:
                return 2

            if cloud <= 30:
                return 3

            return 0

    @staticmethod
    def stars(
        score,
    ):

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


# ============================================================
# EXPERT COMMENTARY
# ============================================================

def generate_commentary(
    fish,
    pressure,
    trend,
    stability,
    wind,
    wind_dir,
    precip,
    sun_title,
    sun_desc,
    air_temp,
    water_temp,
    cloud,
    moon,
    score,
):

    result = []

    result.append(
        f"⏱ <b>Час:</b> "
        f"{sun_title}. {sun_desc}"
    )

    result.append(
        f"🌕 <b>Місяць:</b> {moon}"
    )

    result.append(
        f"🌀 <b>Тиск:</b> "
        f"{pressure} мм | "
        f"{trend} | "
        f"{stability}"
    )

    result.append(
        f"🌡 <b>Температура:</b> "
        f"повітря {air_temp}°C, "
        f"вода ~{water_temp}°C"
    )

    if water_temp > 25:

        result.append(
            "• Спека — шукайте "
            "глибину, тінь та течію."
        )

    elif water_temp < 9:

        result.append(
            "• Холодна вода — "
            "повільна подача і дрібна наживка."
        )

    if wind < 2:

        result.append(
            f"💨 <b>Вітер:</b> "
            f"штиль {wind} м/с, {wind_dir}."
        )

    elif wind <= 6:

        result.append(
            f"💨 <b>Вітер:</b> "
            f"сприятливий {wind} м/с, "
            f"{wind_dir}."
        )

    else:

        result.append(
            f"💨 <b>Вітер:</b> "
            f"сильний {wind} м/с, "
            f"{wind_dir}."
        )

    if precip > 1.5:

        result.append(
            f"🌧 <b>Опади:</b> "
            f"{precip} мм."
        )

    if cloud > 65:

        result.append(
            f"☁️ <b>Хмарність:</b> "
            f"{cloud}% — хороший фактор "
            f"для хижака."
        )

    if fish in PREDATOR_FISH:

        result.append(
            f"🎯 <b>Для {fish}:</b> "
            f"перспективны бровки, "
            f"перепады глубины и участки "
            f"с движением воды."
        )

    else:

        result.append(
            f"🎯 <b>Для {fish}:</b> "
            f"ищите стабильную глубину, "
            f"кормовой стол и используйте "
            f"мотыль, опарыш или кукурузу."
        )

    if score >= 78:

        result.append(
            "\n🏆 <b>Итог:</b> "
            "Отличные условия для рыбалки."
        )

    elif score >= 55:

        result.append(
            "\n⚖️ <b>Итог:</b> "
            "Хорошие условия. "
            "Место и наживка будут решающими."
        )

    else:

        result.append(
            "\n⚠️ <b>Итог:</b> "
            "Условия сложные. "
            "Лучше рассчитывать на короткие "
            "периоды активности."
        )

    return "\n".join(result)


# ============================================================
# FORECAST
# ============================================================

async def calculate_forecast(
    weather_service,
    region,
    fish,
    hour,
    day_offset,
):

    weather_result = (
        await weather_service.get_weather(
            region
        )
    )

    if not weather_result:
        return None

    data, source = weather_result

    hourly = data.get(
        "hourly",
        {}
    )

    times = hourly.get(
        "time",
        []
    )

    pressures = hourly.get(
        "surface_pressure",
        []
    )

    temperatures = hourly.get(
        "temperature_2m",
        []
    )

    humidity = hourly.get(
        "relative_humidity_2m",
        []
    )

    winds = hourly.get(
        "wind_speed_10m",
        []
    )

    wind_directions = hourly.get(
        "wind_direction_10m",
        []
    )

    clouds = hourly.get(
        "cloud_cover",
        []
    )

    precipitation = hourly.get(
        "precipitation",
        []
    )

    if not times:
        logger.error(
            "Weather data contains no hourly data"
        )
        return None

    # --------------------------------------------------------
    # Находим индекс по реальной дате/времени.
    # Это надёжнее, чем жёстко считать 48 + day*24.
    # --------------------------------------------------------

    now = datetime.now()

    target_date = (
        now.date()
        + timedelta(days=day_offset)
    )

    target_index = None

    for i, time_string in enumerate(times):

        try:

            dt = datetime.fromisoformat(
                time_string
            )

            if (
                dt.date() == target_date
                and dt.hour == hour
            ):

                target_index = i
                break

        except Exception:
            continue

    # Fallback, если API отдал время
    # в другом формате.
    if target_index is None:

        target_index = min(
            48
            + day_offset * 24
            + hour,
            len(times) - 1,
        )

    def get_value(
        array,
        index,
        default,
    ):

        if (
            index < 0
            or index >= len(array)
        ):
            return default

        value = array[index]

        if value is None:
            return default

        return value

    pressure_hpa = safe_float(
        get_value(
            pressures,
            target_index,
            1013.25,
        ),
        1013.25,
    )

    pressure_mm = (
        pressure_hpa
        * 0.750061683
    )

    wind = safe_float(
        get_value(
            winds,
            target_index,
            2.5,
        ),
        2.5,
    )

    air_temp = safe_float(
        get_value(
            temperatures,
            target_index,
            18,
        ),
        18,
    )

    precip = safe_float(
        get_value(
            precipitation,
            target_index,
            0,
        ),
        0,
    )

    cloud = safe_float(
        get_value(
            clouds,
            target_index,
            40,
        ),
        40,
    )

    humidity_value = safe_float(
        get_value(
            humidity,
            target_index,
            55,
        ),
        55,
    )

    wind_degrees = get_value(
        wind_directions,
        target_index,
        None,
    )

    wind_dir = get_wind_direction_text(
        wind_degrees
    )

    # --------------------------------------------------------
    # Вода.
    #
    # Open-Meteo sea_surface_temperature
    # убрали намеренно.
    #
    # Для Днепра это морская температура
    # и она нам здесь не нужна.
    #
    # Используем приближение температуры воды.
    # --------------------------------------------------------

    water_temp = round(
        air_temp * 0.82 + 3.2,
        1,
    )

    # Ограничиваем очевидно абсурдные значения.
    water_temp = max(
        2,
        min(
            30,
            water_temp,
        ),
    )

    predator = (
        fish in PREDATOR_FISH
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    score = 48

    stability_text, stability_points = (
        FishingCalculator.pressure_stability(
            pressures,
            target_index,
        )
    )

    trend_text, trend_points = (
        FishingCalculator.pressure_trend(
            pressures,
            target_index,
        )
    )

    score += stability_points
    score += trend_points

    score += (
        FishingCalculator.pressure_score(
            pressure_mm,
            predator,
        )
    )

    score += (
        FishingCalculator.temperature_score(
            water_temp,
            predator,
        )
    )

    score += (
        FishingCalculator.wind_score(
            wind,
            wind_dir,
            predator,
        )
    )

    score += (
        FishingCalculator.precipitation_score(
            precip,
            predator,
        )
    )

    score += (
        FishingCalculator.cloud_score(
            cloud,
            predator,
        )
    )

    sun_title, sun_desc, sun_points = (
        check_sun_activity(hour)
    )

    score += sun_points

    moon_text, moon_points = (
        get_moon_phase_info(
            datetime.combine(
                target_date,
                datetime.min.time(),
            )
        )
    )

    if predator:
        score += moon_points
    else:
        score += int(
            moon_points * 0.5
        )

    score = max(
        0,
        min(
            100,
            round(score),
        ),
    )

    stars = (
        FishingCalculator.stars(
            score
        )
    )

    stars_graphic = (
        "⭐" * stars
        + "☆" * (5 - stars)
    )

    if day_offset == 0:

        day_text = (
            f"Сьогодні "
            f"({target_date.strftime('%d.%m.%Y')})"
        )

    elif day_offset == 1:

        day_text = (
            f"Завтра "
            f"({target_date.strftime('%d.%m.%Y')})"
        )

    else:

        day_text = (
            f"Післязавтра "
            f"({target_date.strftime('%d.%m.%Y')})"
        )

    commentary = generate_commentary(
        fish=fish,
        pressure=round(
            pressure_mm,
            1,
        ),
        trend=trend_text,
        stability=stability_text,
        wind=round(
            wind,
            1,
        ),
        wind_dir=wind_dir,
        precip=round(
            precip,
            1,
        ),
        sun_title=sun_title,
        sun_desc=sun_desc,
        air_temp=round(
            air_temp,
            1,
        ),
        water_temp=water_temp,
        cloud=round(
            cloud,
        ),
        moon=moon_text,
        score=score,
    )

    return {
        "fish": fish,
        "forecast_day": day_text,
        "hour": hour,

        "pressure_mm": round(
            pressure_mm,
            1,
        ),

        "pressure_stability": (
            stability_text
        ),

        "pressure_trend": (
            trend_text
        ),

        "wind_ms": round(
            wind,
            1,
        ),

        "wind_dir": wind_dir,

        "humidity": round(
            humidity_value
        ),

        "cloud_cover": round(
            cloud
        ),

        "precipitation": round(
            precip,
            1,
        ),

        "temperature": round(
            air_temp,
            1,
        ),

        "water_temp": water_temp,

        "moon_phase": moon_text,

        "stars": stars,

        "stars_graphic": stars_graphic,

        "expert_commentary": commentary,

        "sources_used": source,

        "score_100": score,
    }


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN
)

storage = MemoryStorage()

dp = Dispatcher(
    storage=storage
)

weather_service = WeatherService()


# ============================================================
# KEYBOARDS
# ============================================================

def get_regions_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Дніпропетровська"
                ),
                KeyboardButton(
                    text="Київська"
                ),
            ],
            [
                KeyboardButton(
                    text="Полтавська"
                ),
                KeyboardButton(
                    text="Запорізька"
                ),
            ],
            [
                KeyboardButton(
                    text="Черкаська"
                ),
            ],
            [
                KeyboardButton(
                    text="📜 Моя історія"
                ),
                KeyboardButton(
                    text="ℹ️ Допомога"
                ),
            ],
        ],
        resize_keyboard=True,
    )


def get_fish_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Лящ"),
                KeyboardButton(text="Карась"),
                KeyboardButton(text="Короп"),
            ],
            [
                KeyboardButton(text="Щука"),
                KeyboardButton(text="Окунь"),
                KeyboardButton(text="Сом"),
            ],
            [
                KeyboardButton(text="Плотва"),
            ],
            [
                KeyboardButton(
                    text="◀️ Змінити область"
                ),
            ],
        ],
        resize_keyboard=True,
    )


def get_day_keyboard():

    today = datetime.now()

    buttons = []

    for i in range(3):

        d = (
            today
            + timedelta(days=i)
        )

        if i == 0:

            label = (
                f"Сьогодні "
                f"({d.strftime('%d.%m')})"
            )

        elif i == 1:

            label = (
                f"Завтра "
                f"({d.strftime('%d.%m')})"
            )

        else:

            label = (
                f"Післязавтра "
                f"({d.strftime('%d.%m')})"
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"day_{i}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data="back_to_fish",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


# ============================================================
# /START
# ============================================================

@dp.message(Command("start"))
async def cmd_start(
    message: Message,
    state: FSMContext,
):

    await state.clear()

    await state.set_state(
        ForecastStates.choosing_region
    )

    await message.answer(
        "Привіт! 🎣\n\n"
        "Я допоможу оцінити кльов "
        "за погодою, тиском, вітром, "
        "хмарністю, опадами та часом доби.\n\n"
        "📍 Оберіть область:",
        reply_markup=get_regions_keyboard(),
    )


# ============================================================
# HELP
# ============================================================

@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Допомога")
async def cmd_help(
    message: Message,
):

    text = (
        "<b>🎣 Як працює прогноз</b>\n\n"

        "🌀 Атмосферний тиск\n"
        "• абсолютне значення\n"
        "• тренд за останні години\n"
        "• стабільність за 48 годин\n\n"

        "🌡 Температура\n"
        "• повітря\n"
        "• розрахункова температура води\n\n"

        "💨 Вітер\n"
        "• швидкість\n"
        "• напрямок\n\n"

        "☁️ Хмарність\n"
        "🌧 Опади\n"
        "🌕 Місячна фаза\n"
        "🌅 Час доби\n\n"

        "⚠️ Це прогноз ймовірності активності риби, "
        "а не гарантія улову."
    )

    await message.answer(
        text,
        parse_mode="HTML",
    )


# ============================================================
# HISTORY
# ============================================================

@dp.message(F.text == "📜 Моя історія")
async def show_history(
    message: Message,
):

    rows = get_user_history_from_db(
        message.from_user.id
    )

    if not rows:

        await message.answer(
            "У вас поки немає збережених прогнозів."
        )

        return

    text = (
        "<b>📜 Останні прогнози</b>\n\n"
    )

    for row in rows:

        (
            region,
            fish,
            day,
            hour,
            stars,
            timestamp,
        ) = row

        stars = stars or 0

        graphic = (
            "⭐" * stars
            + "☆" * (5 - stars)
        )

        hour_text = (
            f"{hour:02d}:00"
            if hour is not None
            else "—"
        )

        text += (
            f"📍 {region}\n"
            f"🎣 {fish}\n"
            f"📅 {day} о {hour_text}\n"
            f"⭐ {graphic}\n"
            f"🕒 {timestamp}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML",
    )


# ============================================================
# REGION
# ============================================================

@dp.message(F.text.in_(REGIONS.keys()))
async def handle_region(
    message: Message,
    state: FSMContext,
):

    region = message.text

    await state.update_data(
        region=region
    )

    await state.set_state(
        ForecastStates.choosing_fish
    )

    await message.answer(
        f"📍 Область: "
        f"<b>{region}</b>\n\n"
        f"🎣 Оберіть рибу:",
        reply_markup=get_fish_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# CHANGE REGION
# ============================================================

@dp.message(
    F.text == "◀️ Змінити область"
)
async def change_region(
    message: Message,
    state: FSMContext,
):

    await cmd_start(
        message,
        state,
    )


# ============================================================
# FISH
# ============================================================

@dp.message(F.text.in_(FISH_LIST))
async def handle_fish(
    message: Message,
    state: FSMContext,
):

    data = await state.get_data()

    if "region" not in data:

        await message.answer(
            "Спочатку оберіть область.",
            reply_markup=get_regions_keyboard(),
        )

        await state.set_state(
            ForecastStates.choosing_region
        )

        return

    fish = message.text

    await state.update_data(
        fish=fish
    )

    await state.set_state(
        ForecastStates.choosing_day
    )

    await message.answer(
        f"🎣 Риба: <b>{fish}</b>\n\n"
        f"Оберіть день:",
        reply_markup=get_day_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# DAY
# ============================================================

@dp.callback_query(
    F.data.startswith("day_")
)
async def handle_day(
    callback: CallbackQuery,
    state: FSMContext,
):

    try:

        day_offset = int(
            callback.data.split("_")[1]
        )

    except Exception:

        await callback.answer(
            "Помилка",
            show_alert=True,
        )

        return

    await state.update_data(
        day_offset=day_offset
    )

    await state.set_state(
        ForecastStates.choosing_hour
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌅 Світанок — 06:00",
                    callback_data="hour_6",
                )
            ],
            [
                InlineKeyboardButton(
                    text="☀️ День — 12:00",
                    callback_data="hour_12",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌇 Захід — 20:00",
                    callback_data="hour_20",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="back_to_day",
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "🕐 Оберіть час:",
        reply_markup=keyboard,
    )

    await callback.answer()


# ============================================================
# BACK TO FISH
# ============================================================

@dp.callback_query(
    F.data == "back_to_fish"
)
async def handle_back_to_fish(
    callback: CallbackQuery,
    state: FSMContext,
):

    await state.set_state(
        ForecastStates.choosing_fish
    )

    await callback.message.edit_text(
        "🎣 Оберіть рибу "
        "за допомогою кнопок нижче."
    )

    await callback.answer()


# ============================================================
# BACK TO DAY
# ============================================================

@dp.callback_query(
    F.data == "back_to_day"
)
async def handle_back_to_day(
    callback: CallbackQuery,
    state: FSMContext,
):

    data = await state.get_data()

    fish = data.get(
        "fish",
        "Рибу",
    )

    await state.set_state(
        ForecastStates.choosing_day
    )

    await callback.message.edit_text(
        f"🎣 Риба: <b>{fish}</b>\n\n"
        f"Оберіть день:",
        reply_markup=get_day_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


# ============================================================
# HOUR
# ============================================================

@dp.callback_query(
    F.data.startswith("hour_")
)
async def handle_hour(
    callback: CallbackQuery,
    state: FSMContext,
):

    try:

        hour = int(
            callback.data.split("_")[1]
        )

    except Exception:

        await callback.answer(
            "Помилка часу",
            show_alert=True,
        )

        return

    data = await state.get_data()

    region = data.get(
        "region"
    )

    fish = data.get(
        "fish"
    )

    day_offset = data.get(
        "day_offset",
        0,
    )

    if not region or not fish:

        await callback.message.answer(
            "Сесія вибору втрачена. "
            "Натисніть /start."
        )

        await state.clear()
        await callback.answer()

        return

    await callback.message.edit_text(
        "⏳ <b>Аналізую погоду...</b>\n\n"
        "Перевіряю тиск, вітер, "
        "температуру, хмарність "
        "та інші фактори.",
        parse_mode="HTML",
    )

    try:

        result = await calculate_forecast(
            weather_service,
            region,
            fish,
            hour,
            day_offset,
        )

    except Exception as e:

        logger.exception(
            "Forecast calculation error: %s",
            e,
        )

        result = None

    if not result:

        await callback.message.answer(
            "⚠️ <b>Метеодані тимчасово недоступні.</b>\n\n"
            "Open-Meteo зараз обмежив запити, "
            "тому бот не буде безкінечно "
            "бомбити сервер повторними запитами.\n\n"
            "Автоматично використовується "
            "резервне джерело MET Norway.\n\n"
            "Спробуйте повторити прогноз "
            "через кілька хвилин.",
            reply_markup=get_regions_keyboard(),
            parse_mode="HTML",
        )

        await state.clear()
        await callback.answer()

        return

    forecast_id = save_forecast_to_db(
        callback.from_user.id,
        region,
        fish,
        result["forecast_day"],
        result["hour"],
        result["pressure_mm"],
        result["wind_ms"],
        result["temperature"],
        result["stars"],
    )

    response = (
        f"📍 <b>{region}</b>\n"
        f"📅 {result['forecast_day']} "
        f"о {result['hour']:02d}:00\n"
        f"🎣 <b>{fish}</b>\n"
        f"📡 Джерело: {result['sources_used']}\n\n"

        f"🌕 {result['moon_phase']}\n"
        f"🌡 {result['temperature']}°C "
        f"(вода ~{result['water_temp']}°C)\n"

        f"🌀 Тиск: "
        f"{result['pressure_mm']} мм\n"
        f"   {result['pressure_trend']}\n"
        f"   {result['pressure_stability']}\n"

        f"💨 {result['wind_ms']} м/с "
        f"({result['wind_dir']})\n"

        f"🌧 Опади: "
        f"{result['precipitation']} мм\n"

        f"☁️ Хмарність: "
        f"{result['cloud_cover']}%\n"

        f"💧 Вологість: "
        f"{result['humidity']}%\n\n"

        f"⭐ <b>Кльов: "
        f"{result['stars']}/5</b>\n"
        f"{result['stars_graphic']}\n"
        f"<i>Внутрішній бал: "
        f"{result['score_100']}/100</i>\n\n"

        f"💡 <b>Експертний аналіз:</b>\n"
        f"{result['expert_commentary']}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Поділитися в чаті",
                    callback_data=(
                        f"share_"
                        f"{result['stars']}_"
                        f"{fish}_"
                        f"{region}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Перейти в чат",
                    url=GROUP_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    text="👍 Точний",
                    callback_data=(
                        f"fb_good_{forecast_id}"
                    ),
                ),
                InlineKeyboardButton(
                    text="👎 Хибний",
                    callback_data=(
                        f"fb_bad_{forecast_id}"
                    ),
                ),
            ],
        ]
    )

    await callback.message.answer(
        response,
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await state.clear()

    await callback.answer()


# ============================================================
# FEEDBACK
# ============================================================

@dp.callback_query(
    F.data.startswith("fb_")
)
async def handle_feedback(
    callback: CallbackQuery,
):

    try:

        parts = callback.data.split("_")

        rating = parts[1]

        forecast_id = int(
            parts[2]
        )

        save_feedback_to_db(
            callback.from_user.id,
            forecast_id,
            rating,
        )

        if rating == "good":

            text = (
                "Дякуємо! "
                "Відгук допоможе покращити прогноз 👍"
            )

        else:

            text = (
                "Дякуємо за зворотний зв'язок 👎"
            )

        await callback.answer(
            text,
            show_alert=True,
        )

    except Exception as e:

        logger.exception(
            "Feedback error: %s",
            e,
        )

        await callback.answer(
            "Помилка збереження відгуку.",
            show_alert=True,
        )


# ============================================================
# SHARE
# ============================================================

@dp.callback_query(
    F.data.startswith("share_")
)
async def handle_share(
    callback: CallbackQuery,
):

    try:

        _, stars, fish, region = (
            callback.data.split(
                "_",
                3,
            )
        )

        stars = int(stars)

        graphic = (
            "⭐" * stars
            + "☆" * (5 - stars)
        )

        first_name = (
            callback.from_user.first_name
            or "Рибалка"
        )

        text = (
            f"📢 <b>{first_name}</b> "
            f"поділився прогнозом!\n\n"
            f"📍 {region}\n"
            f"🎣 <b>{fish}</b>\n"
            f"⭐ {stars}/5 "
            f"({graphic})\n\n"
            f"🎣 Приєднуйтесь до "
            f"рибальського клубу!"
        )

        await bot.send_message(
            GROUP_CHAT_ID,
            text,
            parse_mode="HTML",
        )

        await callback.answer(
            "✅ Надіслано в чат!",
            show_alert=True,
        )

    except Exception as e:

        logger.exception(
            "Share error: %s",
            e,
        )

        await callback.answer(
            "❌ Не вдалося надіслати.",
            show_alert=True,
        )


# ============================================================
# FALLBACK
# ============================================================

@dp.message()
async def fallback(
    message: Message,
    state: FSMContext,
):

    current_state = (
        await state.get_state()
    )

    if current_state is None:

        await message.answer(
            "🎣 Натисніть /start",
            reply_markup=get_regions_keyboard(),
        )


# ============================================================
# ERROR HANDLER
# ============================================================

@dp.errors()
async def global_error_handler(
    event,
):

    logger.exception(
        "Unhandled aiogram error: %s",
        event.exception,
    )

    return True


# ============================================================
# HEALTH SERVER
# ============================================================

async def health_handler(
    request: web.Request,
):

    return web.json_response(
        {
            "status": "ok",
            "service": "fishing-bot",
            "time": datetime.now(
                timezone.utc
            ).isoformat(),
        }
    )


async def health_head(
    request: web.Request,
):

    return web.Response(
        status=200
    )


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health_handler,
    )

    app.router.add_get(
        "/health",
        health_handler,
    )

    app.router.add_head(
        "/",
        health_head,
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    logger.info(
        "Health server started on port %s",
        PORT,
    )

    return runner


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "======================================"
    )

    logger.info(
        "FISHING BOT START"
    )

    logger.info(
        "Python PID: %s",
        os.getpid(),
    )

    logger.info(
        "Port: %s",
        PORT,
    )

    logger.info(
        "======================================"
    )

    init_db()

    await weather_service.start()

    health_runner = (
        await start_health_server()
    )

    try:

        logger.info(
            "Starting Telegram polling..."
        )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    except asyncio.CancelledError:

        logger.info(
            "Polling cancelled"
        )

    except Exception as e:

        logger.exception(
            "Polling crashed: %s",
            e,
        )

        raise

    finally:

        logger.info(
            "Stopping services..."
        )

        await weather_service.close()

        await bot.session.close()

        await health_runner.cleanup()

        logger.info(
            "Bot stopped"
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped by user"
        )
