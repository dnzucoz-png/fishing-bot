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

API_TOKEN = os.getenv("8373587458:AAEVFuI-yRfE4vTeKT86idwi-0ytbl122T4")

if not API_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. "
        "Добавь BOT_TOKEN в Environment Variables на Render."
    )


GROUP_CHAT_ID = -1004434293069

GROUP_URL = "https://t.me/+rKxYkNg85aAwNzFi"

DATABASE = "fishing_forecast.db"

CACHE_TTL = 45 * 60  # 45 минут


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


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# FSM
# ============================================================

class ForecastStates(StatesGroup):
    choosing_region = State()
    choosing_fish = State()
    choosing_day = State()
    choosing_hour = State()


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False,
    )

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    return conn


def init_db():

    conn = get_db()

    cursor = conn.cursor()

    # --------------------------------------------------------
    # Прогнозы
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Отзывы
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            forecast_id INTEGER,
            rating TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # Кэш Open-Meteo
    # --------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weather_cache (
            cache_key TEXT PRIMARY KEY,
            weather_json TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    """)

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
# WEATHER CACHE SQLITE
# ============================================================

def get_weather_cache(cache_key):

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT weather_json, timestamp

        FROM weather_cache

        WHERE cache_key = ?
        """,
        (cache_key,),
    )

    row = cursor.fetchone()

    conn.close()

    if not row:
        return None

    return row[0], row[1]


def save_weather_cache(
    cache_key,
    weather_json,
):

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT OR REPLACE INTO weather_cache
        (
            cache_key,
            weather_json,
            timestamp
        )
        VALUES (?, ?, ?)
        """,
        (
            cache_key,
            weather_json,
            datetime.now().timestamp(),
        ),
    )

    conn.commit()
    conn.close()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ
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

    return directions[
        round(degrees / 45) % 8
    ]


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
    ).days % 29.53

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


def check_sun_activity(
    hour: int,
) -> Tuple[str, str, int]:

    if 4 <= hour <= 7:

        return (
            "🌅 Світанок (золота година)",
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

    # Один общий lock для всего процесса.
    # Не даём нескольким пользователям одновременно
    # долбить Open-Meteo.

    request_lock = asyncio.Lock()

    def __init__(
        self,
        lat: float,
        lon: float,
    ):

        self.lat = lat
        self.lon = lon

        self.cache_key = (
            f"{lat:.4f}_{lon:.4f}"
        )

    # ========================================================
    # OPEN-METEO
    # ========================================================

    async def fetch_open_meteo(
        self,
        session,
    ):

        url = (
            "https://api.open-meteo.com/v1/forecast"

            f"?latitude={self.lat}"
            f"&longitude={self.lon}"

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

            timeout = aiohttp.ClientTimeout(
                total=15,
                connect=5,
            )

            async with session.get(
                url,
                timeout=timeout,
            ) as response:

                # ==========================================
                # УСПЕХ
                # ==========================================

                if response.status == 200:

                    data = await response.json()

                    logger.info(
                        "Open-Meteo OK: "
                        f"{self.lat},{self.lon}"
                    )

                    return data

                # ==========================================
                # RATE LIMIT
                # ==========================================

                if response.status == 429:

                    logger.warning(
                        "Open-Meteo 429. "
                        "НЕ ЖДУ 25-60 секунд. "
                        "Будет использован кэш."
                    )

                    return None

                # ==========================================
                # ДРУГАЯ ОШИБКА
                # ==========================================

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
                "Open-Meteo network error: %s",
                e,
            )

            return None

        except Exception:

            logger.exception(
                "Open-Meteo unexpected error"
            )

            return None

    # ========================================================
    # ПОЛУЧЕНИЕ ПОГОДЫ
    # ========================================================

    async def get_averaged_weather(self):

        now = datetime.now().timestamp()

        # ====================================================
        # 1. СНАЧАЛА SQLITE CACHE
        # ====================================================

        cached = get_weather_cache(
            self.cache_key
        )

        if cached:

            weather_json, timestamp = cached

            age = now - timestamp

            if age < CACHE_TTL:

                logger.info(
                    "Погода из SQLite cache: "
                    f"{int(age / 60)} мин"
                )

                try:

                    import json

                    return json.loads(
                        weather_json
                    )

                except Exception:

                    logger.exception(
                        "Ошибка чтения weather cache"
                    )

        # ====================================================
        # 2. БЛОКИРУЕМ НОВЫЙ ЗАПРОС
        # ====================================================

        async with self.request_lock:

            # =================================================
            # После ожидания lock ещё раз смотрим cache.
            # Возможно другой пользователь уже получил данные.
            # =================================================

            now = datetime.now().timestamp()

            cached = get_weather_cache(
                self.cache_key
            )

            if cached:

                weather_json, timestamp = cached

                age = now - timestamp

                if age < CACHE_TTL:

                    logger.info(
                        "Погода уже получена "
                        "другим пользователем."
                    )

                    try:

                        import json

                        return json.loads(
                            weather_json
                        )

                    except Exception:

                        pass

            # =================================================
            # 3. ОДИН запрос Open-Meteo
            # =================================================

            logger.info(
                "Запрос Open-Meteo: "
                f"{self.lat},{self.lon}"
            )

            async with aiohttp.ClientSession() as session:

                data = await self.fetch_open_meteo(
                    session
                )

            # =================================================
            # 4. Получили
            # =================================================

            if data:

                try:

                    import json

                    save_weather_cache(
                        self.cache_key,
                        json.dumps(
                            data,
                            ensure_ascii=False,
                        ),
                    )

                except Exception:

                    logger.exception(
                        "Не удалось сохранить weather cache"
                    )

                return data

            # =================================================
            # 5. Open-Meteo недоступен.
            # Берём старый кэш независимо от возраста.
            # =================================================

            cached = get_weather_cache(
                self.cache_key
            )

            if cached:

                weather_json, timestamp = cached

                age = (
                    now - timestamp
                )

                logger.warning(
                    "Open-Meteo недоступен. "
                    "Использую старый cache: "
                    f"{int(age / 60)} мин"
                )

                try:

                    import json

                    return json.loads(
                        weather_json
                    )

                except Exception:

                    logger.exception(
                        "Ошибка старого weather cache"
                    )

            # =================================================
            # 6. Вообще ничего нет
            # =================================================

            logger.error(
                "Нет данных погоды."
            )

            return None

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

        elif diff <= 6:
            return 8

        elif diff <= 10:
            return 0

        elif diff <= 15:
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
                "Сильно падає 📉 (перед фронтом)",
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

    # ========================================================
    # PRESSURE STABILITY
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

        elif 0.2 <= precip <= 1.8:

            return (
                7
                if is_predator
                else 4
            )

        elif precip <= 3.5:

            return -6

        return -16

    # ========================================================
    # CLOUD
    # ========================================================

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

    # ========================================================
    # STARS
    # ========================================================

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
                "• Спека — шукайте "
                "тінь, глибину, течію."
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
                f"({wind_ms} м/с, {wind_dir}). "
                f"Делікатне оснащення."
            )

        elif wind_ms <= 6:

            comments.append(
                f"💨 <b>Вітер:</b> сприятливий "
                f"({wind_ms} м/с, {wind_dir})."
            )

        else:

            comments.append(
                f"💨 <b>Вітер:</b> сильний "
                f"({wind_ms} м/с, {wind_dir}). "
                f"Шукайте підвітряний берег."
            )

        if precip > 1.5:

            comments.append(
                f"🌧 <b>Опади:</b> "
                f"{precip} мм — добре "
                f"для сома, щуки, "
                f"великого ляща."
            )

        elif cloud_cover > 65:

            comments.append(
                f"☁️ <b>Хмарність:</b> "
                f"{cloud_cover}% — "
                f"сприятливо для хижака."
            )

        is_pred = fish_type in [
            "Щука",
            "Окунь",
            "Сом",
        ]

        if is_pred:

            comments.append(
                f"🎯 <b>Для {fish_type}:</b> "
                f"активні проводки "
                f"на брівках і перепадах."
            )

        else:

            comments.append(
                f"🎯 <b>Для {fish_type}:</b> "
                f"дрібна фракція + "
                f"мотиль/опариш/кукурудза."
            )

        if score >= 78:

            comments.append(
                "\n🏆 <b>Підсумок:</b> "
                "Відмінні умови! "
                "Вирушайте на водойму."
            )

        elif score >= 55:

            comments.append(
                "\n⚖️ <b>Підсумок:</b> "
                "Добрі умови. "
                "Успіх залежить від "
                "місця і наживки."
            )

        else:

            comments.append(
                "\n⚠️ <b>Підсумок:</b> "
                "Складні умови. "
                "Потрібні майстерність "
                "і терпіння."
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

            logger.error(
                "В ответе Open-Meteo нет hourly"
            )

            return None

        pressures = hourly.get(
            "surface_pressure",
            [],
        )

        if not pressures:

            logger.error(
                "Нет surface_pressure"
            )

            return None

        max_idx = (
            len(pressures) - 1
        )

        # 48 часов истории +
        # день прогноза +
        # выбранный час

        target_index = min(
            48
            + day_offset * 24
            + target_hour,
            max_idx,
        )

        def safe(
            value,
            default,
        ):

            if value is None:
                return default

            return value

        pressure_hpa = safe(
            pressures[target_index],
            1013.25,
        )

        pressure_mm = (
            pressure_hpa
            * 0.75006
        )

        wind_ms = safe(
            hourly[
                "wind_speed_10m"
            ][target_index],
            2.5,
        )

        temp = safe(
            hourly[
                "temperature_2m"
            ][target_index],
            18.0,
        )

        precip = safe(
            hourly[
                "precipitation"
            ][target_index],
            0.0,
        )

        wind_dir = (
            get_wind_direction_text(
                hourly[
                    "wind_direction_10m"
                ][target_index]
            )
        )

        humidity = safe(
            hourly[
                "relative_humidity_2m"
            ][target_index],
            55,
        )

        cloud_cover = safe(
            hourly[
                "cloud_cover"
            ][target_index],
            40,
        )

        # ====================================================
        # Температура воды
        #
        # Open-Meteo sea_surface_temperature
        # здесь специально НЕ используем.
        #
        # Для Днепра это не температура реки.
        # ====================================================

        water_temp = round(
            temp * 0.82 + 3.2,
            1,
        )

        is_predator = fish_type in [
            "Щука",
            "Окунь",
            "Сом",
        ]

        score = 48

        # Давление — стабильность

        stab_text, stab_pts = (
            self._stability_score(
                pressures,
                target_index,
            )
        )

        score += stab_pts

        # Давление — тренд

        trend_text, trend_pts = (
            self._pressure_trend_score(
                pressures,
                target_index,
            )
        )

        score += trend_pts

        # Давление

        score += self._pressure_score(
            pressure_mm,
            is_predator,
        )

        # Температура

        score += self._temperature_score(
            temp,
            water_temp,
            is_predator,
        )

        # Ветер

        score += self._wind_score(
            wind_ms,
            wind_dir,
            is_predator,
        )

        # Осадки

        score += self._precip_score(
            precip,
            is_predator,
        )

        # Облачность

        score += self._cloud_score(
            cloud_cover,
            is_predator,
        )

        # Солнце

        (
            sun_title,
            sun_desc,
            sun_pts,
        ) = check_sun_activity(
            target_hour
        )

        score += sun_pts

        # Луна

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
            max(0, score),
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

            "pressure_stability": (
                stab_text
            ),

            "pressure_trend": (
                trend_text
            ),

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

            "expert_commentary": (
                commentary
            ),

            "sources_used": (
                "Open-Meteo"
            ),

            "score_100": (
                final_score
            ),
        }


# ============================================================
# BOT
# ============================================================

bot = Bot(
    token=API_TOKEN
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
        "Привіт! 🎣\n\n"
        "Оберіть область для прогнозу кльову:",
        reply_markup=get_regions_keyboard(),
    )


# ============================================================
# HELP
# ============================================================

@dp.message(Command("help"))
@dp.message(
    F.text == "ℹ️ Допомога"
)
async def cmd_help(
    message: Message,
):

    text = (
        "<b>🎣 Як працює прогноз кльову</b>\n\n"

        "• Стабільність тиску за 48 год\n"
        "• Тренд тиску\n"
        "• Абсолютний тиск\n"
        "• Температура повітря\n"
        "• Розрахункова температура води\n"
        "• Вітер і напрямок\n"
        "• Опади\n"
        "• Хмарність\n"
        "• Ранкові та вечірні години\n"
        "• Фаза місяця\n\n"

        "<b>Джерело погоди:</b> Open-Meteo\n\n"

        "Погода кешується, тому тимчасовий "
        "ліміт Open-Meteo не повинен "
        "зупиняти роботу бота."
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
                text="◀️ Назад (до вибору риби)",
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

    day_offset = int(
        callback.data.split("_")[1]
    )

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
                    text="◀️ Назад (до вибору дня)",
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
                text="◀️ Назад (до вибору риби)",
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
# HOUR
# ============================================================

@dp.callback_query(
    F.data.startswith("hour_")
)
async def handle_hour(
    callback: CallbackQuery,
    state: FSMContext,
):

    hour = int(
        callback.data.split("_")[1]
    )

    data = await state.get_data()

    region = data.get(
        "region",
        "Дніпропетровська",
    )

    fish_type = data.get(
        "fish",
        "Лящ",
    )

    day_offset = data.get(
        "day_offset",
        0,
    )

    coords = REGIONS.get(
        region
    )

    if not coords:

        await callback.answer(
            "Помилка області",
            show_alert=True,
        )

        return

    client = MultiSourceWeatherClient(
        coords["lat"],
        coords["lon"],
    )

    await callback.message.edit_text(
        "⏳ Аналізую погоду "
        "та розраховую кльов..."
    )

    try:

        result = await asyncio.wait_for(
            client.evaluate_biting(
                fish_type,
                region,
                hour,
                day_offset,
            ),
            timeout=25,
        )

    except asyncio.TimeoutError:

        logger.error(
            "Forecast calculation timeout"
        )

        await callback.message.answer(
            "⚠️ Сервер погоди відповідає "
            "занадто довго.\n\n"
            "Спробуйте ще раз через хвилину.",
            reply_markup=get_regions_keyboard(),
        )

        await state.clear()
        await callback.answer()

        return

    except Exception:

        logger.exception(
            "Forecast calculation error"
        )

        await callback.message.answer(
            "❌ Виникла помилка під час "
            "розрахунку прогнозу.",
            reply_markup=get_regions_keyboard(),
        )

        await state.clear()
        await callback.answer()

        return

    if not result:

        await callback.message.answer(
            "❌ Не вдалося отримати "
            "метеодані.\n\n"
            "Спробуйте ще раз через "
            "кілька хвилин.",
            reply_markup=get_regions_keyboard(),
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
        f"📍 <b>{region}</b> | "
        f"{result['forecast_day']} "
        f"о {result['hour']:02d}:00\n"

        f"🎣 <b>{fish_type}</b> | "
        f"{result['sources_used']}\n\n"

        f"🌕 {result['moon_phase']}\n"

        f"🌡 "
        f"{result['temperature']}°C "
        f"(вода ~{result['water_temp']}°C)\n"

        f"🌀 Тиск: "
        f"{result['pressure_mm']} мм\n"

        f"   "
        f"{result['pressure_trend']} | "
        f"{result['pressure_stability']}\n"

        f"💨 "
        f"{result['wind_ms']} м/с "
        f"({result['wind_dir']}) | "

        f"🌧 "
        f"{result['precipitation']} мм\n"

        f"☁️ Хмарність: "
        f"{result['cloud_cover']}%\n\n"

        f"⭐ <b>Оцінка кльову: "
        f"{result['stars']}/5</b> "
        f"({result['stars_graphic']})\n"

        f"<i>Внутрішній бал: "
        f"{result['score_100']}/100</i>\n\n"

        f"💡 <b>Експертний аналіз:</b>\n"
        f"{result['expert_commentary']}"
    )

    # ========================================================
    # BUTTONS
    # ========================================================

    kb = InlineKeyboardMarkup(
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

    parts = callback.data.split("_")

    if len(parts) != 3:

        await callback.answer(
            "Помилка",
            show_alert=True,
        )

        return

    rating = parts[1]

    try:

        forecast_id = int(
            parts[2]
        )

    except ValueError:

        await callback.answer(
            "Помилка",
            show_alert=True,
        )

        return

    save_feedback_to_db(
        callback.from_user.id,
        forecast_id,
        rating,
    )

    if rating == "good":

        msg = (
            "Дякуємо! "
            "Відгук допоможе "
            "покращити прогнози 👍"
        )

    else:

        msg = (
            "Дякуємо за "
            "зворотний зв’язок 👎"
        )

    await callback.answer(
        msg,
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
            or "Рибалка"
        )

        text = (
            f"📢 <b>{first_name} "
            f"поділився прогнозом!</b>\n\n"

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
            "✅ Надіслано в чат клубу!",
            show_alert=True,
        )

    except Exception:

        logger.exception(
            "Share error"
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
    request,
):

    return web.Response(
        text="Fishing bot is running ✅"
    )


async def health_info(
    request,
):

    return web.json_response(
        {
            "status": "ok",
            "bot": "Fishing Forecast",
            "time": datetime.now().isoformat(),
        }
    )


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
    # HTTP SERVER
    # --------------------------------------------------------

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        health_info,
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

    # --------------------------------------------------------
    # TELEGRAM POLLING
    # --------------------------------------------------------

    logger.info(
        "Starting Telegram polling..."
    )

    try:

        await dp.start_polling(
            bot,
            allowed_updates=(
                dp.resolve_used_update_types()
            ),
            polling_timeout=30,
        )

    except asyncio.CancelledError:

        logger.warning(
            "Polling cancelled"
        )

        raise

    except Exception:

        logger.exception(
            "CRITICAL POLLING ERROR"
        )

    finally:

        logger.info(
            "Stopping bot..."
        )

        try:

            await bot.session.close()

        except Exception:

            logger.exception(
                "Bot session close error"
            )

        try:

            await runner.cleanup()

        except Exception:

            logger.exception(
                "HTTP runner cleanup error"
            )

        logger.info(
            "Bot stopped."
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

    except Exception:

        logger.exception(
            "FATAL APPLICATION ERROR"
        )
