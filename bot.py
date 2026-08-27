import asyncio
import logging
import os
import sqlite3
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
# НАСТРОЙКИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("fishing_bot")

# ------------------------------------------------------------
# TELEGRAM TOKEN
# ------------------------------------------------------------
# На Render создай:
#
# BOT_TOKEN = твой токен
#
# Если переменной нет, бот не стартует.
# ------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. "
        "На Render открой Environment → Add Environment Variable "
        "и добавь BOT_TOKEN."
    )


# ============================================================
# TELEGRAM / ГРУППА
# ============================================================

GROUP_CHAT_ID = -1004434293069

GROUP_URL = "https://t.me/+rKxYkNg85aAwNzFi"


# ============================================================
# ОБЛАСТИ
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
# РЫБА
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


# ============================================================
# CACHE
# ============================================================

weather_cache = {}

# 45 минут
CACHE_TTL = 45 * 60

# Если Open-Meteo отдал 429,
# не долбим API снова несколько минут.
RATE_LIMIT_COOLDOWN = 5 * 60

weather_rate_limit = {}


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

DB_FILE = "fishing_forecast.db"


def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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

    conn.commit()
    conn.close()

    logger.info("SQLite database initialized")


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
    conn.close()


def get_user_history_from_db(user_id):

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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

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

    return directions[round(degrees / 45) % 8]


def get_moon_phase_info(date_obj: datetime) -> Tuple[str, int]:

    known_new_moon = datetime(2024, 1, 11)

    phase_days = (
        date_obj - known_new_moon
    ).total_seconds() / 86400 % 29.53

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

    def __init__(self, lat: float, lon: float):

        self.lat = lat
        self.lon = lon

        self.cache_key = (
            f"{round(lat, 4)}_{round(lon, 4)}"
        )

    # --------------------------------------------------------
    # Open-Meteo
    # --------------------------------------------------------

    async def fetch_open_meteo(
        self,
        session,
        model: Optional[str] = None,
    ):

        now = datetime.now().timestamp()

        # Проверяем cooldown после 429
        last_limit = weather_rate_limit.get(
            self.cache_key,
            0,
        )

        if now - last_limit < RATE_LIMIT_COOLDOWN:

            logger.warning(
                "Open-Meteo cooldown для %s",
                self.cache_key,
            )

            return None

        model_param = ""

        if model:
            model_param = f"&models={model}"

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
            "precipitation,"
            "weather_code"
            f"{model_param}"
            "&timezone=auto"
            "&past_days=2"
            "&forecast_days=5"
        )

        for attempt in range(3):

            try:

                timeout = aiohttp.ClientTimeout(
                    total=20,
                    connect=8,
                )

                headers = {
                    "User-Agent": "FishingForecastBot/1.0"
                }

                async with session.get(
                    url,
                    timeout=timeout,
                    headers=headers,
                ) as response:

                    if response.status == 200:

                        data = await response.json()

                        logger.info(
                            "Open-Meteo OK: %s %s",
                            self.lat,
                            self.lon,
                        )

                        return data

                    if response.status == 429:

                        weather_rate_limit[
                            self.cache_key
                        ] = datetime.now().timestamp()

                        logger.error(
                            "Open-Meteo 429: rate limit"
                        )

                        return None

                    logger.error(
                        "Open-Meteo HTTP %s",
                        response.status,
                    )

                    if attempt < 2:

                        await asyncio.sleep(
                            2 + attempt * 2
                        )

            except asyncio.TimeoutError:

                logger.warning(
                    "Open-Meteo timeout, attempt %s",
                    attempt + 1,
                )

                if attempt < 2:
                    await asyncio.sleep(2)

            except aiohttp.ClientError as e:

                logger.warning(
                    "Open-Meteo network error: %s",
                    e,
                )

                if attempt < 2:
                    await asyncio.sleep(2)

            except Exception as e:

                logger.exception(
                    "Unexpected Open-Meteo error: %s",
                    e,
                )

                break

        return None

    # --------------------------------------------------------
    # Получение погоды
    # --------------------------------------------------------

    async def get_weather(self):

        now = datetime.now().timestamp()

        # ----------------------------------------------------
        # CACHE
        # ----------------------------------------------------

        cached = weather_cache.get(
            self.cache_key
        )

        if cached:

            data, timestamp = cached

            if now - timestamp < CACHE_TTL:

                logger.info(
                    "Weather cache HIT: %s",
                    self.cache_key,
                )

                return data

            else:

                logger.info(
                    "Weather cache expired: %s",
                    self.cache_key,
                )

        # ----------------------------------------------------
        # REQUEST
        # ----------------------------------------------------

        logger.info(
            "Weather cache MISS: %s",
            self.cache_key,
        )

        try:

            timeout = aiohttp.ClientTimeout(
                total=25
            )

            async with aiohttp.ClientSession(
                timeout=timeout
            ) as session:

                data = await self.fetch_open_meteo(
                    session
                )

                if data:

                    weather_cache[
                        self.cache_key
                    ] = (
                        data,
                        datetime.now().timestamp(),
                    )

                    return data

        except Exception as e:

            logger.exception(
                "Weather session error: %s",
                e,
            )

        return None

    # --------------------------------------------------------
    # Старое имя оставляем для совместимости
    # --------------------------------------------------------

    async def get_averaged_weather(self):

        return await self.get_weather()

    # ========================================================
    # SCORE
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

        elif diff <= 6:
            return 8

        elif diff <= 10:
            return 0

        elif diff <= 15:
            return -10

        return -18

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

        if len(recent) < 5 or len(older) < 5:

            return (
                "Недостатньо даних",
                0,
            )

        avg_recent = (
            sum(recent) / len(recent)
        )

        avg_older = (
            sum(older) / len(older)
        )

        delta = (
            avg_recent - avg_older
        ) * 0.75006

        if delta < -2.5:

            return (
                "Сильно падає 📉",
                12,
            )

        elif delta < -0.8:

            return (
                "Повільно падає 📉",
                8,
            )

        elif delta > 2.5:

            return (
                "Сильно росте 📈",
                -6,
            )

        elif delta > 0.8:

            return (
                "Повільно росте 📈",
                2,
            )

        return (
            "Стабільний ✅",
            10,
        )

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
            max(valid) -
            min(valid)
        )

        if diff <= 4:

            return (
                "Дуже стабільний ✅",
                12,
            )

        elif diff <= 7:

            return (
                "Стабільний",
                6,
            )

        elif diff <= 11:

            return (
                "Помірно мінливий ⚠️",
                -4,
            )

        return (
            "Стрибкоподібний ❌",
            -16,
        )

    def _temperature_score(
        self,
        temp: float,
        water_temp: float,
        is_predator: bool,
    ) -> int:

        if is_predator:

            if 8 <= water_temp <= 16:
                return 12

            elif 5 <= water_temp <= 20:
                return 6

            elif (
                water_temp > 24
                or water_temp < 3
            ):
                return -10

            return 0

        else:

            if 16 <= water_temp <= 23:
                return 12

            elif 12 <= water_temp <= 26:
                return 6

            elif (
                water_temp > 28
                or water_temp < 8
            ):
                return -8

            return 0

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

    def _precip_score(
        self,
        precip: float,
        is_predator: bool,
    ) -> int:

        if precip <= 0.1:

            return 0

        elif 0.2 <= precip <= 1.8:

            return (
                7
                if is_predator
                else 4
            )

        elif precip <= 3.5:

            return -6

        return -16

    def _cloud_score(
        self,
        cloud: float,
        is_predator: bool,
    ) -> int:

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

    def calculate_star_score(
        self,
        score_100: float,
    ) -> int:

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

        comments = [
            (
                f"⏱ <b>Час:</b> "
                f"{sun_title}. "
                f"{sun_desc}"
            ),

            (
                f"🌕 <b>Місяць:</b> "
                f"{moon_text}"
            ),

            (
                f"🌀 <b>Тиск:</b> "
                f"{pressure_mm} мм | "
                f"{trend_text} | "
                f"{stability_text}"
            ),

            (
                f"🌡 <b>Температура:</b> "
                f"повітря {temp}°C, "
                f"вода ~{water_temp}°C"
            ),
        ]

        if water_temp > 25:

            comments.append(
                "• Спека — шукайте тінь, "
                "глибину та течію."
            )

        elif water_temp < 9:

            comments.append(
                "• Холодна вода — "
                "дрібні наживки, "
                "повільна подача."
            )

        if wind_ms < 2:

            comments.append(
                f"💨 <b>Вітер:</b> штиль "
                f"({wind_ms} м/с, {wind_dir})."
            )

        elif wind_ms <= 6:

            comments.append(
                f"💨 <b>Вітер:</b> "
                f"сприятливий "
                f"({wind_ms} м/с, {wind_dir})."
            )

        else:

            comments.append(
                f"💨 <b>Вітер:</b> "
                f"сильний "
                f"({wind_ms} м/с, {wind_dir})."
            )

        if precip > 1.5:

            comments.append(
                f"🌧 <b>Опади:</b> "
                f"{precip} мм."
            )

        elif cloud_cover > 65:

            comments.append(
                f"☁️ <b>Хмарність:</b> "
                f"{cloud_cover}%."
            )

        is_pred = fish_type in {
            "Щука",
            "Окунь",
            "Сом",
        }

        if is_pred:

            comments.append(
                f"🎯 <b>Для {fish_type}:</b> "
                f"активні проводки на "
                f"брівках і перепадах."
            )

        else:

            comments.append(
                f"🎯 <b>Для {fish_type}:</b> "
                f"дрібна фракція + "
                f"мотиль/опариш/кукурудза."
            )

        if score >= 78:

            comments.append(
                "🏆 <b>Підсумок:</b> "
                "Відмінні умови! "
                "Можна їхати на водойму."
            )

        elif score >= 55:

            comments.append(
                "⚖️ <b>Підсумок:</b> "
                "Добрі умови. "
                "Багато залежить від місця."
            )

        else:

            comments.append(
                "⚠️ <b>Підсумок:</b> "
                "Складні умови. "
                "Потрібні місце і терпіння."
            )

        return "\n".join(comments)

    # ========================================================
    # НАХОЖДЕНИЕ НУЖНОГО ЧАСА
    # ========================================================

    def find_target_index(
        self,
        hourly,
        target_datetime: datetime,
    ):

        times = hourly.get("time", [])

        if not times:
            return None

        target_str = (
            target_datetime.strftime(
                "%Y-%m-%dT%H:00"
            )
        )

        # Точное совпадение
        for index, value in enumerate(times):

            if value == target_str:
                return index

        # Если точного совпадения нет,
        # ищем ближайшее время.
        best_index = None
        best_diff = None

        for index, value in enumerate(times):

            try:

                dt = datetime.fromisoformat(
                    value
                )

                diff = abs(
                    (
                        dt -
                        target_datetime
                    ).total_seconds()
                )

                if (
                    best_diff is None
                    or diff < best_diff
                ):

                    best_diff = diff
                    best_index = index

            except Exception:
                continue

        return best_index

    # ========================================================
    # ОЦЕНКА КЛЕВА
    # ========================================================

    async def evaluate_biting(
        self,
        fish_type: str,
        region: str,
        target_hour: int,
        day_offset: int = 0,
    ):

        data = await self.get_weather()

        if not data:

            return None

        hourly = data.get("hourly")

        if not hourly:

            logger.error(
                "Open-Meteo response has no hourly"
            )

            return None

        # ----------------------------------------------------
        # ЦЕЛЕВАЯ ДАТА
        # ----------------------------------------------------

        today = datetime.now()

        target_datetime = (
            today.replace(
                hour=target_hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            + timedelta(days=day_offset)
        )

        target_index = self.find_target_index(
            hourly,
            target_datetime,
        )

        if target_index is None:

            logger.error(
                "Не найден час %s",
                target_datetime,
            )

            return None

        logger.info(
            "Forecast target: %s index=%s",
            target_datetime,
            target_index,
        )

        # ----------------------------------------------------
        # SAFE
        # ----------------------------------------------------

        def safe(
            values,
            index,
            default,
        ):

            try:

                value = values[index]

                if value is None:
                    return default

                return value

            except Exception:

                return default

        pressures = hourly.get(
            "surface_pressure",
            [],
        )

        pressure_hpa = safe(
            pressures,
            target_index,
            1013.25,
        )

        pressure_mm = (
            pressure_hpa * 0.75006
        )

        wind_values = hourly.get(
            "wind_speed_10m",
            [],
        )

        wind_ms = safe(
            wind_values,
            target_index,
            2.5,
        )

        temp_values = hourly.get(
            "temperature_2m",
            [],
        )

        temp = safe(
            temp_values,
            target_index,
            18.0,
        )

        precip_values = hourly.get(
            "precipitation",
            [],
        )

        precip = safe(
            precip_values,
            target_index,
            0.0,
        )

        wind_direction_values = hourly.get(
            "wind_direction_10m",
            [],
        )

        wind_degrees = safe(
            wind_direction_values,
            target_index,
            0,
        )

        wind_dir = get_wind_direction_text(
            wind_degrees
        )

        humidity_values = hourly.get(
            "relative_humidity_2m",
            [],
        )

        humidity = safe(
            humidity_values,
            target_index,
            55,
        )

        cloud_values = hourly.get(
            "cloud_cover",
            [],
        )

        cloud_cover = safe(
            cloud_values,
            target_index,
            40,
        )

        # ----------------------------------------------------
        # ВОДА
        # ----------------------------------------------------
        #
        # Open-Meteo для речного места не даёт
        # реальную температуру воды.
        #
        # Поэтому здесь используем приблизительную модель.
        # ----------------------------------------------------

        water_temp = round(
            temp * 0.82 + 3.2,
            1,
        )

        is_predator = fish_type in {
            "Щука",
            "Окунь",
            "Сом",
        }

        # ----------------------------------------------------
        # SCORE
        # ----------------------------------------------------

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

        moon_text, moon_pts = (
            get_moon_phase_info(
                target_datetime
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

        stars = self.calculate_star_score(
            final_score
        )

        # ----------------------------------------------------
        # DATE
        # ----------------------------------------------------

        date_str = target_datetime.strftime(
            "%d.%m.%Y"
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

        # ----------------------------------------------------
        # COMMENTARY
        # ----------------------------------------------------

        commentary = (
            self.generate_expert_commentary(
                fish_type,
                round(
                    pressure_mm,
                    1,
                ),
                trend_text,
                stab_text,
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
                stab_text,

            "pressure_trend":
                trend_text,

            "wind_ms": round(
                wind_ms,
                1,
            ),

            "wind_dir":
                wind_dir,

            "humidity":
                round(humidity),

            "cloud_cover":
                round(cloud_cover),

            "precipitation":
                round(
                    precip,
                    1,
                ),

            "temperature":
                round(
                    temp,
                    1,
                ),

            "water_temp":
                round(
                    water_temp,
                    1,
                ),

            "moon_phase":
                moon_text,

            "stars":
                stars,

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
# BOT
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
        "Привіт! 🎣\n\n"
        "Оберіть область для прогнозу кльову:",
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
        "<b>🎣 Як працює прогноз кльову</b>\n\n"

        "• Стабільність тиску\n"
        "• Тренд тиску\n"
        "• Атмосферний тиск\n"
        "• Температура\n"
        "• Вітер\n"
        "• Опади\n"
        "• Хмарність\n"
        "• Час доби\n"
        "• Фаза місяця\n\n"

        "<b>Джерело погоди:</b>\n"
        "Open-Meteo\n\n"

        "⚠️ Прогноз кльову є розрахунковим "
        "і не гарантує улов."
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

    try:

        rows = get_user_history_from_db(
            message.from_user.id
        )

    except Exception as e:

        logger.exception(
            "History error: %s",
            e,
        )

        await message.answer(
            "❌ Не вдалося отримати історію."
        )

        return

    if not rows:

        await message.answer(
            "У вас поки немає "
            "збережених прогнозів."
        )

        return

    text = (
        "<b>📜 Ваші останні прогнози:</b>\n\n"
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
            f"📍 {region} | 🎣 {fish}\n"
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
        f"Область: "
        f"<b>{message.text}</b>\n\n"
        f"Оберіть рибу:",
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
            "через /start."
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
        f"Риба: "
        f"<b>{message.text}</b>\n\n"
        f"Оберіть день:",
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
            "Помилка.",
            show_alert=True,
        )

        return

    await state.update_data(
        day_offset=day_offset
    )

    await state.set_state(
        ForecastStates.choosing_hour
    )

    kb = InlineKeyboardMarkup(
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
        "Оберіть час доби:",
        reply_markup=kb,
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
        "Рибу",
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
        f"Риба: "
        f"<b>{fish_type}</b>\n\n"
        f"Оберіть день:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
        parse_mode="HTML",
    )

    await callback.answer()


# ============================================================
# HOUR / MAIN FORECAST
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

    fish_type = data.get(
        "fish"
    )

    day_offset = data.get(
        "day_offset",
        0,
    )

    if region not in REGIONS:

        await callback.message.answer(
            "❌ Область не вибрана. "
            "Натисніть /start."
        )

        await state.clear()
        await callback.answer()

        return

    if fish_type not in FISH_LIST:

        await callback.message.answer(
            "❌ Риба не вибрана. "
            "Натисніть /start."
        )

        await state.clear()
        await callback.answer()

        return

    coords = REGIONS[
        region
    ]

    client = MultiSourceWeatherClient(
        coords["lat"],
        coords["lon"],
    )

    await callback.message.edit_text(
        "⏳ <b>Аналізую погоду...</b>\n\n"
        "Отримую метеодані та "
        "розраховую кльов 🎣",
        parse_mode="HTML",
    )

    try:

        result = await asyncio.wait_for(
            client.evaluate_biting(
                fish_type,
                region,
                hour,
                day_offset,
            ),
            timeout=40,
        )

    except asyncio.TimeoutError:

        logger.error(
            "Forecast timeout"
        )

        await callback.message.answer(
            "❌ Open-Meteo занадто довго "
            "не відповідає.\n\n"
            "Спробуйте ще раз через кілька хвилин.",
            reply_markup=get_regions_keyboard(),
        )

        await state.clear()
        await callback.answer()

        return

    except Exception as e:

        logger.exception(
            "Forecast calculation error: %s",
            e,
        )

        await callback.message.answer(
            "❌ Помилка під час розрахунку "
            "прогнозу.",
            reply_markup=get_regions_keyboard(),
        )

        await state.clear()
        await callback.answer()

        return

    if not result:

        await callback.message.answer(
            "❌ Не вдалося отримати "
            "метеодані.\n\n"
            "Open-Meteo тимчасово обмежив "
            "запити або не відповідає.\n\n"
            "Спробуйте ще раз через "
            "кілька хвилин.",
            reply_markup=get_regions_keyboard(),
        )

        await state.clear()
        await callback.answer()

        return

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    try:

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

    except Exception as e:

        logger.exception(
            "Database save error: %s",
            e,
        )

        forecast_id = 0

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    response = (
        f"📍 <b>{region}</b>\n"
        f"{result['forecast_day']} "
        f"о {result['hour']:02d}:00\n\n"

        f"🎣 <b>{fish_type}</b>\n"
        f"Джерело: {result['sources_used']}\n\n"

        f"🌕 {result['moon_phase']}\n"

        f"🌡 "
        f"{result['temperature']}°C "
        f"(вода ~{result['water_temp']}°C)\n"

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

        f"<i>Розрахунковий бал: "
        f"{result['score_100']}/100</i>\n\n"

        f"💡 <b>Експертний аналіз:</b>\n"
        f"{result['expert_commentary']}"
    )

    # --------------------------------------------------------
    # BUTTONS
    # --------------------------------------------------------

    kb_rows = [
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
    ]

    if forecast_id:

        kb_rows.append(
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
            ]
        )

    kb = InlineKeyboardMarkup(
        inline_keyboard=kb_rows
    )

    await callback.message.answer(
        response,
        reply_markup=kb,
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

            msg = (
                "Дякуємо! "
                "Відгук допоможе покращити "
                "прогнози 👍"
            )

        else:

            msg = (
                "Дякуємо за зворотний "
                "зв’язок 👎"
            )

        await callback.answer(
            msg,
            show_alert=True,
        )

    except Exception as e:

        logger.exception(
            "Feedback error: %s",
            e,
        )

        await callback.answer(
            "❌ Помилка.",
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
            or "Рибалка"
        )

        text = (
            f"📢 <b>{first_name} "
            f"поділився прогнозом!</b>\n\n"

            f"📍 {region}\n"
            f"🎣 <b>{fish}</b>\n"

            f"⭐ {stars_int}/5 "
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
            "повідомлення в чат.",
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

    try:

        current_state = (
            await state.get_state()
        )

        if current_state is None:

            await message.answer(
                "Натисніть /start 👇",
                reply_markup=(
                    get_regions_keyboard()
                ),
            )

    except Exception as e:

        logger.exception(
            "Fallback error: %s",
            e,
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


async def health_head(
    request: web.Request,
):

    return web.Response(
        text="OK"
    )


# ============================================================
# WEB SERVER
# ============================================================

async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_head(
        "/",
        health_head,
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
        "Health server started on port %s",
        port,
    )

    return runner


# ============================================================
# BOT POLLING
# ============================================================

async def start_bot():

    logger.info(
        "Starting Telegram bot..."
    )

    # Проверяем токен до polling
    try:

        me = await bot.get_me()

        logger.info(
            "Telegram bot connected: @%s id=%s",
            me.username,
            me.id,
        )

    except Exception as e:

        logger.exception(
            "Telegram token/API error: %s",
            e,
        )

        raise

    while True:

        try:

            logger.info(
                "Starting polling..."
            )

            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
            )

        except asyncio.CancelledError:

            logger.info(
                "Polling cancelled"
            )

            raise

        except Exception as e:

            logger.exception(
                "Polling crashed: %s",
                e,
            )

            logger.info(
                "Restart polling in 10 seconds..."
            )

            await asyncio.sleep(10)


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "===================================="
    )

    logger.info(
        "🎣 FISHING FORECAST BOT"
    )

    logger.info(
        "Starting..."
    )

    logger.info(
        "===================================="
    )

    # DB
    init_db()

    # Health
    health_runner = (
        await start_health_server()
    )

    try:

        # Telegram
        await start_bot()

    finally:

        logger.info(
            "Stopping health server..."
        )

        await health_runner.cleanup()

        await bot.session.close()


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

    except Exception as e:

        logger.exception(
            "FATAL ERROR: %s",
            e,
        )
