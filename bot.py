import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
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
from aiogram.exceptions import TelegramUnauthorizedError


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("fishing_bot")


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан. "
        "Добавь BOT_TOKEN в Render → Environment."
    )


def get_int_env(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()

    try:
        return int(value)
    except ValueError:
        logger.warning(
            "%s имеет неправильное значение: %s. Использую %s",
            name,
            value,
            default,
        )
        return default


GROUP_CHAT_ID = get_int_env(
    "GROUP_CHAT_ID",
    -1004434293069,
)

GROUP_URL = os.getenv(
    "GROUP_URL",
    "https://t.me/+rKxYkNg85aAwNzFi",
).strip()


OPEN_METEO_API_KEY = os.getenv(
    "OPEN_METEO_API_KEY",
    "",
).strip()


# ============================================================
# SETTINGS
# ============================================================

DB_FILE = os.getenv(
    "DB_FILE",
    "fishing_forecast.db",
)

PORT = get_int_env(
    "PORT",
    10000,
)

# Кэш погоды:
# 2 часа.
#
# ВАЖНО:
# Погода дополнительно сохраняется в SQLite.
# Поэтому при рестарте Render кэш не пропадает.

WEATHER_CACHE_TTL = 2 * 60 * 60

# После 429 не пытаемся снова стучаться
# в Open-Meteo в течение 20 минут.

RATE_LIMIT_COOLDOWN = 20 * 60

# Минимальный интервал между запросами
# к Open-Meteo из этого процесса.

MIN_API_REQUEST_INTERVAL = 10


# ============================================================
# REGIONS
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


# ============================================================
# FISH
# ============================================================

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
# FSM
# ============================================================

class ForecastStates(StatesGroup):
    choosing_region = State()
    choosing_fish = State()
    choosing_day = State()
    choosing_hour = State()


# ============================================================
# MEMORY CACHE
# ============================================================

weather_cache = {}

weather_api_lock = asyncio.Lock()

last_open_meteo_request = 0.0


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA busy_timeout=30000"
    )

    return conn


def init_db():
    conn = get_db()

    cursor = conn.cursor()

    cursor.execute(
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

    cursor.execute(
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

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_cache (
            cache_key TEXT PRIMARY KEY,
            region TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            data TEXT,
            timestamp REAL NOT NULL,
            failed_until REAL DEFAULT 0
        )
        """
    )

    # --------------------------------------------------------
    # Совместимость со старой базой
    # --------------------------------------------------------

    try:
        cursor.execute(
            "ALTER TABLE forecasts ADD COLUMN hour INTEGER"
        )
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

    logger.info(
        "SQLite database initialized"
    )


# ============================================================
# WEATHER CACHE DATABASE
# ============================================================

def load_weather_cache_from_db(
    cache_key: str,
):
    conn = get_db()

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            data,
            timestamp,
            failed_until
        FROM weather_cache
        WHERE cache_key = ?
        """,
        (cache_key,),
    )

    row = cursor.fetchone()

    conn.close()

    if not row:
        return None

    data_text, timestamp, failed_until = row

    try:
        import json

        data = (
            json.loads(data_text)
            if data_text
            else None
        )

    except Exception as e:
        logger.warning(
            "Ошибка чтения weather cache: %s",
            e,
        )
        data = None

    return {
        "data": data,
        "timestamp": timestamp,
        "failed_until": failed_until or 0,
    }


def save_weather_cache_to_db(
    cache_key: str,
    region: str,
    latitude: float,
    longitude: float,
    data,
    timestamp: float,
    failed_until: float = 0,
):
    import json

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO weather_cache (
            cache_key,
            region,
            latitude,
            longitude,
            data,
            timestamp,
            failed_until
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key)
        DO UPDATE SET
            data = excluded.data,
            timestamp = excluded.timestamp,
            failed_until = excluded.failed_until
        """,
        (
            cache_key,
            region,
            latitude,
            longitude,
            json.dumps(
                data,
                ensure_ascii=False,
            )
            if data is not None
            else None,
            timestamp,
            failed_until,
        ),
    )

    conn.commit()
    conn.close()


def save_rate_limit_to_db(
    cache_key: str,
    region: str,
    latitude: float,
    longitude: float,
    failed_until: float,
):
    import json

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO weather_cache (
            cache_key,
            region,
            latitude,
            longitude,
            data,
            timestamp,
            failed_until
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key)
        DO UPDATE SET
            failed_until = excluded.failed_until
        """,
        (
            cache_key,
            region,
            latitude,
            longitude,
            json.dumps(
                None
            ),
            0,
            failed_until,
        ),
    )

    conn.commit()
    conn.close()


# ============================================================
# FORECAST DATABASE
# ============================================================

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

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO forecasts (
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

    forecast_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return forecast_id


def save_feedback_to_db(
    user_id,
    forecast_id,
    rating,
):
    conn = get_db()

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO feedback (
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
    conn.close()


def get_user_history_from_db(
    user_id,
):
    conn = get_db()

    cursor = conn.cursor()

    cursor.execute(
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

    rows = cursor.fetchall()

    conn.close()

    return rows


# ============================================================
# HELPERS
# ============================================================

def get_wind_direction_text(
    degrees,
) -> str:

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
        return directions[
            round(float(degrees) / 45) % 8
        ]

    except Exception:
        return "Н/Д"


# ============================================================
# MOON
# ============================================================

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
    ).total_seconds() / 86400 % 29.53

    if phase_days < 1.8:
        return (
            "Новомісяць 🌑",
            -6,
        )

    elif phase_days < 7.4:
        return (
            "Зростаючий місяць 🌒",
            4,
        )

    elif phase_days < 11.1:
        return (
            "Перша чверть 🌓",
            6,
        )

    elif phase_days < 16.5:
        return (
            "Повня 🌕",
            10,
        )

    elif phase_days < 22.1:
        return (
            "Спадаючий місяць 🌖",
            5,
        )

    elif phase_days < 25.8:
        return (
            "Остання чверть 🌗",
            3,
        )

    return (
        "Старий місяць 🌘",
        -4,
    )


# ============================================================
# SUN ACTIVITY
# ============================================================

def check_sun_activity(
    hour: int,
) -> Tuple[str, str, int]:

    if 4 <= hour <= 7:

        return (
            "🌅 Світанок",
            "Максимальна ранкова активність.",
            16,
        )

    elif 19 <= hour <= 21:

        return (
            "🌇 Захід сонця",
            "Вихід хижака та ляща на мілководдя.",
            14,
        )

    elif hour >= 22 or hour < 4:

        return (
            "🌙 Ніч",
            "Можливий кльов сома та великого ляща.",
            4,
        )

    return (
        "☀️ День",
        "Стандартна активність.",
        0,
    )


# ============================================================
# WEATHER CLIENT
# ============================================================

class WeatherClient:

    def __init__(
        self,
        lat: float,
        lon: float,
        region_name: str,
    ):

        self.lat = lat
        self.lon = lon
        self.region_name = region_name

        self.cache_key = (
            f"{region_name}:{lat}:{lon}"
        )

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    def build_url(self):

        return (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.lat}"
            f"&longitude={self.lon}"
            "&hourly="
            "temperature_2m,"
            "apparent_temperature,"
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

    # --------------------------------------------------------
    # REQUEST
    # --------------------------------------------------------

    async def fetch_open_meteo(
        self,
        session: aiohttp.ClientSession,
    ):

        global last_open_meteo_request

        async with weather_api_lock:

            now = time.monotonic()

            elapsed = (
                now - last_open_meteo_request
            )

            if elapsed < MIN_API_REQUEST_INTERVAL:

                wait_time = (
                    MIN_API_REQUEST_INTERVAL
                    - elapsed
                )

                logger.info(
                    "Ждём %.1f сек перед запросом Open-Meteo",
                    wait_time,
                )

                await asyncio.sleep(
                    wait_time
                )

            last_open_meteo_request = (
                time.monotonic()
            )

            url = self.build_url()

            logger.info(
                "Запрос погоды: %s",
                self.region_name,
            )

            try:

                timeout = aiohttp.ClientTimeout(
                    total=30,
                    connect=10,
                )

                async with session.get(
                    url,
                    timeout=timeout,
                    headers={
                        "User-Agent":
                            "FishingForecastBot/2.0"
                    },
                ) as response:

                    if response.status == 200:

                        data = await response.json()

                        if (
                            not data
                            or "hourly" not in data
                        ):

                            logger.error(
                                "Open-Meteo вернул "
                                "некорректные данные"
                            )

                            return None

                        logger.info(
                            "Погода получена: %s",
                            self.region_name,
                        )

                        return data

                    if response.status == 429:

                        logger.warning(
                            "Open-Meteo 429: "
                            "превышен лимит запросов"
                        )

                        return "RATE_LIMIT"

                    text = await response.text()

                    logger.error(
                        "Open-Meteo HTTP %s: %s",
                        response.status,
                        text[:500],
                    )

                    return None

            except asyncio.TimeoutError:

                logger.warning(
                    "Open-Meteo timeout"
                )

                return None

            except aiohttp.ClientError as e:

                logger.warning(
                    "Ошибка соединения Open-Meteo: %s",
                    e,
                )

                return None

            except Exception as e:

                logger.exception(
                    "Ошибка Open-Meteo: %s",
                    e,
                )

                return None

    # --------------------------------------------------------
    # CACHE
    # --------------------------------------------------------

    async def get_weather(self):

        now = time.time()

        # ====================================================
        # MEMORY CACHE
        # ====================================================

        cache = weather_cache.get(
            self.cache_key
        )

        # ====================================================
        # DATABASE CACHE
        # ====================================================

        if cache is None:

            cache = (
                load_weather_cache_from_db(
                    self.cache_key
                )
            )

            if cache:

                weather_cache[
                    self.cache_key
                ] = cache

                logger.info(
                    "Загружен weather cache "
                    "из SQLite: %s",
                    self.region_name,
                )

        # ====================================================
        # FRESH CACHE
        # ====================================================

        if cache:

            age = (
                now
                - cache.get(
                    "timestamp",
                    0,
                )
            )

            if (
                cache.get("data")
                and age < WEATHER_CACHE_TTL
            ):

                logger.info(
                    "Используем кеш погоды: "
                    "%s, возраст %d мин",
                    self.region_name,
                    int(age / 60),
                )

                return cache["data"]

        # ====================================================
        # RATE LIMIT COOLDOWN
        # ====================================================

        if cache:

            failed_until = cache.get(
                "failed_until",
                0,
            )

            if now < failed_until:

                remaining = int(
                    failed_until - now
                )

                logger.warning(
                    "Open-Meteo cooldown для %s: "
                    "ещё %d сек",
                    self.region_name,
                    remaining,
                )

                if cache.get("data"):

                    logger.info(
                        "Используем старый кеш"
                    )

                    return cache["data"]

                return None

        # ====================================================
        # REQUEST
        # ====================================================

        async with aiohttp.ClientSession() as session:

            result = await self.fetch_open_meteo(
                session
            )

        # ====================================================
        # RATE LIMIT
        # ====================================================

        if result == "RATE_LIMIT":

            failed_until = (
                now
                + RATE_LIMIT_COOLDOWN
            )

            if cache:

                cache["failed_until"] = (
                    failed_until
                )

                weather_cache[
                    self.cache_key
                ] = cache

                save_weather_cache_to_db(
                    self.cache_key,
                    self.region_name,
                    self.lat,
                    self.lon,
                    cache.get("data"),
                    cache.get(
                        "timestamp",
                        0,
                    ),
                    failed_until,
                )

                if cache.get("data"):

                    logger.warning(
                        "429 → используем "
                        "старый кеш"
                    )

                    return cache["data"]

            else:

                weather_cache[
                    self.cache_key
                ] = {
                    "data": None,
                    "timestamp": 0,
                    "failed_until":
                        failed_until,
                }

                save_rate_limit_to_db(
                    self.cache_key,
                    self.region_name,
                    self.lat,
                    self.lon,
                    failed_until,
                )

            return None

        # ====================================================
        # OTHER ERROR
        # ====================================================

        if result is None:

            if cache and cache.get("data"):

                logger.warning(
                    "Open-Meteo недоступен → "
                    "используем старый кеш"
                )

                return cache["data"]

            return None

        # ====================================================
        # SAVE NEW CACHE
        # ====================================================

        new_cache = {
            "data": result,
            "timestamp": now,
            "failed_until": 0,
        }

        weather_cache[
            self.cache_key
        ] = new_cache

        save_weather_cache_to_db(
            self.cache_key,
            self.region_name,
            self.lat,
            self.lon,
            result,
            now,
            0,
        )

        logger.info(
            "Новая погода сохранена: %s",
            self.region_name,
        )

        return result

    # ========================================================
    # PRESSURE
    # ========================================================

    def pressure_score(
        self,
        pressure_mm: float,
        is_predator: bool,
    ) -> int:

        optimum = (
            748
            if is_predator
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

    # ========================================================
    # PRESSURE TREND
    # ========================================================

    def pressure_trend_score(
        self,
        pressures: list,
        idx: int,
    ) -> Tuple[str, int]:

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

        avg_older = (
            sum(older)
            / len(older)
        )

        delta = (
            avg_recent - avg_older
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

    # ========================================================
    # STABILITY
    # ========================================================

    def stability_score(
        self,
        pressures: list,
        idx: int,
    ) -> Tuple[str, int]:

        if idx < 48:

            return (
                "Недостатньо історії",
                0,
            )

        valid = [
            p
            for p in pressures[
                idx - 48:idx + 1
            ]
            if p is not None
        ]

        if len(valid) < 20:

            return (
                "Недостатньо даних",
                0,
            )

        diff = (
            max(valid)
            - min(valid)
        )

        diff_mm = (
            diff * 0.75006
        )

        if diff_mm <= 4:

            return (
                "Дуже стабільний ✅",
                12,
            )

        if diff_mm <= 7:

            return (
                "Стабільний",
                6,
            )

        if diff_mm <= 11:

            return (
                "Помірно мінливий ⚠️",
                -4,
            )

        return (
            "Стрибкоподібний ❌",
            -16,
        )

    # ========================================================
    # TEMPERATURE
    # ========================================================

    def temperature_score(
        self,
        temp: float,
        water_temp: float,
        is_predator: bool,
    ) -> int:

        if is_predator:

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

    # ========================================================
    # WIND
    # ========================================================

    def wind_score(
        self,
        wind_ms: float,
        wind_dir: str,
        is_predator: bool,
    ) -> int:

        if wind_ms < 1.5:

            score = (
                -4
                if is_predator
                else 2
            )

        elif 2.0 <= wind_ms <= 5.5:

            score = 10

        elif 5.5 < wind_ms <= 7.5:

            score = 2

        elif wind_ms > 9:

            score = -22

        else:

            score = -8

        if wind_dir in {
            "Пд",
            "Пд-Зх",
            "Зх",
            "Пд-Сх",
        }:

            score += 4

        elif wind_dir in {
            "Пн",
            "Пн-Сх",
        }:

            score -= 3

        return score

    # ========================================================
    # PRECIPITATION
    # ========================================================

    def precip_score(
        self,
        precip: float,
        is_predator: bool,
    ) -> int:

        if precip <= 0.1:
            return 0

        if 0.2 <= precip <= 1.8:

            return (
                7
                if is_predator
                else 4
            )

        if precip <= 3.5:
            return -6

        return -16

    # ========================================================
    # CLOUD
    # ========================================================

    def cloud_score(
        self,
        cloud: float,
        is_predator: bool,
    ) -> int:

        if is_predator:

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

    # ========================================================
    # STARS
    # ========================================================

    def calculate_stars(
        self,
        score: float,
    ) -> int:

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

    # ========================================================
    # COMMENTARY
    # ========================================================

    def generate_commentary(
        self,
        fish_type,
        pressure_mm,
        trend_text,
        stability_text,
        wind_ms,
        wind_dir,
        precip,
        sun_title,
        sun_desc,
        temp,
        water_temp,
        score,
        humidity,
        cloud_cover,
        moon_text,
    ):

        comments = []

        comments.append(
            f"⏱ <b>Час:</b> "
            f"{sun_title}. "
            f"{sun_desc}"
        )

        comments.append(
            f"🌕 <b>Місяць:</b> "
            f"{moon_text}"
        )

        comments.append(
            f"🌀 <b>Тиск:</b> "
            f"{pressure_mm} мм | "
            f"{trend_text} | "
            f"{stability_text}"
        )

        comments.append(
            f"🌡 <b>Температура:</b> "
            f"повітря {temp}°C, "
            f"вода ~{water_temp}°C"
        )

        if water_temp > 25:

            comments.append(
                "   • Спека — "
                "шукайте тінь, "
                "глибину та течію."
            )

        elif water_temp < 9:

            comments.append(
                "   • Холодна вода — "
                "дрібні наживки, "
                "повільна подача."
            )

        if wind_ms < 2:

            comments.append(
                f"💨 <b>Вітер:</b> "
                f"штиль "
                f"({wind_ms} м/с, "
                f"{wind_dir})."
            )

        elif wind_ms <= 6:

            comments.append(
                f"💨 <b>Вітер:</b> "
                f"сприятливий "
                f"({wind_ms} м/с, "
                f"{wind_dir})."
            )

        else:

            comments.append(
                f"💨 <b>Вітер:</b> "
                f"сильний "
                f"({wind_ms} м/с, "
                f"{wind_dir})."
            )

        if precip > 1.5:

            comments.append(
                f"🌧 <b>Опади:</b> "
                f"{precip} мм."
            )

        elif cloud_cover > 65:

            comments.append(
                f"☁️ <b>Хмарність:</b> "
                f"{cloud_cover}% — "
                f"сприятливо для хижака."
            )

        comments.append("")

        comments.append(
            f"🎯 <b>Рекомендації "
            f"по {fish_type}:</b>"
        )

        if fish_type in PREDATOR_FISH:

            comments.append(
                "   • Активні проводки "
                "на брівках і перепадах."
            )

            comments.append(
                "   • Шукайте хижака біля "
                "корчів, каміння "
                "та водоростей."
            )

            if fish_type == "Щука":

                comments.append(
                    "   • Воблери, блешні, "
                    "живець."
                )

            elif fish_type == "Окунь":

                comments.append(
                    "   • Вертушки, "
                    "мікроджиг, "
                    "відвідний поводок."
                )

            elif fish_type == "Сом":

                comments.append(
                    "   • Донка, живець, "
                    "крупні наживки."
                )

        else:

            comments.append(
                "   • Дрібна фракція + "
                "мотиль/опариш/"
                "кукурудза."
            )

            comments.append(
                "   • Підгодовуйте місце, "
                "але не перегодовуйте."
            )

            if fish_type == "Лящ":

                comments.append(
                    "   • Фідер або донка."
                )

            elif fish_type == "Карась":

                comments.append(
                    "   • Поплавок біля "
                    "очерету."
                )

            elif fish_type == "Короп":

                comments.append(
                    "   • Бойли або "
                    "велика кукурудза."
                )

            elif fish_type == "Плотва":

                comments.append(
                    "   • Тонка оснастка "
                    "і дрібний гачок."
                )

        comments.append("")

        if score >= 78:

            comments.append(
                "🏆 <b>ВИСНОВОК:</b> "
                "Відмінні умови!"
            )

        elif score >= 55:

            comments.append(
                "⚖️ <b>ВИСНОВОК:</b> "
                "Гарні умови. "
                "Багато залежить "
                "від місця."
            )

        else:

            comments.append(
                "⚠️ <b>ВИСНОВОК:</b> "
                "Складні умови. "
                "Потрібні майстерність "
                "і терпіння."
            )

        comments.append("")

        comments.append(
            "📊 <i>Прогноз базується "
            "на даних Open-Meteo "
            "та експертному алгоритмі.</i>"
        )

        return "\n".join(comments)

    # ========================================================
    # EVALUATE
    # ========================================================

    async def evaluate_biting(
        self,
        fish_type: str,
        target_hour: int,
        day_offset: int = 0,
    ):

        data = await self.get_weather()

        if not data:
            return None

        hourly = data.get(
            "hourly"
        )

        if not hourly:
            return None

        pressures = hourly.get(
            "surface_pressure",
            [],
        )

        if not pressures:
            return None

        # ----------------------------------------------------
        # Open-Meteo:
        #
        # past_days=2
        #
        # Индекс прогнозного дня:
        #
        # 48 + day * 24 + hour
        # ----------------------------------------------------

        target_index = (
            48
            + day_offset * 24
            + target_hour
        )

        max_idx = (
            len(pressures) - 1
        )

        target_index = max(
            0,
            min(
                target_index,
                max_idx,
            ),
        )

        def safe(
            value,
            default,
        ):
            return (
                value
                if value is not None
                else default
            )

        pressure_hpa = safe(
            pressures[target_index],
            1013.25,
        )

        pressure_mm = (
            pressure_hpa * 0.75006
        )

        wind_list = hourly.get(
            "wind_speed_10m",
            [],
        )

        wind_dir_list = hourly.get(
            "wind_direction_10m",
            [],
        )

        temp_list = hourly.get(
            "temperature_2m",
            [],
        )

        precip_list = hourly.get(
            "precipitation",
            [],
        )

        humidity_list = hourly.get(
            "relative_humidity_2m",
            [],
        )

        cloud_list = hourly.get(
            "cloud_cover",
            [],
        )

        def get_value(
            arr,
            index,
            default,
        ):

            if (
                index < 0
                or index >= len(arr)
            ):
                return default

            return safe(
                arr[index],
                default,
            )

        wind_ms = get_value(
            wind_list,
            target_index,
            2.5,
        )

        temp = get_value(
            temp_list,
            target_index,
            18.0,
        )

        precip = get_value(
            precip_list,
            target_index,
            0.0,
        )

        wind_degrees = get_value(
            wind_dir_list,
            target_index,
            None,
        )

        humidity = get_value(
            humidity_list,
            target_index,
            55,
        )

        cloud_cover = get_value(
            cloud_list,
            target_index,
            40,
        )

        wind_dir = (
            get_wind_direction_text(
                wind_degrees
            )
        )

        # ----------------------------------------------------
        # ОЦЕНКА ВОДЫ
        #
        # Это только приблизительная модель.
        # Open-Meteo здесь не даёт температуру
        # пресной воды для нашего водоёма.
        # ----------------------------------------------------

        water_temp = round(
            temp * 0.82 + 3.2,
            1,
        )

        is_predator = (
            fish_type in PREDATOR_FISH
        )

        score = 48

        stability_text, stability_pts = (
            self.stability_score(
                pressures,
                target_index,
            )
        )

        trend_text, trend_pts = (
            self.pressure_trend_score(
                pressures,
                target_index,
            )
        )

        score += stability_pts
        score += trend_pts

        score += self.pressure_score(
            pressure_mm,
            is_predator,
        )

        score += self.temperature_score(
            temp,
            water_temp,
            is_predator,
        )

        score += self.wind_score(
            wind_ms,
            wind_dir,
            is_predator,
        )

        score += self.precip_score(
            precip,
            is_predator,
        )

        score += self.cloud_score(
            cloud_cover,
            is_predator,
        )

        sun_title, sun_desc, sun_pts = (
            check_sun_activity(
                target_hour
            )
        )

        score += sun_pts

        target_date = (
            datetime.now()
            + timedelta(
                days=day_offset
            )
        )

        moon_text, moon_pts = (
            get_moon_phase_info(
                target_date
            )
        )

        if is_predator:

            score += moon_pts

        else:

            score += int(
                moon_pts * 0.5
            )

        final_score = min(
            100,
            max(
                0,
                int(score),
            ),
        )

        stars = (
            self.calculate_stars(
                final_score
            )
        )

        date_str = (
            target_date.strftime(
                "%d.%m.%Y"
            )
        )

        if day_offset == 0:

            day_text = (
                f"Сьогодні ({date_str})"
            )

        elif day_offset == 1:

            day_text = (
                f"Завтра ({date_str})"
            )

        else:

            day_text = (
                f"Післязавтра ({date_str})"
            )

        commentary = (
            self.generate_commentary(
                fish_type,
                round(
                    pressure_mm,
                    1,
                ),
                trend_text,
                stability_text,
                round(
                    wind_ms,
                    1,
                ),
                wind_dir,
                round(
                    precip,
                    1,
                ),
                sun_title,
                sun_desc,
                round(
                    temp,
                    1,
                ),
                round(
                    water_temp,
                    1,
                ),
                final_score,
                round(
                    humidity
                ),
                round(
                    cloud_cover
                ),
                moon_text,
            )
        )

        return {
            "fish": fish_type,
            "forecast_day": day_text,
            "hour": target_hour,
            "pressure_mm": round(
                pressure_mm,
                1,
            ),
            "pressure_stability":
                stability_text,
            "pressure_trend":
                trend_text,
            "wind_ms": round(
                wind_ms,
                1,
            ),
            "wind_dir": wind_dir,
            "humidity": round(
                humidity
            ),
            "cloud_cover": round(
                cloud_cover
            ),
            "precipitation": round(
                precip,
                1,
            ),
            "temperature": round(
                temp,
                1,
            ),
            "water_temp": round(
                water_temp,
                1,
            ),
            "moon_phase":
                moon_text,
            "stars": stars,
            "stars_graphic":
                "⭐" * stars
                + "☆" * (5 - stars),
            "expert_commentary":
                commentary,
            "sources_used":
                "Open-Meteo",
            "score_100":
                final_score,
        }


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(
    token=BOT_TOKEN
)

storage = MemoryStorage()

dp = Dispatcher(
    storage=storage
)


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
                KeyboardButton(
                    text="Лящ"
                ),
                KeyboardButton(
                    text="Карась"
                ),
                KeyboardButton(
                    text="Короп"
                ),
            ],
            [
                KeyboardButton(
                    text="Щука"
                ),
                KeyboardButton(
                    text="Окунь"
                ),
                KeyboardButton(
                    text="Сом"
                ),
            ],
            [
                KeyboardButton(
                    text="Плотва"
                ),
            ],
            [
                KeyboardButton(
                    text="◀️ Змінити область"
                ),
            ],
        ],
        resize_keyboard=True,
    )


# ============================================================
# DAY KEYBOARD
# ============================================================

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
# START
# ============================================================

@dp.message(
    Command("start")
)
async def cmd_start(
    message: Message,
    state: FSMContext,
):

    await state.clear()

    await state.set_state(
        ForecastStates.choosing_region
    )

    await message.answer(
        "🎣 <b>Fishing Forecast</b>\n\n"
        "Оберіть область для прогнозу кльову:",
        reply_markup=get_regions_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# HELP
# ============================================================

@dp.message(
    Command("help")
)
@dp.message(
    F.text == "ℹ️ Допомога"
)
async def cmd_help(
    message: Message,
):

    text = (
        "📖 <b>Допомога</b>\n\n"

        "<b>Як користуватися:</b>\n"
        "1. Оберіть область.\n"
        "2. Виберіть рибу.\n"
        "3. Виберіть день.\n"
        "4. Виберіть час.\n"
        "5. Отримайте прогноз.\n\n"

        "<b>Алгоритм враховує:</b>\n"
        "• температуру;\n"
        "• тиск;\n"
        "• тренд тиску;\n"
        "• стабільність тиску;\n"
        "• вітер;\n"
        "• опади;\n"
        "• хмарність;\n"
        "• час доби;\n"
        "• фазу місяця;\n"
        "• вид риби.\n\n"

        "<b>Джерело:</b> Open-Meteo\n"
        "<b>Кеш погоди:</b> 2 години\n"
        "<b>SQLite:</b> увімкнено"
    )

    await message.answer(
        text,
        parse_mode="HTML",
    )


# ============================================================
# HISTORY
# ============================================================

@dp.message(
    F.text == "📜 Моя історія"
)
async def show_history(
    message: Message,
):

    rows = (
        get_user_history_from_db(
            message.from_user.id
        )
    )

    if not rows:

        await message.answer(
            "У вас поки немає "
            "збережених прогнозів."
        )

        return

    text = (
        "<b>📜 Ваші останні "
        "прогнози:</b>\n\n"
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

        if hour is not None:

            hour_str = (
                f"{hour:02d}:00"
            )

        else:

            hour_str = "—"

        text += (
            f"📍 {region}\n"
            f"🎣 {fish}\n"
            f"{day} о {hour_str}\n"
            f"Оцінка: {graphic}\n"
            f"🕒 {timestamp}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML",
    )


# ============================================================
# REGION
# ============================================================

@dp.message(
    F.text.in_(REGIONS.keys())
)
async def handle_region(
    message: Message,
    state: FSMContext,
):

    await state.update_data(
        region=message.text
    )

    await state.set_state(
        ForecastStates.choosing_fish
    )

    await message.answer(
        f"📍 Область: "
        f"<b>{message.text}</b>\n\n"
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

@dp.message(
    F.text.in_(FISH_LIST)
)
async def handle_fish(
    message: Message,
    state: FSMContext,
):

    data = await state.get_data()

    if "region" not in data:

        await message.answer(
            "Спочатку оберіть область "
            "через /start"
        )

        return

    await state.update_data(
        fish=message.text
    )

    await state.set_state(
        ForecastStates.choosing_day
    )

    await message.answer(
        f"🎣 Риба: "
        f"<b>{message.text}</b>\n\n"
        f"Оберіть день:",
        reply_markup=get_day_keyboard(),
        parse_mode="HTML",
    )


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
        "🎣 Оберіть рибу за допомогою "
        "кнопок нижче 👇"
    )

    await callback.answer()


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
            "Помилка вибору дня",
            show_alert=True,
        )

        return

    await state.update_data(
        day_offset=day_offset
    )

    await state.set_state(
        ForecastStates.choosing_hour
    )

    keyboard = (
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🌅 Світанок (06:00)",
                        callback_data="hour_6",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="☀️ День (12:00)",
                        callback_data="hour_12",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🌇 Захід (20:00)",
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
    )

    await callback.message.edit_text(
        "🕐 <b>Оберіть час доби:</b>",
        reply_markup=keyboard,
        parse_mode="HTML",
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

    await state.set_state(
        ForecastStates.choosing_day
    )

    data = await state.get_data()

    fish_type = data.get(
        "fish",
        "Риба",
    )

    await callback.message.edit_text(
        f"🎣 Риба: "
        f"<b>{fish_type}</b>\n\n"
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
            "Помилка вибору часу",
            show_alert=True,
        )

        return

    data = await state.get_data()

    region = data.get(
        "region"
    )

    fish_type = data.get(
        "fish"
    )

    day_offset = data.get(
        "day_offset",
        0,
    )

    if region not in REGIONS:

        await callback.answer(
            "Спочатку оберіть область.",
            show_alert=True,
        )

        return

    if fish_type not in FISH_LIST:

        await callback.answer(
            "Спочатку оберіть рибу.",
            show_alert=True,
        )

        return

    coords = REGIONS[
        region
    ]

    client = WeatherClient(
        coords["lat"],
        coords["lon"],
        region,
    )

    await callback.message.edit_text(
        "⏳ <b>Аналізую погоду...</b>\n\n"
        "Перевіряю тиск, вітер, "
        "температуру, опади, хмарність "
        "та час доби.",
        parse_mode="HTML",
    )

    try:

        result = await client.evaluate_biting(
            fish_type,
            hour,
            day_offset,
        )

    except Exception as e:

        logger.exception(
            "Ошибка расчёта: %s",
            e,
        )

        result = None

    if not result:

        await callback.message.answer(
            "⚠️ <b>Не вдалося отримати "
            "погодні дані.</b>\n\n"
            "Open-Meteo тимчасово "
            "обмежив запити або сервіс "
            "недоступний.\n\n"
            "Спробуйте повторити трохи пізніше.",
            reply_markup=get_regions_keyboard(),
            parse_mode="HTML",
        )

        await state.clear()

        await callback.answer()

        return

    # ========================================================
    # SAVE
    # ========================================================

    forecast_id = (
        save_forecast_to_db(
            callback.from_user.id,
            region,
            fish_type,
            result["forecast_day"],
            result["hour"],
            result["pressure_mm"],
            result["wind_ms"],
            result["temperature"],
            result["stars"],
        )
    )

    # ========================================================
    # RESPONSE
    # ========================================================

    response = (
        f"📍 <b>{region}</b>\n"
        f"📅 {result['forecast_day']} "
        f"о {result['hour']:02d}:00\n"
        f"🎣 <b>{fish_type}</b>\n\n"

        f"🌕 {result['moon_phase']}\n"

        f"🌡 "
        f"{result['temperature']}°C "
        f"(вода ~"
        f"{result['water_temp']}°C)\n"

        f"🌀 Тиск: "
        f"{result['pressure_mm']} мм\n"

        f"   {result['pressure_trend']}\n"
        f"   {result['pressure_stability']}\n"

        f"💨 "
        f"{result['wind_ms']} м/с "
        f"({result['wind_dir']})\n"

        f"🌧 "
        f"{result['precipitation']} мм\n"

        f"☁️ "
        f"Хмарність: "
        f"{result['cloud_cover']}%\n\n"

        f"⭐ <b>Оцінка кльову: "
        f"{result['stars']}/5</b>\n"

        f"{result['stars_graphic']}\n\n"

        f"📊 <i>Бал: "
        f"{result['score_100']}/100</i>\n\n"

        f"💡 <b>Експертний аналіз:</b>\n"
        f"{result['expert_commentary']}\n\n"

        f"📡 <i>Джерело: "
        f"{result['sources_used']}</i>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Поділитися в чаті",
                    callback_data=(
                        f"share|"
                        f"{result['stars']}|"
                        f"{fish_type}|"
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
                        f"fb_good|"
                        f"{forecast_id}"
                    ),
                ),
                InlineKeyboardButton(
                    text="👎 Хибний",
                    callback_data=(
                        f"fb_bad|"
                        f"{forecast_id}"
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

        parts = callback.data.split("|")

        rating = parts[0].replace(
            "fb_",
            "",
        )

        forecast_id = int(
            parts[1]
        )

        save_feedback_to_db(
            callback.from_user.id,
            forecast_id,
            rating,
        )

        if rating == "good":

            text = (
                "Дякуємо! "
                "Ваш відгук допоможе "
                "покращити прогноз 👍"
            )

        else:

            text = (
                "Дякуємо за "
                "зворотний зв’язок 👎"
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
            "❌ Помилка",
            show_alert=True,
        )


# ============================================================
# SHARE
# ============================================================

@dp.callback_query(
    F.data.startswith("share|")
)
async def handle_share(
    callback: CallbackQuery,
):

    try:

        parts = callback.data.split(
            "|",
            3,
        )

        if len(parts) != 4:

            raise ValueError(
                "Неверные share data"
            )

        _, stars, fish, region = parts

        stars_int = int(
            stars
        )

        stars_int = max(
            0,
            min(
                5,
                stars_int,
            ),
        )

        graphic = (
            "⭐" * stars_int
            + "☆" * (5 - stars_int)
        )

        first_name = (
            callback.from_user.first_name
            or "Користувач"
        )

        text = (
            f"📢 <b>{first_name}</b> "
            f"поділився прогнозом!\n\n"

            f"📍 {region}\n"
            f"🎣 <b>{fish}</b>\n"
            f"⭐ {stars_int}/5 "
            f"({graphic})\n\n"

            f"🎣 Fishing Forecast"
        )

        await bot.send_message(
            GROUP_CHAT_ID,
            text,
            parse_mode="HTML",
        )

        await callback.answer(
            "✅ Надіслано в чат клубу!",
            show_alert=True,
        )

    except Exception as e:

        logger.exception(
            "Share error: %s",
            e,
        )

        await callback.answer(
            "❌ Не вдалося відправити "
            "прогноз у чат.\n"
            "Перевір GROUP_CHAT_ID "
            "і права бота.",
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

    else:

        await message.answer(
            "Будь ласка, використовуйте "
            "кнопки меню 👇"
        )


# ============================================================
# HEALTH SERVER
# ============================================================

async def health(
    request: web.Request,
):

    return web.Response(
        text="Fishing bot is running ✅"
    )


async def health_status(
    request: web.Request,
):

    return web.json_response(
        {
            "status": "ok",
            "bot": "Fishing Forecast",
            "time": datetime.utcnow().isoformat(),
        }
    )


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_head(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        health_status,
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
# TELEGRAM CHECK
# ============================================================

async def check_telegram():

    logger.info(
        "Checking Telegram bot token..."
    )

    try:

        me = await bot.get_me()

        logger.info(
            "Telegram bot authorized: "
            "@%s id=%s",
            me.username,
            me.id,
        )

        return True

    except TelegramUnauthorizedError:

        logger.error(
            "================================================"
        )

        logger.error(
            "TELEGRAM UNAUTHORIZED"
        )

        logger.error(
            "BOT_TOKEN неправильный, "
            "отозванный или принадлежит другому боту."
        )

        logger.error(
            "Проверь Render → Environment → BOT_TOKEN"
        )

        logger.error(
            "================================================"
        )

        return False

    except Exception as e:

        logger.exception(
            "Telegram authorization check failed: %s",
            e,
        )

        return False


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "===================================="
    )

    logger.info(
        "STARTING FISHING FORECAST BOT"
    )

    logger.info(
        "===================================="
    )

    # --------------------------------------------------------
    # DATABASE
    # --------------------------------------------------------

    init_db()

    # --------------------------------------------------------
    # HEALTH SERVER
    # --------------------------------------------------------

    health_runner = (
        await start_health_server()
    )

    try:

        # ----------------------------------------------------
        # TELEGRAM TOKEN CHECK
        # ----------------------------------------------------

        authorized = (
            await check_telegram()
        )

        if not authorized:

            raise RuntimeError(
                "Telegram BOT_TOKEN is invalid."
            )

        # ----------------------------------------------------
        # REMOVE WEBHOOK
        # ----------------------------------------------------

        try:

            await bot.delete_webhook(
                drop_pending_updates=False
            )

            logger.info(
                "Telegram webhook cleared"
            )

        except Exception as e:

            logger.warning(
                "Не удалось удалить webhook: %s",
                e,
            )

        # ----------------------------------------------------
        # POLLING
        # ----------------------------------------------------

        logger.info(
            "Starting Telegram polling..."
        )

        await dp.start_polling(
            bot,
            polling_timeout=30,
            handle_as_tasks=True,
            allowed_updates=dp.resolve_used_update_types(),
        )

    except TelegramUnauthorizedError:

        logger.error(
            "Telegram server says - Unauthorized"
        )

        raise

    except Exception as e:

        logger.exception(
            "Polling crashed: %s",
            e,
        )

        raise

    finally:

        logger.info(
            "Stopping health server..."
        )

        try:

            await health_runner.cleanup()

        except Exception as e:

            logger.warning(
                "Health server cleanup error: %s",
                e,
            )

        logger.info(
            "Closing Telegram bot session..."
        )

        try:

            await bot.session.close()

        except Exception as e:

            logger.warning(
                "Telegram session close error: %s",
                e,
            )

        logger.info(
            "Bot shutdown complete"
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
            "Bot stopped manually"
        )

    except Exception as e:

        logger.critical(
            "Fatal error: %s",
            e,
        )

        raise
