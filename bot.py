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
        "Добавь BOT_TOKEN в Render -> Environment."
    )


GROUP_CHAT_ID_RAW = os.getenv(
    "GROUP_CHAT_ID",
    "-1004434293069",
).strip()

try:
    GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW)
except ValueError:
    GROUP_CHAT_ID = -1004434293069


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

PORT = int(
    os.getenv(
        "PORT",
        "10000",
    )
)


# ============================================================
# WEATHER SETTINGS
# ============================================================

# ВАЖНО:
#
# Open-Meteo на Render может отдавать 429 из-за общего IP.
#
# Поэтому НЕ делаем запрос при каждом нажатии пользователя.
#
# Все области получаем одним запросом.
#

WEATHER_CACHE_TTL = 3 * 60 * 60

# После 429 вообще не трогаем Open-Meteo
# в течение 30 минут.
WEATHER_429_COOLDOWN = 30 * 60

# Минимальный интервал между реальными запросами
# к Open-Meteo.
WEATHER_REQUEST_INTERVAL = 10

last_weather_request_time = 0.0

weather_request_lock = asyncio.Lock()


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
        "lat": 49.5883,
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

    cur = conn.cursor()

    # --------------------------------------------------------
    # Forecast history
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Feedback
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Weather cache
    #
    # В одной строке хранится JSON всего прогноза
    # конкретной области.
    # --------------------------------------------------------

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_cache (
            region TEXT PRIMARY KEY,
            data TEXT,
            timestamp REAL DEFAULT 0,
            failed_until REAL DEFAULT 0
        )
        """
    )

    # --------------------------------------------------------
    # Compatibility with old DB
    # --------------------------------------------------------

    try:

        cur.execute(
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
# WEATHER CACHE DB
# ============================================================

def load_weather_cache(region):

    conn = get_db()

    cur = conn.cursor()

    cur.execute(
        """
        SELECT data, timestamp, failed_until
        FROM weather_cache
        WHERE region = ?
        """,
        (region,),
    )

    row = cur.fetchone()

    conn.close()

    if not row:
        return None

    data_raw, timestamp, failed_until = row

    return {
        "data": data_raw,
        "timestamp": timestamp or 0,
        "failed_until": failed_until or 0,
    }


def save_weather_cache(
    region,
    data_raw,
    timestamp,
    failed_until=0,
):

    conn = get_db()

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO weather_cache (
            region,
            data,
            timestamp,
            failed_until
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(region)
        DO UPDATE SET
            data = excluded.data,
            timestamp = excluded.timestamp,
            failed_until = excluded.failed_until
        """,
        (
            region,
            data_raw,
            timestamp,
            failed_until,
        ),
    )

    conn.commit()

    conn.close()


def set_weather_failed_until(
    region,
    failed_until,
):

    cache = load_weather_cache(
        region
    )

    data_raw = None
    timestamp = 0

    if cache:

        data_raw = cache["data"]

        timestamp = cache["timestamp"]

    save_weather_cache(
        region,
        data_raw,
        timestamp,
        failed_until,
    )


# ============================================================
# FORECAST HISTORY
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

    cur = conn.cursor()

    cur.execute(
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

    forecast_id = cur.lastrowid

    conn.commit()

    conn.close()

    return forecast_id


def save_feedback_to_db(
    user_id,
    forecast_id,
    rating,
):

    conn = get_db()

    cur = conn.cursor()

    cur.execute(
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

    rows = cur.fetchall()

    conn.close()

    return rows


# ============================================================
# HELPERS
# ============================================================

def safe_float(
    value,
    default=0.0,
):

    try:

        if value is None:
            return default

        return float(value)

    except (
        ValueError,
        TypeError,
    ):

        return default


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

        index = (
            round(
                float(degrees) / 45
            )
            % 8
        )

        return directions[index]

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
    ).total_seconds() / 86400

    phase_days %= 29.53

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

    if 19 <= hour <= 21:

        return (
            "🌇 Захід сонця",
            "Вихід хижака та ляща на мілководдя.",
            14,
        )

    if hour >= 22 or hour < 4:

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

    def __init__(self):

        self.regions = REGIONS

    # --------------------------------------------------------
    # Build ONE request for ALL regions
    # --------------------------------------------------------

    def build_url(self):

        latitudes = ",".join(
            str(v["lat"])
            for v in self.regions.values()
        )

        longitudes = ",".join(
            str(v["lon"])
            for v in self.regions.values()
        )

        hourly = ",".join(
            [
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "surface_pressure",
                "wind_speed_10m",
                "wind_direction_10m",
                "cloud_cover",
                "precipitation",
            ]
        )

        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitudes}"
            f"&longitude={longitudes}"
            f"&hourly={hourly}"
            "&timezone=auto"
            "&past_days=2"
            "&forecast_days=3"
        )

        if OPEN_METEO_API_KEY:

            url += (
                f"&apikey={OPEN_METEO_API_KEY}"
            )

        return url

    # --------------------------------------------------------
    # Fetch ALL regions
    # --------------------------------------------------------

    async def fetch_all(self):

        global last_weather_request_time

        async with weather_request_lock:

            now = time.time()

            elapsed = (
                now
                - last_weather_request_time
            )

            if (
                elapsed
                < WEATHER_REQUEST_INTERVAL
            ):

                wait_time = (
                    WEATHER_REQUEST_INTERVAL
                    - elapsed
                )

                await asyncio.sleep(
                    wait_time
                )

            last_weather_request_time = (
                time.time()
            )

            url = self.build_url()

            logger.info(
                "Open-Meteo: запрос прогноза "
                "для всех областей одним запросом"
            )

            timeout = aiohttp.ClientTimeout(
                total=30,
                connect=10,
            )

            try:

                async with aiohttp.ClientSession(
                    timeout=timeout
                ) as session:

                    async with session.get(
                        url,
                        headers={
                            "User-Agent":
                                "FishingForecastBot/2.0"
                        },
                    ) as response:

                        if response.status == 200:

                            data = (
                                await response.json()
                            )

                            logger.info(
                                "Open-Meteo: данные получены"
                            )

                            return data

                        if response.status == 429:

                            logger.warning(
                                "Open-Meteo 429: "
                                "превышен лимит запросов"
                            )

                            return "RATE_LIMIT"

                        text = (
                            await response.text()
                        )

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
                    "Open-Meteo connection error: %s",
                    e,
                )

                return None

            except Exception as e:

                logger.exception(
                    "Open-Meteo error: %s",
                    e,
                )

                return None

    # --------------------------------------------------------
    # Save multi-location response
    # --------------------------------------------------------

    def save_all_regions(
        self,
        data,
    ):

        now = time.time()

        # Если только одна точка —
        # API может вернуть dict.
        if isinstance(data, dict):

            data_list = [data]

        else:

            data_list = data

        region_names = list(
            self.regions.keys()
        )

        for index, region in enumerate(
            region_names
        ):

            if index >= len(data_list):
                break

            item = data_list[index]

            if not isinstance(item, dict):
                continue

            try:

                import json

                raw = json.dumps(
                    item,
                    ensure_ascii=False,
                )

                save_weather_cache(
                    region,
                    raw,
                    now,
                    0,
                )

            except Exception as e:

                logger.exception(
                    "Ошибка сохранения погоды "
                    "%s: %s",
                    region,
                    e,
                )

    # --------------------------------------------------------
    # Get weather for one region
    # --------------------------------------------------------

    async def get_region_weather(
        self,
        region,
    ):

        if region not in REGIONS:
            return None

        import json

        now = time.time()

        cache = load_weather_cache(
            region
        )

        # ----------------------------------------------------
        # Fresh cache
        # ----------------------------------------------------

        if cache:

            age = (
                now
                - cache["timestamp"]
            )

            if (
                cache["data"]
                and age < WEATHER_CACHE_TTL
            ):

                try:

                    logger.info(
                        "Погода %s: используем "
                        "кэш, возраст %d мин",
                        region,
                        int(age / 60),
                    )

                    return json.loads(
                        cache["data"]
                    )

                except Exception:

                    logger.warning(
                        "Повреждён кэш %s",
                        region,
                    )

        # ----------------------------------------------------
        # 429 cooldown
        # ----------------------------------------------------

        if cache:

            failed_until = (
                cache["failed_until"]
            )

            if now < failed_until:

                if cache["data"]:

                    try:

                        logger.warning(
                            "Погода %s: API "
                            "на cooldown, "
                            "используем старый кэш",
                            region,
                        )

                        return json.loads(
                            cache["data"]
                        )

                    except Exception:
                        pass

                logger.warning(
                    "Погода %s: API на cooldown "
                    "и старых данных нет",
                    region,
                )

                return None

        # ----------------------------------------------------
        # Fetch ALL regions
        # ----------------------------------------------------

        result = await self.fetch_all()

        # ----------------------------------------------------
        # 429
        # ----------------------------------------------------

        if result == "RATE_LIMIT":

            failed_until = (
                now
                + WEATHER_429_COOLDOWN
            )

            for region_name in REGIONS:

                set_weather_failed_until(
                    region_name,
                    failed_until,
                )

            # Пытаемся использовать старый
            # кэш конкретной области.

            if cache and cache["data"]:

                try:

                    return json.loads(
                        cache["data"]
                    )

                except Exception:

                    return None

            return None

        # ----------------------------------------------------
        # Other API error
        # ----------------------------------------------------

        if result is None:

            if cache and cache["data"]:

                try:

                    logger.warning(
                        "Open-Meteo недоступен → "
                        "используем старые данные %s",
                        region,
                    )

                    return json.loads(
                        cache["data"]
                    )

                except Exception:
                    pass

            return None

        # ----------------------------------------------------
        # Save ALL regions
        # ----------------------------------------------------

        self.save_all_regions(
            result
        )

        # ----------------------------------------------------
        # Read requested region
        # ----------------------------------------------------

        fresh = load_weather_cache(
            region
        )

        if not fresh or not fresh["data"]:

            return None

        try:

            return json.loads(
                fresh["data"]
            )

        except Exception as e:

            logger.exception(
                "Ошибка чтения weather cache: %s",
                e,
            )

            return None


# ============================================================
# FISHING ENGINE
# ============================================================

class FishingEngine:

    def __init__(
        self,
        weather_client,
    ):

        self.weather = weather_client

    # --------------------------------------------------------
    # Pressure
    # --------------------------------------------------------

    def pressure_score(
        self,
        pressure_mm,
        predator,
    ):

        optimum = (
            748
            if predator
            else 752
        )

        diff = abs(
            pressure_mm
            - optimum
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

    # --------------------------------------------------------
    # Pressure trend
    # --------------------------------------------------------

    def pressure_trend(
        self,
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

        recent_avg = (
            sum(recent)
            / len(recent)
        )

        older_avg = (
            sum(older)
            / len(older)
        )

        delta_mm = (
            recent_avg
            - older_avg
        ) * 0.75006

        if delta_mm < -2.5:

            return (
                "Сильно падає 📉",
                12,
            )

        if delta_mm < -0.8:

            return (
                "Повільно падає 📉",
                8,
            )

        if delta_mm > 2.5:

            return (
                "Сильно росте 📈",
                -6,
            )

        if delta_mm > 0.8:

            return (
                "Повільно росте 📈",
                2,
            )

        return (
            "Стабільний ✅",
            10,
        )

    # --------------------------------------------------------
    # Stability
    # --------------------------------------------------------

    def stability_score(
        self,
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

        difference = (
            max(values)
            - min(values)
        )

        difference_mm = (
            difference
            * 0.75006
        )

        if difference_mm <= 4:

            return (
                "Дуже стабільний ✅",
                12,
            )

        if difference_mm <= 7:

            return (
                "Стабільний",
                6,
            )

        if difference_mm <= 11:

            return (
                "Помірно мінливий ⚠️",
                -4,
            )

        return (
            "Стрибкоподібний ❌",
            -16,
        )

    # --------------------------------------------------------
    # Temperature
    # --------------------------------------------------------

    def temperature_score(
        self,
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

    # --------------------------------------------------------
    # Wind
    # --------------------------------------------------------

    def wind_score(
        self,
        wind_ms,
        wind_dir,
        predator,
    ):

        if wind_ms < 1.5:

            score = (
                -4
                if predator
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

    # --------------------------------------------------------
    # Precipitation
    # --------------------------------------------------------

    def precip_score(
        self,
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

    # --------------------------------------------------------
    # Clouds
    # --------------------------------------------------------

    def cloud_score(
        self,
        cloud,
        predator,
    ):

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

    # --------------------------------------------------------
    # Stars
    # --------------------------------------------------------

    def stars(
        self,
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

    # --------------------------------------------------------
    # Water temperature approximation
    # --------------------------------------------------------

    def water_temperature(
        self,
        air_temp,
    ):

        # Упрощённая оценка для пресного водоёма.
        #
        # Это НЕ измерение температуры воды.
        #

        value = (
            air_temp * 0.82
            + 3.2
        )

        return round(
            max(
                2,
                min(
                    30,
                    value,
                ),
            ),
            1,
        )

    # --------------------------------------------------------
    # Commentary
    # --------------------------------------------------------

    def commentary(
        self,
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
        score,
        humidity,
        cloud,
        moon,
    ):

        lines = []

        lines.append(
            f"⏱ <b>Час:</b> "
            f"{sun_title}. "
            f"{sun_desc}"
        )

        lines.append(
            f"🌕 <b>Місяць:</b> {moon}"
        )

        lines.append(
            f"🌀 <b>Тиск:</b> "
            f"{pressure} мм | "
            f"{trend} | "
            f"{stability}"
        )

        lines.append(
            f"🌡 <b>Температура:</b> "
            f"повітря {air_temp}°C, "
            f"вода ~{water_temp}°C"
        )

        if water_temp > 25:

            lines.append(
                "   • Спека — "
                "шукайте глибину, "
                "течію та тінь."
            )

        elif water_temp < 9:

            lines.append(
                "   • Холодна вода — "
                "повільна проводка "
                "та невеликі наживки."
            )

        if wind < 2:

            lines.append(
                f"💨 <b>Вітер:</b> "
                f"штиль "
                f"({wind} м/с, {wind_dir})"
            )

        elif wind <= 6:

            lines.append(
                f"💨 <b>Вітер:</b> "
                f"сприятливий "
                f"({wind} м/с, {wind_dir})"
            )

        else:

            lines.append(
                f"💨 <b>Вітер:</b> "
                f"сильний "
                f"({wind} м/с, {wind_dir})"
            )

        if precip > 1.5:

            lines.append(
                f"🌧 <b>Опади:</b> "
                f"{precip} мм"
            )

        if cloud > 65:

            lines.append(
                f"☁️ <b>Хмарність:</b> "
                f"{cloud}%"
            )

        lines.append("")

        lines.append(
            f"🎯 <b>Рекомендації "
            f"по {fish}:</b>"
        )

        if fish in PREDATOR_FISH:

            lines.append(
                "   • Шукайте брівки, "
                "перепади глибини, "
                "корчі та каміння."
            )

            lines.append(
                "   • Активна проводка "
                "слідкує за активністю "
                "хижака."
            )

            if fish == "Щука":

                lines.append(
                    "   • Воблери, блешні, "
                    "живець."
                )

            elif fish == "Окунь":

                lines.append(
                    "   • Мікроджиг, "
                    "вертушки, "
                    "відвідний поводок."
                )

            elif fish == "Сом":

                lines.append(
                    "   • Донка, живець, "
                    "крупна наживка."
                )

        else:

            lines.append(
                "   • Не перегружайте "
                "кормом точку."
            )

            lines.append(
                "   • Мотиль, опариш, "
                "кукурудза."
            )

            if fish == "Лящ":

                lines.append(
                    "   • Фідер або донка."
                )

            elif fish == "Карась":

                lines.append(
                    "   • Поплавок біля "
                    "очерету."
                )

            elif fish == "Короп":

                lines.append(
                    "   • Бойли або "
                    "велика кукурудза."
                )

            elif fish == "Плотва":

                lines.append(
                    "   • Тонка оснастка "
                    "та дрібний гачок."
                )

        lines.append("")

        if score >= 78:

            lines.append(
                "🏆 <b>ВИСНОВОК:</b> "
                "Відмінні умови!"
            )

        elif score >= 55:

            lines.append(
                "⚖️ <b>ВИСНОВОК:</b> "
                "Гарні умови."
            )

        else:

            lines.append(
                "⚠️ <b>ВИСНОВОК:</b> "
                "Складні умови. "
                "Потрібно правильно "
                "вибрати місце."
            )

        lines.append("")

        lines.append(
            "📊 <i>Прогноз розраховано "
            "за погодними даними "
            "Open-Meteo та алгоритмом "
            "кльову.</i>"
        )

        return "\n".join(lines)

    # --------------------------------------------------------
    # Main evaluation
    # --------------------------------------------------------

    async def evaluate(
        self,
        region,
        fish,
        hour,
        day_offset,
    ):

        data = await (
            self.weather
            .get_region_weather(region)
        )

        if not data:

            return None

        hourly = data.get(
            "hourly"
        )

        if not hourly:

            logger.error(
                "Нет hourly для %s",
                region,
            )

            return None

        pressures = hourly.get(
            "surface_pressure",
            [],
        )

        if not pressures:

            return None

        # ----------------------------------------------------
        # Open-Meteo:
        # past_days=2
        # therefore ~48 historical hours
        # ----------------------------------------------------

        target_index = (
            48
            + day_offset * 24
            + hour
        )

        max_index = (
            len(pressures) - 1
        )

        target_index = max(
            0,
            min(
                target_index,
                max_index,
            ),
        )

        def get_hourly(
            key,
            default,
        ):

            values = hourly.get(
                key,
                [],
            )

            if (
                target_index
                < len(values)
            ):

                value = values[
                    target_index
                ]

                if value is not None:

                    return value

            return default

        pressure_hpa = safe_float(
            get_hourly(
                "surface_pressure",
                1013.25,
            ),
            1013.25,
        )

        pressure_mm = (
            pressure_hpa
            * 0.75006
        )

        wind_ms = safe_float(
            get_hourly(
                "wind_speed_10m",
                2.5,
            ),
            2.5,
        )

        wind_degrees = (
            get_hourly(
                "wind_direction_10m",
                None,
            )
        )

        wind_dir = (
            get_wind_direction_text(
                wind_degrees
            )
        )

        air_temp = safe_float(
            get_hourly(
                "temperature_2m",
                18,
            ),
            18,
        )

        precip = safe_float(
            get_hourly(
                "precipitation",
                0,
            ),
            0,
        )

        humidity = safe_float(
            get_hourly(
                "relative_humidity_2m",
                55,
            ),
            55,
        )

        cloud = safe_float(
            get_hourly(
                "cloud_cover",
                40,
            ),
            40,
        )

        water_temp = (
            self.water_temperature(
                air_temp
            )
        )

        predator = (
            fish in PREDATOR_FISH
        )

        score = 48

        stability_text, stability_points = (
            self.stability_score(
                pressures,
                target_index,
            )
        )

        trend_text, trend_points = (
            self.pressure_trend(
                pressures,
                target_index,
            )
        )

        score += stability_points

        score += trend_points

        score += self.pressure_score(
            pressure_mm,
            predator,
        )

        score += self.temperature_score(
            water_temp,
            predator,
        )

        score += self.wind_score(
            wind_ms,
            wind_dir,
            predator,
        )

        score += self.precip_score(
            precip,
            predator,
        )

        score += self.cloud_score(
            cloud,
            predator,
        )

        sun_title, sun_desc, sun_points = (
            check_sun_activity(
                hour
            )
        )

        score += sun_points

        target_date = (
            datetime.now()
            + timedelta(
                days=day_offset
            )
        )

        moon_text, moon_points = (
            get_moon_phase_info(
                target_date
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
                score,
            ),
        )

        stars = self.stars(
            score
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

        expert = self.commentary(
            fish=fish,
            pressure=round(
                pressure_mm,
                1,
            ),
            trend=trend_text,
            stability=stability_text,
            wind=round(
                wind_ms,
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
            score=score,
            humidity=round(
                humidity
            ),
            cloud=round(
                cloud
            ),
            moon=moon_text,
        )

        return {
            "fish": fish,
            "forecast_day": day_text,
            "hour": hour,
            "pressure_mm": round(
                pressure_mm,
                1,
            ),
            "pressure_trend": trend_text,
            "pressure_stability":
                stability_text,
            "wind_ms": round(
                wind_ms,
                1,
            ),
            "wind_dir": wind_dir,
            "humidity": round(
                humidity
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
            "stars_graphic": (
                "⭐" * stars
                + "☆" * (5 - stars)
            ),
            "score_100": score,
            "expert_commentary": expert,
            "sources_used": "Open-Meteo",
        }


# ============================================================
# GLOBAL OBJECTS
# ============================================================

bot = Bot(
    token=BOT_TOKEN
)

storage = MemoryStorage()

dp = Dispatcher(
    storage=storage
)

weather_client = WeatherClient()

fishing_engine = FishingEngine(
    weather_client
)


# ============================================================
# KEYBOARDS
# ============================================================

def regions_keyboard():

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


def fish_keyboard():

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
                KeyboardButton(
                    text="◀️ Змінити область"
                ),
            ],
        ],
        resize_keyboard=True,
    )


def day_keyboard():

    today = datetime.now()

    buttons = []

    for i in range(3):

        date = (
            today
            + timedelta(days=i)
        )

        if i == 0:

            label = (
                f"Сьогодні "
                f"({date.strftime('%d.%m')})"
            )

        elif i == 1:

            label = (
                f"Завтра "
                f"({date.strftime('%d.%m')})"
            )

        else:

            label = (
                f"Післязавтра "
                f"({date.strftime('%d.%m')})"
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


def hour_keyboard():

    return InlineKeyboardMarkup(
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
        reply_markup=regions_keyboard(),
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

    await message.answer(
        "📖 <b>Як користуватися ботом</b>\n\n"
        "1️⃣ Оберіть область.\n"
        "2️⃣ Оберіть рибу.\n"
        "3️⃣ Оберіть день.\n"
        "4️⃣ Оберіть час.\n"
        "5️⃣ Отримайте прогноз.\n\n"
        "<b>Враховується:</b>\n"
        "• температура;\n"
        "• тиск;\n"
        "• тренд тиску;\n"
        "• стабільність тиску;\n"
        "• вітер;\n"
        "• опади;\n"
        "• хмарність;\n"
        "• час доби;\n"
        "• фаза місяця;\n"
        "• вид риби.\n\n"
        "🌦 <b>Джерело:</b> Open-Meteo\n"
        "💾 <b>Кеш:</b> 3 години\n\n"
        "При тимчасовій помилці API бот "
        "використовує останні збережені дані.",
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

    rows = get_user_history_from_db(
        message.from_user.id
    )

    if not rows:

        await message.answer(
            "📜 Історія поки порожня."
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
            f"{graphic}\n"
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
        "🎣 Оберіть рибу:",
        reply_markup=fish_keyboard(),
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

    if not data.get("region"):

        await message.answer(
            "Спочатку оберіть область."
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
        "Оберіть день:",
        reply_markup=day_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# BACK TO FISH
# ============================================================

@dp.callback_query(
    F.data == "back_to_fish"
)
async def back_to_fish(
    callback: CallbackQuery,
    state: FSMContext,
):

    await state.set_state(
        ForecastStates.choosing_fish
    )

    await callback.message.edit_text(
        "🎣 Оберіть рибу."
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
            "Помилка дня.",
            show_alert=True,
        )

        return

    await state.update_data(
        day_offset=day_offset
    )

    await state.set_state(
        ForecastStates.choosing_hour
    )

    await callback.message.edit_text(
        "🕐 <b>Оберіть час доби:</b>",
        reply_markup=hour_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


# ============================================================
# BACK TO DAY
# ============================================================

@dp.callback_query(
    F.data == "back_to_day"
)
async def back_to_day(
    callback: CallbackQuery,
    state: FSMContext,
):

    data = await state.get_data()

    fish = data.get(
        "fish",
        "Риба",
    )

    await state.set_state(
        ForecastStates.choosing_day
    )

    await callback.message.edit_text(
        f"🎣 <b>{fish}</b>\n\n"
        "Оберіть день:",
        reply_markup=day_keyboard(),
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
            "Помилка часу.",
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

    if region not in REGIONS:

        await callback.answer(
            "Спочатку оберіть область.",
            show_alert=True,
        )

        return

    if fish not in FISH_LIST:

        await callback.answer(
            "Спочатку оберіть рибу.",
            show_alert=True,
        )

        return

    await callback.message.edit_text(
        "⏳ <b>Аналізую погоду...</b>\n\n"
        "Отримую метеодані та розраховую "
        "умови кльову.",
        parse_mode="HTML",
    )

    await callback.answer()

    try:

        result = await (
            fishing_engine.evaluate(
                region=region,
                fish=fish,
                hour=hour,
                day_offset=day_offset,
            )
        )

    except Exception as e:

        logger.exception(
            "Forecast calculation error: %s",
            e,
        )

        result = None

    if not result:

        await callback.message.answer(
            "⚠️ <b>Не вдалося отримати "
            "метеодані.</b>\n\n"
            "Open-Meteo зараз може "
            "обмежувати запити.\n\n"
            "Бот не зупинився. "
            "Спробуйте трохи пізніше.",
            reply_markup=regions_keyboard(),
            parse_mode="HTML",
        )

        await state.clear()

        return

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    forecast_id = (
        save_forecast_to_db(
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
    )

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    response = (
        f"📍 <b>{region}</b>\n"
        f"📅 {result['forecast_day']} "
        f"о {result['hour']:02d}:00\n"
        f"🎣 <b>{fish}</b>\n\n"

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

        f"☁️ Хмарність: "
        f"{result['cloud_cover']}%\n\n"

        f"⭐ <b>Кльов: "
        f"{result['stars']}/5</b>\n"

        f"{result['stars_graphic']}\n\n"

        f"📊 Бал: "
        f"<b>{result['score_100']}/100</b>\n\n"

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
                        f"fb_good_"
                        f"{forecast_id}"
                    ),
                ),
                InlineKeyboardButton(
                    text="👎 Хибний",
                    callback_data=(
                        f"fb_bad_"
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

        parts = (
            callback.data.split("_")
        )

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
                "Дякуємо! 👍"
            )

        else:

            text = (
                "Дякуємо за відгук 👎"
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

        stars_int = int(
            stars
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
            f"⭐ {stars}/5 "
            f"({graphic})\n\n"
            f"🎣 Fishing Forecast"
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
            "❌ Не вдалося відправити.",
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
            reply_markup=regions_keyboard(),
        )


# ============================================================
# HEALTH SERVER
# ============================================================

async def health(
    request,
):

    return web.Response(
        text="Fishing bot is running ✅"
    )


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        health,
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

    except Exception as e:

        logger.exception(
            "Telegram authorization failed: %s",
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
        # Telegram token check
        # ----------------------------------------------------

        authorized = (
            await check_telegram()
        )

        if not authorized:

            logger.error(
                "Telegram BOT_TOKEN invalid."
            )

            logger.error(
                "Проверь BOT_TOKEN в "
                "Render Environment Variables."
            )

            return

        # ----------------------------------------------------
        # Remove webhook
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
                "Webhook cleanup warning: %s",
                e,
            )

        # ----------------------------------------------------
        # Polling
        # ----------------------------------------------------

        logger.info(
            "Starting Telegram polling..."
        )

        await dp.start_polling(
            bot,
            polling_timeout=30,
            handle_as_tasks=True,
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

        try:

            await dp.storage.close()

        except Exception as e:

            logger.warning(
                "Storage close error: %s",
                e,
            )

        try:

            await health_runner.cleanup()

        except Exception as e:

            logger.warning(
                "Health server cleanup error: %s",
                e,
            )

        try:

            await bot.session.close()

        except Exception as e:

            logger.warning(
                "Telegram session close error: %s",
                e,
            )

        logger.info(
            "Fishing bot stopped."
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
            "Bot stopped manually."
        )

    except Exception as e:

        logger.exception(
            "Fatal error: %s",
            e,
        )
