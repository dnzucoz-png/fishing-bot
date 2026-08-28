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
from aiogram.exceptions import TelegramConflictError


# ============================================================
# НАСТРОЙКИ
# ============================================================

API_TOKEN = os.environ.get("BOT_TOKEN")

if not API_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. "
        "Добавь BOT_TOKEN в Environment Variables на Render."
    )

GROUP_CHAT_ID = -1004434293069
GROUP_URL = "https://t.me/+rKxYkNg85aAwNzFi"

DB_FILE = "fishing_forecast.db"

REGIONS = {
    "Дніпропетровська": {"lat": 48.4647, "lon": 35.0462},
    "Київська": {"lat": 50.4501, "lon": 30.5234},
    "Полтавська": {"lat": 49.5895, "lon": 34.5514},
    "Запорізька": {"lat": 47.8388, "lon": 35.1396},
    "Черкаська": {"lat": 49.4444, "lon": 32.0598},
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

# Кэш в памяти.
# ВАЖНО: это только быстрый кэш.
# Основной резервный кэш находится в SQLite.
weather_cache = {}

# 45 минут
CACHE_TTL = 45 * 60

# После 429 не пытаемся долбить API постоянно.
RATE_LIMIT_COOLDOWN = 10 * 60

# Защита от одновременных запросов.
weather_locks = {}

# Последний момент 429 для каждого региона.
rate_limit_until = {}


# ============================================================
# СОСТОЯНИЯ FSM
# ============================================================

class ForecastStates(StatesGroup):
    choosing_region = State()
    choosing_fish = State()
    choosing_day = State()
    choosing_hour = State()


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # История прогнозов
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

    # Отзывы
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            forecast_id INTEGER,
            rating TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Кэш погоды
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weather_cache (
            cache_key TEXT PRIMARY KEY,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            weather_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)

    conn.commit()
    conn.close()

    logging.info("SQLite database initialized")


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

    cursor.execute("""
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
    """, (
        user_id,
        region,
        fish_type,
        forecast_day,
        hour,
        pressure,
        wind,
        temp,
        stars,
    ))

    forecast_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return forecast_id


def save_feedback_to_db(user_id, forecast_id, rating):
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


def get_user_history_from_db(user_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
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
    """, (user_id,))

    rows = cursor.fetchall()

    conn.close()

    return rows


def save_weather_cache(
    cache_key: str,
    latitude: float,
    longitude: float,
    weather_json: str,
):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR REPLACE INTO weather_cache (
            cache_key,
            latitude,
            longitude,
            weather_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        cache_key,
        latitude,
        longitude,
        weather_json,
        time.time(),
    ))

    conn.commit()
    conn.close()


def load_weather_cache(cache_key: str):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            weather_json,
            created_at
        FROM weather_cache
        WHERE cache_key = ?
    """, (cache_key,))

    row = cursor.fetchone()

    conn.close()

    if not row:
        return None, None

    return row[0], row[1]


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
# ПОГОДНЫЙ КЛИЕНТ
# ============================================================

class MultiSourceWeatherClient:

    def __init__(self, lat: float, lon: float):

        self.lat = lat
        self.lon = lon

        self.cache_key = f"{lat:.4f}_{lon:.4f}"

        if self.cache_key not in weather_locks:
            weather_locks[self.cache_key] = asyncio.Lock()

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    def build_url(self, host="api.open-meteo.com"):

        return (
            f"https://{host}/v1/forecast"
            f"?latitude={self.lat}"
            f"&longitude={self.lon}"
            f"&hourly="
            f"temperature_2m,"
            f"apparent_temperature,"
            f"relative_humidity_2m,"
            f"surface_pressure,"
            f"wind_speed_10m,"
            f"wind_direction_10m,"
            f"cloud_cover,"
            f"precipitation,"
            f"sea_surface_temperature"
            f"&timezone=auto"
            f"&past_days=2"
            f"&forecast_days=3"
        )

    # --------------------------------------------------------
    # Запрос к Open-Meteo
    # --------------------------------------------------------

    async def request_open_meteo(
        self,
        session,
        host,
    ):

        url = self.build_url(host)

        timeout = aiohttp.ClientTimeout(
            total=20,
            connect=8,
            sock_read=15,
        )

        try:

            async with session.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": "FishingForecastBot/1.0",
                    "Accept": "application/json",
                },
            ) as response:

                if response.status == 200:

                    data = await response.json()

                    if "hourly" not in data:
                        logging.error(
                            "Open-Meteo ответил без hourly"
                        )
                        return None

                    logging.info(
                        "Weather received from %s",
                        host,
                    )

                    return data

                if response.status == 429:

                    logging.warning(
                        "Open-Meteo 429: превышен лимит запросов (%s)",
                        host,
                    )

                    return "429"

                text = await response.text()

                logging.error(
                    "Open-Meteo HTTP %s: %s",
                    response.status,
                    text[:300],
                )

                return None

        except asyncio.TimeoutError:

            logging.warning(
                "Open-Meteo timeout (%s)",
                host,
            )

            return None

        except aiohttp.ClientError as e:

            logging.warning(
                "Open-Meteo connection error (%s): %s",
                host,
                e,
            )

            return None

        except Exception as e:

            logging.exception(
                "Open-Meteo unexpected error: %s",
                e,
            )

            return None

    # --------------------------------------------------------
    # Получение погоды
    # --------------------------------------------------------

    async def get_averaged_weather(self):

        now = time.time()

        # ====================================================
        # 1. Быстрый RAM-кэш
        # ====================================================

        cached = weather_cache.get(self.cache_key)

        if cached:

            data, timestamp = cached

            if now - timestamp < CACHE_TTL:

                logging.info(
                    "Использую RAM-кэш погоды: %s",
                    self.cache_key,
                )

                return data

        # ====================================================
        # 2. SQLite-кэш
        # ====================================================

        cached_json, cached_timestamp = load_weather_cache(
            self.cache_key
        )

        if cached_json:

            try:

                import json

                data = json.loads(cached_json)

                age = now - cached_timestamp

                # Свежий SQLite-кэш
                if age < CACHE_TTL:

                    weather_cache[self.cache_key] = (
                        data,
                        cached_timestamp,
                    )

                    logging.info(
                        "Использую SQLite-кэш погоды: %s",
                        self.cache_key,
                    )

                    return data

            except Exception as e:

                logging.warning(
                    "Ошибка чтения SQLite weather cache: %s",
                    e,
                )

        # ====================================================
        # 3. Проверяем cooldown после 429
        # ====================================================

        cooldown = rate_limit_until.get(
            self.cache_key,
            0,
        )

        if now < cooldown:

            logging.warning(
                "Open-Meteo временно заблокирован после 429. "
                "Пробую использовать старый SQLite-кэш."
            )

            if cached_json:

                try:

                    import json

                    data = json.loads(cached_json)

                    weather_cache[self.cache_key] = (
                        data,
                        cached_timestamp,
                    )

                    return data

                except Exception:
                    pass

            logging.error(
                "Нет ни свежей, ни сохранённой погоды."
            )

            return None

        # ====================================================
        # 4. Только один запрос одновременно
        # ====================================================

        lock = weather_locks[self.cache_key]

        async with lock:

            # Пока ждали lock, другой запрос мог уже
            # получить свежую погоду.
            now = time.time()

            cached = weather_cache.get(self.cache_key)

            if cached:

                data, timestamp = cached

                if now - timestamp < CACHE_TTL:

                    return data

            # =================================================
            # 5. Общая HTTP-сессия
            # =================================================

            async with aiohttp.ClientSession() as session:

                # Основной Open-Meteo
                result = await self.request_open_meteo(
                    session,
                    "api.open-meteo.com",
                )

                # Если 429 — НЕ долбим основной API повторно.
                if result == "429":

                    rate_limit_until[self.cache_key] = (
                        time.time() + RATE_LIMIT_COOLDOWN
                    )

                    # Пробуем второй endpoint один раз.
                    result = await self.request_open_meteo(
                        session,
                        "api.open-meteo.com",
                    )

                    if result == "429":

                        logging.warning(
                            "Второй запрос также получил 429."
                        )

                        result = None

                # =================================================
                # 6. Если API не ответил — используем старый кэш
                # =================================================

                if not result:

                    if cached_json:

                        try:

                            import json

                            data = json.loads(cached_json)

                            weather_cache[self.cache_key] = (
                                data,
                                cached_timestamp,
                            )

                            logging.warning(
                                "Использую сохранённую погоду "
                                "из SQLite."
                            )

                            return data

                        except Exception as e:

                            logging.error(
                                "Не удалось загрузить старую "
                                "погоду: %s",
                                e,
                            )

                    logging.error(
                        "Нет ни свежей, ни сохранённой погоды."
                    )

                    return None

                # =================================================
                # 7. Сохраняем результат
                # =================================================

                try:

                    import json

                    json_data = json.dumps(
                        result,
                        ensure_ascii=False,
                    )

                    save_weather_cache(
                        self.cache_key,
                        self.lat,
                        self.lon,
                        json_data,
                    )

                    weather_cache[self.cache_key] = (
                        result,
                        time.time(),
                    )

                    logging.info(
                        "Погода сохранена в RAM + SQLite."
                    )

                except Exception as e:

                    logging.error(
                        "Ошибка сохранения weather cache: %s",
                        e,
                    )

                return result

    # ========================================================
    # ОЦЕНКА ДАВЛЕНИЯ
    # ========================================================

    def _pressure_score(
        self,
        pressure_mm: float,
        is_predator: bool,
    ) -> int:

        optimum = 748 if is_predator else 752

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
    # ТРЕНД ДАВЛЕНИЯ
    # ========================================================

    def _pressure_trend_score(
        self,
        pressures: list,
        idx: int,
    ) -> Tuple[str, int]:

        if idx < 24:
            return "Недостатньо даних", 0

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
            return "Недостатньо даних", 0

        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older)

        delta = (
            avg_recent - avg_older
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
    # СТАБИЛЬНОСТЬ ДАВЛЕНИЯ
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

        diff = max(valid) - min(valid)

        # surface_pressure в hPa.
        # Переводим примерно в мм рт. ст.
        diff_mm = diff * 0.75006

        if diff_mm <= 4:
            return (
                "Дуже стабільний ✅",
                12,
            )

        elif diff_mm <= 7:
            return (
                "Стабільний",
                6,
            )

        elif diff_mm <= 11:
            return (
                "Помірно мінливий ⚠️",
                -4,
            )

        return (
            "Стрибкоподібний ❌",
            -16,
        )

    # ========================================================
    # ТЕМПЕРАТУРА
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
    # ВЕТЕР
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
    # ОСАДКИ
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
    # ОБЛАЧНОСТЬ
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
    # ЗВЕЗДЫ
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
    # ЭКСПЕРТНЫЙ КОММЕНТАРИЙ
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
            f"⏱ <b>Час:</b> {sun_title}. {sun_desc}",
            f"🌕 <b>Місяць:</b> {moon_text}",
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
                "   • Спека — шукайте тінь, "
                "глибину, течію."
            )

        elif water_temp < 9:

            comments.append(
                "   • Холодна вода — дрібні "
                "наживки, повільна подача."
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
                f"🌧 <b>Опади:</b> {precip} мм — "
                f"добре для сома, щуки, "
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
    # ОСНОВНОЙ РАСЧЕТ
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

        try:

            hourly = data["hourly"]

            pressures = hourly["surface_pressure"]

            max_idx = len(pressures) - 1

            # Данные включают past_days=2.
            # Начинаем примерно с текущих суток.
            target_index = min(
                48
                + day_offset * 24
                + target_hour,
                max_idx,
            )

            def safe(value, default):

                if value is None:
                    return default

                return value

            pressure_hpa = safe(
                pressures[target_index],
                1013.25,
            )

            pressure_mm = (
                pressure_hpa * 0.75006
            )

            wind_ms = safe(
                hourly["wind_speed_10m"][
                    target_index
                ],
                2.5,
            )

            temp = safe(
                hourly["temperature_2m"][
                    target_index
                ],
                18.0,
            )

            precip = safe(
                hourly["precipitation"][
                    target_index
                ],
                0.0,
            )

            wind_dir = get_wind_direction_text(
                hourly[
                    "wind_direction_10m"
                ][target_index]
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

            water_list = hourly.get(
                "sea_surface_temperature"
            )

            if not water_list:

                water_list = [
                    None
                ] * len(pressures)

            water_temp = safe(
                water_list[target_index],
                round(
                    temp * 0.82 + 3.2,
                    1,
                ),
            )

            is_predator = fish_type in [
                "Щука",
                "Окунь",
                "Сом",
            ]

            score = 48

            # Давление
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
                + timedelta(days=day_offset)
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

            stars = self.calculate_star_score(
                final_score
            )

            date_str = target_date.strftime(
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
                "sources_used": (
                    "Open-Meteo"
                ),
                "score_100": final_score,
            }

        except Exception as e:

            logging.exception(
                "Ошибка расчета прогноза: %s",
                e,
            )

            return None


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = Bot(
    token=API_TOKEN
)

storage = MemoryStorage()

dp = Dispatcher(
    storage=storage
)


# ============================================================
# КЛАВИАТУРЫ
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
        "Привіт! 🎣\n"
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
        "<b>Як працює оцінка кльову:</b>\n\n"
        "• Стабільність тиску за 48 год\n"
        "• Тренд тиску\n"
        "• Абсолютний тиск\n"
        "• Температура повітря і води\n"
        "• Вітер\n"
        "• Опади\n"
        "• Хмарність\n"
        "• Ранкова та вечірня активність\n"
        "• Фаза місяця\n\n"
        "Джерело погоди: Open-Meteo"
    )

    await message.answer(
        text,
        parse_mode="HTML",
    )


# ============================================================
# ИСТОРИЯ
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
        "<b>📜 Ваші останні прогнози:</b>\n\n"
    )

    for row in rows:

        (
            region,
            fish,
            day,
            hour,
            stars,
            ts,
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
            f"📍 {region} | 🎣 {fish}\n"
            f"{day} о {hour_str}\n"
            f"Оцінка: {graphic}\n"
            f"🕒 {ts}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML",
    )


# ============================================================
# ВЫБОР ОБЛАСТИ
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
        f"Область: <b>{message.text}</b>\n"
        f"Оберіть рибу:",
        reply_markup=get_fish_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# ИЗМЕНИТЬ ОБЛАСТЬ
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
# ВЫБОР РЫБЫ
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

        d = today + timedelta(days=i)

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

        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"day_{i}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад (до вибору риби)",
            callback_data="back_to_fish",
        )
    ])

    await message.answer(
        f"Риба: <b>{message.text}</b>\n"
        f"Оберіть день:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
        parse_mode="HTML",
    )


# ============================================================
# НАЗАД К РЫБЕ
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

    await callback.message.answer(
        "Оберіть рибу за допомогою "
        "кнопок нижче 👇",
        reply_markup=get_fish_keyboard(),
    )

    await callback.answer()


# ============================================================
# ВЫБОР ДНЯ
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
            "Помилка вибору дня.",
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
# НАЗАД К ДНЮ
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

        d = today + timedelta(days=i)

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

        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"day_{i}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад (до вибору риби)",
            callback_data="back_to_fish",
        )
    ])

    await callback.message.edit_text(
        f"Риба: <b>{fish_type}</b>\n"
        f"Оберіть день:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
        parse_mode="HTML",
    )

    await callback.answer()


# ============================================================
# ВЫБОР ЧАСА
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
            "Помилка вибору часу.",
            show_alert=True,
        )

        return

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

    if region not in REGIONS:

        await callback.message.answer(
            "❌ Невідома область. "
            "Почніть з /start"
        )

        await state.clear()
        await callback.answer()

        return

    coords = REGIONS[region]

    client = MultiSourceWeatherClient(
        coords["lat"],
        coords["lon"],
    )

    try:

        await callback.message.edit_text(
            "⏳ Аналізую погоду та "
            "розраховую кльов..."
        )

    except Exception:
        pass

    # ========================================================
    # Расчет
    # ========================================================

    try:

        result = await asyncio.wait_for(
            client.evaluate_biting(
                fish_type,
                region,
                hour,
                day_offset,
            ),
            timeout=35,
        )

    except asyncio.TimeoutError:

        logging.error(
            "Weather calculation timeout"
        )

        result = None

    except Exception as e:

        logging.exception(
            "Forecast handler error: %s",
            e,
        )

        result = None

    if not result:

        await callback.message.answer(
            "❌ Не вдалося отримати "
            "актуальні метеодані.\n\n"
            "Спробуйте ще раз через кілька хвилин.\n"
            "Бот продовжує працювати.",
            reply_markup=get_regions_keyboard(),
        )

        await state.clear()
        await callback.answer()

        return

    # ========================================================
    # Сохраняем прогноз
    # ========================================================

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

    # ========================================================
    # Формируем ответ
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

        f"   {result['pressure_trend']} | "
        f"{result['pressure_stability']}\n"

        f"💨 "
        f"{result['wind_ms']} м/с "
        f"({result['wind_dir']}) | "
        f"🌧 {result['precipitation']} мм\n"

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
    # Кнопки
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

    try:

        parts = callback.data.split("_")

        rating = parts[1]

        forecast_id = int(parts[2])

        save_feedback_to_db(
            callback.from_user.id,
            forecast_id,
            rating,
        )

        if rating == "good":

            msg = (
                "Дякуємо! Відгук допоможе "
                "покращити прогнози 👍"
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

        logging.exception(
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
            f"поділився прогнозом!</b>\n"
            f"📍 {region} | 🎣 "
            f"<b>{fish}</b>\n"
            f"⭐ {stars}/5 ({graphic})\n"
            f"💬 Приєднуйтесь!"
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

        logging.exception(
            "Share error: %s",
            e,
        )

        await callback.answer(
            "❌ Помилка відправки. "
            "Перевірте, що бот є учасником чату "
            "та має право писати.",
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
                "Натисніть /start",
                reply_markup=get_regions_keyboard(),
            )

    except Exception as e:

        logging.exception(
            "Fallback error: %s",
            e,
        )


# ============================================================
# HEALTH SERVER
# ============================================================

async def health(
    request
):

    return web.Response(
        text="Fishing bot is running ✅"
    )


async def health_status(
    request
):

    return web.json_response({
        "status": "ok",
        "bot": "Fishing Forecast",
        "time": datetime.utcnow().isoformat(),
    })


# ============================================================
# MAIN
# ============================================================

async def main():

    logging.info(
        "========================================"
    )

    logging.info(
        "STARTING FISHING FORECAST BOT"
    )

    logging.info(
        "========================================"
    )

    # ========================================================
    # DB
    # ========================================================

    init_db()

    # ========================================================
    # HTTP health server для Render
    # ========================================================

    app = web.Application()

    app.router.add_get(
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

    logging.info(
        "Health server started on port %s",
        port,
    )

    # ========================================================
    # Telegram
    # ========================================================

    try:

        me = await bot.get_me()

        logging.info(
            "Telegram bot: @%s | id=%s",
            me.username,
            me.id,
        )

    except Exception as e:

        logging.exception(
            "Cannot connect to Telegram: %s",
            e,
        )

        await runner.cleanup()

        raise

    # ========================================================
    # POLLING
    # ========================================================

    try:

        logging.info(
            "Starting Telegram polling..."
        )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    except TelegramConflictError:

        logging.critical(
            "================================================"
        )

        logging.critical(
            "TELEGRAM CONFLICT"
        )

        logging.critical(
            "Другой процесс уже использует "
            "этот BOT_TOKEN."
        )

        logging.critical(
            "Проверь Render Services/Workers "
            "и локально запущенный bot.py."
        )

        logging.critical(
            "================================================"
        )

        raise

    except asyncio.CancelledError:

        logging.info(
            "Polling cancelled"
        )

        raise

    except Exception as e:

        logging.exception(
            "Telegram polling crashed: %s",
            e,
        )

        raise

    finally:

        logging.info(
            "Closing bot..."
        )

        try:
            await bot.session.close()
        except Exception:
            pass

        try:
            await runner.cleanup()
        except Exception:
            pass

        logging.info(
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

        logging.info(
            "Bot stopped manually."
        )

    except Exception as e:

        logging.exception(
            "FATAL ERROR: %s",
            e,
        )
