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
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Добавь BOT_TOKEN в Environment Variables на Render."
    )

GROUP_CHAT_ID = -1004434293069
GROUP_URL = "https://t.me/+rKxYkNg85aAwNzFi"

PORT = int(os.getenv("PORT", "10000"))

# Кеш погоды.
# 45 минут — нормально для прогноза рыбалки.
CACHE_TTL = 45 * 60

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
# ЛОГИ
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
# КЕШ ПОГОДЫ
# ============================================================

weather_cache = {}


def get_cache_key(lat: float, lon: float) -> str:
    return f"{lat:.4f}:{lon:.4f}"


def get_cached_weather(lat: float, lon: float):
    key = get_cache_key(lat, lon)

    item = weather_cache.get(key)

    if not item:
        return None

    data, timestamp = item

    if time.time() - timestamp < CACHE_TTL:
        logger.info(
            "Погода взята из кеша: %s",
            key,
        )
        return data

    weather_cache.pop(key, None)

    return None


def put_cached_weather(lat: float, lon: float, data):
    key = get_cache_key(lat, lon)

    weather_cache[key] = (
        data,
        time.time(),
    )


# ============================================================
# БАЗА
# ============================================================

DB_FILE = "fishing_forecast.db"


def get_db():
    return sqlite3.connect(
        DB_FILE,
        timeout=20,
    )


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
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================================

def safe(value, default):
    return default if value is None else value


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


def get_moon_phase_info(
    date_obj: datetime,
) -> Tuple[str, int]:

    known_new_moon = datetime(2024, 1, 11)

    phase_days = (
        date_obj - known_new_moon
    ).days % 29.53

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
# ПОГОДА OPEN-METEO
# ============================================================

class WeatherClient:

    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(
        self,
        lat: float,
        lon: float,
    ):
        self.lat = lat
        self.lon = lon

    async def request_weather(
        self,
        session: aiohttp.ClientSession,
    ):

        params = {
            "latitude": self.lat,
            "longitude": self.lon,

            "hourly": (
                "temperature_2m,"
                "apparent_temperature,"
                "relative_humidity_2m,"
                "surface_pressure,"
                "wind_speed_10m,"
                "wind_direction_10m,"
                "cloud_cover,"
                "precipitation"
            ),

            "timezone": "auto",

            # Нам нужны прошлые данные для анализа давления.
            "past_days": 2,

            # И следующие 3 дня.
            "forecast_days": 3,
        }

        try:

            timeout = aiohttp.ClientTimeout(
                total=15,
                connect=5,
            )

            async with session.get(
                self.BASE_URL,
                params=params,
                timeout=timeout,
                headers={
                    "User-Agent": "FishingForecastBot/1.0"
                },
            ) as response:

                if response.status == 200:

                    data = await response.json()

                    logger.info(
                        "Open-Meteo OK: %.4f %.4f",
                        self.lat,
                        self.lon,
                    )

                    return data

                if response.status == 429:

                    logger.warning(
                        "Open-Meteo 429: превышен лимит запросов"
                    )

                    # ВАЖНО:
                    # НЕ ждём 25/40/55 секунд.
                    # НЕ долбим API повторно.
                    return None

                text = await response.text()

                logger.error(
                    "Open-Meteo HTTP %s: %s",
                    response.status,
                    text[:300],
                )

                return None

        except asyncio.TimeoutError:

            logger.error(
                "Open-Meteo timeout"
            )

            return None

        except aiohttp.ClientError as e:

            logger.error(
                "Open-Meteo connection error: %s",
                e,
            )

            return None

        except Exception:

            logger.exception(
                "Unexpected weather error"
            )

            return None

    async def get_weather(self):

        # ----------------------------------------------------
        # 1. Сначала кеш
        # ----------------------------------------------------

        cached = get_cached_weather(
            self.lat,
            self.lon,
        )

        if cached is not None:
            return cached

        # ----------------------------------------------------
        # 2. Один запрос
        # ----------------------------------------------------

        async with aiohttp.ClientSession() as session:

            data = await self.request_weather(
                session
            )

        # ----------------------------------------------------
        # 3. Если получили данные — сохраняем
        # ----------------------------------------------------

        if data:

            put_cached_weather(
                self.lat,
                self.lon,
                data,
            )

            return data

        # ----------------------------------------------------
        # 4. Если API временно не отвечает,
        #    попробуем вернуть старый кеш,
        #    даже если TTL уже истёк.
        # ----------------------------------------------------

        key = get_cache_key(
            self.lat,
            self.lon,
        )

        old = weather_cache.get(key)

        if old:

            logger.warning(
                "Использую устаревший кеш погоды"
            )

            return old[0]

        return None

    # ========================================================
    # РАСЧЁТ
    # ========================================================

    def pressure_score(
        self,
        pressure_mm,
        predator,
    ):

        optimum = 748 if predator else 752

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

    def pressure_trend(
        self,
        pressures,
        index,
    ):

        if index < 24:
            return (
                "Недостатньо даних",
                0,
            )

        recent = [
            x
            for x in pressures[index - 12:index + 1]
            if x is not None
        ]

        older = [
            x
            for x in pressures[index - 24:index - 12]
            if x is not None
        ]

        if (
            len(recent) < 5
            or len(older) < 5
        ):
            return (
                "Недостатньо даних",
                0,
            )

        recent_avg = sum(recent) / len(recent)
        older_avg = sum(older) / len(older)

        delta = (
            recent_avg - older_avg
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

    def stability_score(
        self,
        pressures,
        index,
    ):

        if index < 48:
            return (
                "Недостатньо історії",
                0,
            )

        values = [
            x
            for x in pressures[index - 48:index + 1]
            if x is not None
        ]

        if len(values) < 20:
            return (
                "Недостатньо даних",
                0,
            )

        diff = max(values) - min(values)

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

    def wind_score(
        self,
        wind,
        direction,
        predator,
    ):

        if wind < 1.5:

            score = (
                -4 if predator else 2
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

    def precipitation_score(
        self,
        precip,
        predator,
    ):

        if precip <= 0.1:
            return 0

        if 0.2 <= precip <= 1.8:
            return (
                7 if predator else 4
            )

        if precip <= 3.5:
            return -6

        return -16

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

        else:

            if cloud >= 80:
                return 2

            if cloud <= 30:
                return 3

            return 0

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

    # ========================================================
    # ПРОГНОЗ
    # ========================================================

    async def evaluate(
        self,
        fish_type,
        target_hour,
        day_offset,
    ):

        data = await self.get_weather()

        if not data:
            return None

        hourly = data.get("hourly")

        if not hourly:
            logger.error(
                "Open-Meteo returned no hourly data"
            )
            return None

        pressures = hourly.get(
            "surface_pressure",
            [],
        )

        if not pressures:
            return None

        max_index = len(pressures) - 1

        # ----------------------------------------------------
        # ВАЖНО
        #
        # past_days=2 => первые 48 часов истории.
        #
        # Поэтому:
        # 48 + day*24 + hour
        # ----------------------------------------------------

        target_index = (
            48
            + day_offset * 24
            + target_hour
        )

        target_index = max(
            0,
            min(
                target_index,
                max_index,
            ),
        )

        pressure_hpa = safe(
            pressures[target_index],
            1013.25,
        )

        pressure_mm = (
            pressure_hpa * 0.75006
        )

        temperatures = hourly.get(
            "temperature_2m",
            [],
        )

        winds = hourly.get(
            "wind_speed_10m",
            [],
        )

        wind_directions = hourly.get(
            "wind_direction_10m",
            [],
        )

        humidity_values = hourly.get(
            "relative_humidity_2m",
            [],
        )

        clouds = hourly.get(
            "cloud_cover",
            [],
        )

        precipitation = hourly.get(
            "precipitation",
            [],
        )

        temp = safe(
            temperatures[target_index]
            if target_index < len(temperatures)
            else None,
            18.0,
        )

        wind = safe(
            winds[target_index]
            if target_index < len(winds)
            else None,
            2.5,
        )

        wind_degree = safe(
            wind_directions[target_index]
            if target_index < len(wind_directions)
            else None,
            0,
        )

        humidity = safe(
            humidity_values[target_index]
            if target_index < len(humidity_values)
            else None,
            55,
        )

        cloud = safe(
            clouds[target_index]
            if target_index < len(clouds)
            else None,
            40,
        )

        precip = safe(
            precipitation[target_index]
            if target_index < len(precipitation)
            else None,
            0,
        )

        wind_dir = get_wind_direction_text(
            wind_degree
        )

        # ----------------------------------------------------
        # Вода
        #
        # Open-Meteo не даёт SST для речной точки.
        # Поэтому используем оценочную температуру.
        # ----------------------------------------------------

        water_temp = round(
            temp * 0.82 + 3.2,
            1,
        )

        predator = fish_type in {
            "Щука",
            "Окунь",
            "Сом",
        }

        score = 48

        stability_text, stability_points = (
            self.stability_score(
                pressures,
                target_index,
            )
        )

        score += stability_points

        trend_text, trend_points = (
            self.pressure_trend(
                pressures,
                target_index,
            )
        )

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
            wind,
            wind_dir,
            predator,
        )

        score += self.precipitation_score(
            precip,
            predator,
        )

        score += self.cloud_score(
            cloud,
            predator,
        )

        sun_title, sun_desc, sun_points = (
            check_sun_activity(
                target_hour
            )
        )

        score += sun_points

        target_date = (
            datetime.now()
            + timedelta(days=day_offset)
        )

        moon_text, moon_points = (
            get_moon_phase_info(
                target_date
            )
        )

        score += (
            moon_points
            if predator
            else int(moon_points * 0.5)
        )

        final_score = max(
            0,
            min(
                100,
                int(score),
            ),
        )

        stars = self.stars(
            final_score
        )

        date_str = target_date.strftime(
            "%d.%m.%Y"
        )

        if day_offset == 0:
            day_text = f"Сьогодні ({date_str})"

        elif day_offset == 1:
            day_text = f"Завтра ({date_str})"

        else:
            day_text = (
                f"Післязавтра ({date_str})"
            )

        # ----------------------------------------------------
        # КОММЕНТАРИЙ
        # ----------------------------------------------------

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
            f"{pressure_mm:.1f} мм | "
            f"{trend_text} | "
            f"{stability_text}"
        )

        comments.append(
            f"🌡 <b>Температура:</b> "
            f"повітря {temp:.1f}°C, "
            f"вода ~{water_temp:.1f}°C"
        )

        if water_temp > 25:

            comments.append(
                "• Спека — шукайте тінь, "
                "глибину або течію."
            )

        elif water_temp < 9:

            comments.append(
                "• Холодна вода — "
                "дрібна наживка "
                "і повільна подача."
            )

        if wind < 2:

            comments.append(
                f"💨 <b>Вітер:</b> "
                f"штиль "
                f"({wind:.1f} м/с, "
                f"{wind_dir})"
            )

        elif wind <= 6:

            comments.append(
                f"💨 <b>Вітер:</b> "
                f"сприятливий "
                f"({wind:.1f} м/с, "
                f"{wind_dir})"
            )

        else:

            comments.append(
                f"💨 <b>Вітер:</b> "
                f"сильний "
                f"({wind:.1f} м/с, "
                f"{wind_dir})"
            )

        if precip > 1.5:

            comments.append(
                f"🌧 <b>Опади:</b> "
                f"{precip:.1f} мм"
            )

        if cloud > 65:

            comments.append(
                f"☁️ <b>Хмарність:</b> "
                f"{cloud:.0f}%"
            )

        if predator:

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

        if final_score >= 78:

            comments.append(
                "🏆 <b>Підсумок:</b> "
                "Відмінні умови! "
                "Вирушайте на водойму."
            )

        elif final_score >= 55:

            comments.append(
                "⚖️ <b>Підсумок:</b> "
                "Добрі умови. "
                "Успіх залежить "
                "від місця і наживки."
            )

        else:

            comments.append(
                "⚠️ <b>Підсумок:</b> "
                "Складні умови. "
                "Потрібні майстерність "
                "і терпіння."
            )

        commentary = "\n".join(
            comments
        )

        return {
            "fish": fish_type,
            "forecast_day": day_text,
            "hour": target_hour,
            "pressure_mm": round(
                pressure_mm,
                1,
            ),
            "pressure_stability": stability_text,
            "pressure_trend": trend_text,
            "wind_ms": round(
                wind,
                1,
            ),
            "wind_dir": wind_dir,
            "humidity": round(
                humidity,
            ),
            "cloud_cover": round(
                cloud,
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

dp = Dispatcher(
    storage=MemoryStorage()
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
        "Привіт! 🎣\n\n"
        "Оберіть область "
        "для прогнозу кльову:",
        reply_markup=get_regions_keyboard(),
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
        "<b>🎣 Як працює прогноз</b>\n\n"
        "• Тиск\n"
        "• Тренд тиску\n"
        "• Стабільність тиску\n"
        "• Температура\n"
        "• Вітер\n"
        "• Опади\n"
        "• Хмарність\n"
        "• Час доби\n"
        "• Фаза місяця\n\n"
        "🌦 Джерело: Open-Meteo",
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

        hour_text = (
            f"{hour:02d}:00"
            if hour is not None
            else "—"
        )

        text += (
            f"📍 {region}\n"
            f"🎣 {fish}\n"
            f"{day} о {hour_text}\n"
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

        date = today + timedelta(
            days=i
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

    await message.answer(
        f"🎣 Риба: "
        f"<b>{message.text}</b>\n\n"
        f"Оберіть день:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
        parse_mode="HTML",
    )


# ============================================================
# BACK FISH
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
        "Оберіть час доби:",
        reply_markup=keyboard,
    )

    await callback.answer()


# ============================================================
# BACK DAY
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
        "Рибу",
    )

    await state.set_state(
        ForecastStates.choosing_day
    )

    today = datetime.now()

    buttons = []

    for i in range(3):

        date = today + timedelta(
            days=i
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

    await callback.message.edit_text(
        f"🎣 Риба: "
        f"<b>{fish}</b>\n\n"
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
            "Область не выбрана",
            show_alert=True,
        )

        return

    if fish not in FISH_LIST:

        await callback.answer(
            "Риба не выбрана",
            show_alert=True,
        )

        return

    await callback.message.edit_text(
        "⏳ <b>Аналізую погоду...</b>\n\n"
        "Це може зайняти кілька секунд.",
        parse_mode="HTML",
    )

    coords = REGIONS[region]

    client = WeatherClient(
        coords["lat"],
        coords["lon"],
    )

    try:

        result = await client.evaluate(
            fish,
            hour,
            day_offset,
        )

    except Exception:

        logger.exception(
            "Forecast calculation error"
        )

        result = None

    if not result:

        await callback.message.edit_text(
            "⚠️ <b>Погодний сервіс тимчасово "
            "не відповідає.</b>\n\n"
            "Open-Meteo зараз повертає "
            "обмеження запитів (429) "
            "або тимчасово недоступний.\n\n"
            "Кешу для цієї області поки немає.\n"
            "Спробуйте повторити прогноз "
            "через кілька хвилин.",
            parse_mode="HTML",
        )

        await state.clear()

        await callback.answer()

        return

    # --------------------------------------------------------
    # Сохраняем прогноз
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Ответ
    # --------------------------------------------------------

    response = (
        f"📍 <b>{region}</b>\n"
        f"{result['forecast_day']} "
        f"о {result['hour']:02d}:00\n\n"

        f"🎣 <b>{fish}</b>\n\n"

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

        text = (
            "Дякуємо! "
            "Відгук допоможе покращити "
            "прогнози 👍"
        )

    else:

        text = (
            "Дякуємо за зворотний "
            "зв’язок 👎"
        )

    await callback.answer(
        text,
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

        text = (
            f"📢 <b>"
            f"{callback.from_user.first_name}"
            f" поділився прогнозом!</b>\n\n"

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

    except Exception as e:

        logger.error(
            "Share error: %s",
            e,
        )

        await callback.answer(
            "❌ Не вдалося відправити "
            "в чат.",
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

    current_state = await state.get_state()

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
        text="Fishing Forecast Bot is running ✅"
    )


async def healthz(
    request: web.Request,
):

    return web.Response(
        text="OK"
    )


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        healthz,
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
        "Fishing Forecast Bot starting..."
    )

    logger.info(
        "Python PID: %s",
        os.getpid(),
    )

    logger.info(
        "PORT: %s",
        PORT,
    )

    logger.info(
        "======================================"
    )

    init_db()

    health_runner = await start_health_server()

    try:

        logger.info(
            "Starting Telegram polling..."
        )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:

        logger.info(
            "Stopping bot..."
        )

        await bot.session.close()

        await health_runner.cleanup()


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
            "FATAL ERROR"
        )

        raise
