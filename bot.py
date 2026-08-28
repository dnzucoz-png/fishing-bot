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

logger = logging.getLogger(__name__)


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан. "
        "Добавь BOT_TOKEN в Environment Variables на Render."
    )


GROUP_CHAT_ID = int(
    os.getenv(
        "GROUP_CHAT_ID",
        "-1004434293069",
    )
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
# WEATHER CACHE
# ============================================================

weather_cache = {}

CACHE_TTL = 2 * 60 * 60

RATE_LIMIT_COOLDOWN = 20 * 60


# ============================================================
# DATABASE
# ============================================================

DB_FILE = "fishing_forecast.db"


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

    # Compatibility with old DB
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
# FSM
# ============================================================

class ForecastStates(StatesGroup):

    choosing_region = State()

    choosing_fish = State()

    choosing_day = State()

    choosing_hour = State()


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

        index = round(
            float(degrees) / 45
        ) % 8

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

    else:

        return (
            "Старий місяць 🌘",
            -4,
        )


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

    else:

        return (
            "☀️ День",
            "Стандартна активність.",
            0,
        )


# ============================================================
# WEATHER CLIENT
# ============================================================

class MultiSourceWeatherClient:

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

        url = (
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

        if OPEN_METEO_API_KEY:

            url += (
                f"&apikey={OPEN_METEO_API_KEY}"
            )

        return url

    # --------------------------------------------------------
    # API REQUEST
    # --------------------------------------------------------

    async def fetch_open_meteo(
        self,
        session: aiohttp.ClientSession,
    ):

        url = self.build_url()

        logger.info(
            f"Запрос погоды: "
            f"{self.region_name}"
        )

        timeout = aiohttp.ClientTimeout(
            total=25,
            connect=8,
        )

        try:

            async with session.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent":
                        "FishingForecastBot/1.0"
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
                        f"Погода получена: "
                        f"{self.region_name}"
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
                    f"Open-Meteo HTTP "
                    f"{response.status}: "
                    f"{text[:300]}"
                )

                return None

        except asyncio.TimeoutError:

            logger.warning(
                "Open-Meteo timeout"
            )

            return None

        except aiohttp.ClientError as e:

            logger.warning(
                f"Ошибка соединения Open-Meteo: {e}"
            )

            return None

        except Exception as e:

            logger.exception(
                f"Ошибка Open-Meteo: {e}"
            )

            return None

    # --------------------------------------------------------
    # CACHE
    # --------------------------------------------------------

    async def get_averaged_weather(self):

        now = time.time()

        cache = weather_cache.get(
            self.cache_key
        )

        # Fresh cache
        if cache:

            age = (
                now
                - cache.get(
                    "timestamp",
                    0,
                )
            )

            if age < CACHE_TTL:

                logger.info(
                    f"Используем кеш погоды "
                    f"{self.region_name}, "
                    f"{int(age / 60)} мин"
                )

                return cache.get("data")

        # Rate limit cooldown
        if cache:

            failed_until = cache.get(
                "failed_until",
                0,
            )

            if now < failed_until:

                logger.warning(
                    f"API временно заблокирован "
                    f"для {self.region_name}"
                )

                if cache.get("data"):

                    logger.info(
                        "Используем устаревший кеш"
                    )

                    return cache["data"]

                return None

        # API request
        async with aiohttp.ClientSession() as session:

            result = await self.fetch_open_meteo(
                session
            )

        # 429
        if result == "RATE_LIMIT":

            if cache:

                cache["failed_until"] = (
                    now
                    + RATE_LIMIT_COOLDOWN
                )

                weather_cache[
                    self.cache_key
                ] = cache

                if cache.get("data"):

                    logger.warning(
                        "429 → используем "
                        "старую погоду"
                    )

                    return cache["data"]

            else:

                weather_cache[
                    self.cache_key
                ] = {
                    "data": None,
                    "timestamp": 0,
                    "failed_until":
                        now + RATE_LIMIT_COOLDOWN,
                }

            return None

        # Other error
        if not result:

            if cache and cache.get("data"):

                logger.warning(
                    "API недоступно → "
                    "используем старый кеш"
                )

                return cache["data"]

            return None

        # Save
        weather_cache[
            self.cache_key
        ] = {
            "data": result,
            "timestamp": now,
            "failed_until": 0,
        }

        logger.info(
            f"Новая погода сохранена в кеш: "
            f"{self.region_name}"
        )

        return result

    # ========================================================
    # PRESSURE
    # ========================================================

    def _pressure_score(
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

    def _pressure_trend_score(
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
            avg_recent
            - avg_older
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

    def _stability_score(
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

    def _temperature_score(
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

    def _wind_score(
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

    def _precip_score(
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
    # CLOUDS
    # ========================================================

    def _cloud_score(
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

    def calculate_star_score(
        self,
        score_100: float,
    ) -> int:

        if score_100 >= 84:
            return 5

        if score_100 >= 68:
            return 4

        if score_100 >= 50:
            return 3

        if score_100 >= 32:
            return 2

        if score_100 > 12:
            return 1

        return 0

    # ========================================================
    # COMMENTARY
    # ========================================================

    def generate_expert_commentary(
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
                "   • Спека — шукайте "
                "тінь, глибину та течію."
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
        region: str,
        target_hour: int,
        day_offset: int = 0,
    ):

        data = await self.get_averaged_weather()

        if not data:
            return None

        hourly = data.get("hourly")

        if not hourly:
            return None

        pressures = hourly.get(
            "surface_pressure",
            [],
        )

        if not pressures:
            return None

        max_idx = len(pressures) - 1

        target_index = (
            48
            + day_offset * 24
            + target_hour
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

        wind_ms = safe(
            (
                wind_list[target_index]
                if target_index < len(wind_list)
                else None
            ),
            2.5,
        )

        temp = safe(
            (
                temp_list[target_index]
                if target_index < len(temp_list)
                else None
            ),
            18.0,
        )

        precip = safe(
            (
                precip_list[target_index]
                if target_index < len(precip_list)
                else None
            ),
            0.0,
        )

        wind_degrees = (
            wind_dir_list[target_index]
            if target_index < len(
                wind_dir_list
            )
            else None
        )

        wind_dir = (
            get_wind_direction_text(
                wind_degrees
            )
        )

        humidity = safe(
            (
                humidity_list[target_index]
                if target_index < len(
                    humidity_list
                )
                else None
            ),
            55,
        )

        cloud_cover = safe(
            (
                cloud_list[target_index]
                if target_index < len(
                    cloud_list
                )
                else None
            ),
            40,
        )

        # ----------------------------------------------------
        # WATER TEMPERATURE
        # ----------------------------------------------------

        water_temp = round(
            temp * 0.82 + 3.2,
            1,
        )

        # ----------------------------------------------------
        # SCORE
        # ----------------------------------------------------

        is_predator = (
            fish_type in PREDATOR_FISH
        )

        score = 48

        stab_text, stab_pts = (
            self._stability_score(
                pressures,
                target_index,
            )
        )

        score += stab_pts

        trend_text, trend_pts = (
            self._pressure_trend_score(
                pressures,
                target_index,
            )
        )

        score += trend_pts

        score += self._pressure_score(
            pressure_mm,
            is_predator,
        )

        score += self._temperature_score(
            temp,
            water_temp,
            is_predator,
        )

        score += self._wind_score(
            wind_ms,
            wind_dir,
            is_predator,
        )

        score += self._precip_score(
            precip,
            is_predator,
        )

        score += self._cloud_score(
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
                score,
            ),
        )

        stars = (
            self.calculate_star_score(
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
            self.generate_expert_commentary(
                fish_type,
                round(pressure_mm, 1),
                trend_text,
                stab_text,
                round(wind_ms, 1),
                wind_dir,
                round(precip, 1),
                sun_title,
                sun_desc,
                round(temp, 1),
                round(water_temp, 1),
                final_score,
                round(humidity),
                round(cloud_cover),
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
            "pressure_stability": stab_text,
            "pressure_trend": trend_text,
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
            "moon_phase": moon_text,
            "stars": stars,
            "stars_graphic": (
                "⭐" * stars
                + "☆" * (5 - stars)
            ),
            "expert_commentary": commentary,
            "sources_used": "Open-Meteo",
            "score_100": final_score,
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
                KeyboardButton(
                    text="◀️ Змінити область"
                ),
            ],
        ],
        resize_keyboard=True,
    )


# ============================================================
# START
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
        "🎣 <b>Вітаємо!</b>\n\n"
        "Оберіть область для прогнозу кльову:",
        reply_markup=get_regions_keyboard(),
        parse_mode="HTML",
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
        "• особливості виду риби.\n\n"
        "<b>Погода:</b> Open-Meteo\n"
        "<b>Кеш:</b> 2 години."
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

        hour_str = (
            f"{hour:02d}:00"
            if hour is not None
            else "—"
        )

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

@dp.message(F.text.in_(REGIONS.keys()))
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
        "🎣 Оберіть рибу:",
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

    today = datetime.now()

    buttons = []

    for i in range(3):

        d = today + timedelta(
            days=i
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

    await message.answer(
        f"🎣 Риба: "
        f"<b>{message.text}</b>\n\n"
        "Оберіть день:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
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
        "Оберіть рибу за допомогою "
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
            "Некоректний день.",
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

    await callback.message.edit_text(
        "🕐 Оберіть час доби:",
        reply_markup=keyboard,
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

    today = datetime.now()

    buttons = []

    for i in range(3):

        d = today + timedelta(
            days=i
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

    await callback.message.edit_text(
        f"🎣 Риба: "
        f"<b>{fish_type}</b>\n\n"
        "Оберіть день:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
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
            "Некоректний час.",
            show_alert=True,
        )

        return

    data = await state.get_data()

    region = data.get("region")

    fish_type = data.get("fish")

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

    coords = REGIONS[region]

    client = MultiSourceWeatherClient(
        coords["lat"],
        coords["lon"],
        region,
    )

    await callback.message.edit_text(
        "⏳ <b>Аналізую погоду...</b>\n"
        "Це може зайняти кілька секунд.",
        parse_mode="HTML",
    )

    try:

        result = await client.evaluate_biting(
            fish_type,
            region,
            hour,
            day_offset,
        )

    except Exception as e:

        logger.exception(
            f"Ошибка расчёта: {e}"
        )

        result = None

    if not result:

        await callback.message.answer(
            "⚠️ <b>Зараз не вдалося "
            "отримати метеодані.</b>\n\n"
            "Open-Meteo тимчасово "
            "обмежив запити або "
            "API недоступне.\n\n"
            "Спробуйте ще раз пізніше.",
            reply_markup=get_regions_keyboard(),
            parse_mode="HTML",
        )

        await state.clear()

        await callback.answer()

        return

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    forecast_id = save_forecast_to_db(
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

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    response = (
        f"📍 <b>{region}</b>\n"
        f"📅 {result['forecast_day']} "
        f"о {result['hour']:02d}:00\n"
        f"🎣 <b>{fish_type}</b>\n\n"
        f"🌕 {result['moon_phase']}\n"
        f"🌡 {result['temperature']}°C "
        f"(вода ~{result['water_temp']}°C)\n"
        f"🌀 Тиск: "
        f"{result['pressure_mm']} мм\n"
        f"   {result['pressure_trend']}\n"
        f"   {result['pressure_stability']}\n"
        f"💨 {result['wind_ms']} м/с "
        f"({result['wind_dir']})\n"
        f"🌧 {result['precipitation']} мм\n"
        f"☁️ Хмарність: "
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
                        f"share_"
                        f"{result['stars']}_"
                        f"{fish_type}_"
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
            f"Feedback error: {e}"
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

        stars_int = int(stars)

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
            "поділився прогнозом!\n\n"
            f"📍 {region}\n"
            f"🎣 <b>{fish}</b>\n"
            f"⭐ {stars}/5 "
            f"({graphic})\n\n"
            "🎣 Fishing Forecast"
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
            f"Share error: {e}"
        )

        await callback.answer(
            "❌ Помилка відправки",
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
            "Натисніть /start",
            reply_markup=get_regions_keyboard(),
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

    port = int(
        os.environ.get(
            "PORT",
            "10000",
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    logger.info(
        f"Health server started "
        f"on port {port}"
    )

    return runner


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

    health_runner = None

    try:

        # ----------------------------------------------------
        # DATABASE
        # ----------------------------------------------------

        init_db()

        # ----------------------------------------------------
        # HEALTH SERVER
        # ----------------------------------------------------

        health_runner = (
            await start_health_server()
        )

        # ----------------------------------------------------
        # TELEGRAM TOKEN CHECK
        # ----------------------------------------------------

        logger.info(
            "Checking Telegram bot token..."
        )

        me = await bot.get_me()

        logger.info(
            f"Telegram bot authorized: "
            f"@{me.username} "
            f"id={me.id}"
        )

        # ----------------------------------------------------
        # WEBHOOK
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
                f"Не удалось удалить webhook: {e}"
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
        )

    except Exception as e:

        logger.exception(
            f"FATAL ERROR: {e}"
        )

        raise

    finally:

        # ----------------------------------------------------
        # STOP HEALTH SERVER
        # ----------------------------------------------------

        if health_runner is not None:

            try:

                await health_runner.cleanup()

                logger.info(
                    "Health server stopped"
                )

            except Exception as e:

                logger.warning(
                    f"Health cleanup error: {e}"
                )

        # ----------------------------------------------------
        # CLOSE TELEGRAM
        # ----------------------------------------------------

        try:

            await bot.session.close()

            logger.info(
                "Telegram session closed"
            )

        except Exception as e:

            logger.warning(
                f"Telegram session close error: {e}"
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

        logger.exception(
            f"Bot terminated: {e}"
        )
