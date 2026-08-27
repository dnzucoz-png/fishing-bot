import asyncio
import html
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
        "ОШИБКА: переменная BOT_TOKEN не установлена в Render"
    )

GROUP_CHAT_ID = int(
    os.getenv("GROUP_CHAT_ID", "-1004434293069")
)

GROUP_URL = os.getenv(
    "GROUP_URL",
    "https://t.me/+rKxYkNg85aAwNzFi"
)

REGIONS = {
    "Дніпропетровська": {
        "lat": 48.4647,
        "lon": 35.0462
    },
    "Київська": {
        "lat": 50.4501,
        "lon": 30.5234
    },
    "Полтавська": {
        "lat": 49.5895,
        "lon": 34.5514
    },
    "Запорізька": {
        "lat": 47.8388,
        "lon": 35.1396
    },
    "Черкаська": {
        "lat": 49.4444,
        "lon": 32.0598
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

CACHE_TTL = 45 * 60

POLLING_RESTART_DELAY = 10
WATCHDOG_INTERVAL = 60

DB_FILE = "fishing_forecast.db"


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger("FishingBot")


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

def db_connect():

    conn = sqlite3.connect(
        DB_FILE,
        timeout=10
    )

    conn.execute(
        "PRAGMA busy_timeout = 10000"
    )

    return conn


def init_db():

    conn = db_connect()

    try:

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
                timestamp DATETIME
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                forecast_id INTEGER,
                rating TEXT,
                timestamp DATETIME
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()

    finally:

        conn.close()

    logger.info("SQLite initialized")


def save_forecast_to_db(
    user_id,
    region,
    fish_type,
    forecast_day,
    hour,
    pressure,
    wind,
    temp,
    stars
):

    conn = db_connect()

    try:

        cursor = conn.cursor()

        cursor.execute("""
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

        return forecast_id

    finally:

        conn.close()


def save_feedback_to_db(
    user_id,
    forecast_id,
    rating
):

    conn = db_connect()

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
            )
        )

        conn.commit()

    finally:

        conn.close()


def get_user_history_from_db(user_id):

    conn = db_connect()

    try:

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

        return cursor.fetchall()

    finally:

        conn.close()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def get_wind_direction_text(
    degrees
):

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
    date_obj: datetime
) -> Tuple[str, int]:

    known_new_moon = datetime(
        2024,
        1,
        11
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
    hour: int
):

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
# ПОГОДА
# ============================================================

weather_cache = {}


class WeatherClient:

    def __init__(
        self,
        lat,
        lon
    ):

        self.lat = lat
        self.lon = lon

        self.cache_key = (
            f"{lat}_{lon}"
        )


    async def fetch(
        self,
        session,
        model=None
    ):

        model_param = ""

        if model:
            model_param = (
                f"&models={model}"
            )

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
            f"{model_param}"
            "&timezone=auto"
            "&past_days=2"
            "&forecast_days=3"
        )

        for attempt in range(3):

            try:

                timeout = aiohttp.ClientTimeout(
                    total=15,
                    connect=5
                )

                async with session.get(
                    url,
                    timeout=timeout
                ) as response:

                    if response.status == 200:

                        return await response.json()

                    if response.status == 429:

                        delay = (
                            15 +
                            attempt * 15
                        )

                        logger.warning(
                            "Open-Meteo 429. "
                            "Waiting %s sec.",
                            delay
                        )

                        await asyncio.sleep(
                            delay
                        )

                        continue

                    logger.warning(
                        "Open-Meteo HTTP %s",
                        response.status
                    )

            except asyncio.CancelledError:

                raise

            except Exception:

                logger.exception(
                    "Weather request error"
                )

            await asyncio.sleep(
                2 + attempt
            )

        return None


    async def get_weather(self):

        now = datetime.now().timestamp()

        cached = weather_cache.get(
            self.cache_key
        )

        if cached:

            data, timestamp = cached

            if (
                now - timestamp
                < CACHE_TTL
            ):

                logger.info(
                    "Weather cache hit"
                )

                return data

        timeout = aiohttp.ClientTimeout(
            total=20
        )

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            data = await self.fetch(
                session
            )

            if not data:

                logger.warning(
                    "Trying ECMWF..."
                )

                data = await self.fetch(
                    session,
                    "ecmwf_ifs04"
                )

            if not data:

                logger.error(
                    "Weather unavailable"
                )

                return None

            weather_cache[
                self.cache_key
            ] = (
                data,
                now
            )

            return data


    def pressure_score(
        self,
        pressure,
        predator
    ):

        optimum = (
            748
            if predator
            else 752
        )

        diff = abs(
            pressure - optimum
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
        index
    ):

        if index < 24:

            return (
                "Недостатньо даних",
                0
            )

        recent = [
            x
            for x in pressures[
                index - 12:index + 1
            ]
            if x is not None
        ]

        older = [
            x
            for x in pressures[
                index - 24:index - 12
            ]
            if x is not None
        ]

        if (
            len(recent) < 5
            or len(older) < 5
        ):

            return (
                "Недостатньо даних",
                0
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
                12
            )

        if delta < -0.8:

            return (
                "Повільно падає 📉",
                8
            )

        if delta > 2.5:

            return (
                "Сильно росте 📈",
                -6
            )

        if delta > 0.8:

            return (
                "Повільно росте 📈",
                2
            )

        return (
            "Стабільний ✅",
            10
        )


    def stability_score(
        self,
        pressures,
        index
    ):

        if index < 48:

            return (
                "Недостатньо історії",
                0
            )

        values = [
            x
            for x in pressures[
                index - 48:index + 1
            ]
            if x is not None
        ]

        if len(values) < 20:

            return (
                "Недостатньо даних",
                0
            )

        diff = (
            max(values)
            - min(values)
        )

        if diff <= 4:

            return (
                "Дуже стабільний ✅",
                12
            )

        if diff <= 7:

            return (
                "Стабільний",
                6
            )

        if diff <= 11:

            return (
                "Помірно мінливий ⚠️",
                -4
            )

        return (
            "Стрибкоподібний ❌",
            -16
        )


    def temperature_score(
        self,
        water_temp,
        predator
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


    def wind_score(
        self,
        wind,
        direction,
        predator
    ):

        if wind < 1.5:

            score = (
                -4
                if predator
                else 2
            )

        elif 2 <= wind <= 5.5:

            score = 10

        elif wind <= 7.5:

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
        precipitation,
        predator
    ):

        if precipitation <= 0.1:
            return 0

        if precipitation <= 1.8:

            return (
                7
                if predator
                else 4
            )

        if precipitation <= 3.5:
            return -6

        return -16


    def cloud_score(
        self,
        cloud,
        predator
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


    def stars(
        self,
        score
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


    def commentary(
        self,
        fish,
        pressure,
        trend,
        stability,
        wind,
        wind_dir,
        precipitation,
        sun_title,
        sun_desc,
        temp,
        water,
        score,
        cloud,
        moon
    ):

        result = [

            f"⏱ <b>Час:</b> "
            f"{sun_title}. {sun_desc}",

            f"🌕 <b>Місяць:</b> "
            f"{moon}",

            f"🌀 <b>Тиск:</b> "
            f"{pressure} мм | "
            f"{trend} | "
            f"{stability}",

            f"🌡 <b>Температура:</b> "
            f"повітря {temp}°C, "
            f"вода ~{water}°C",
        ]

        if water > 25:

            result.append(
                "• Спека — шукайте "
                "тінь, глибину, течію."
            )

        elif water < 9:

            result.append(
                "• Холодна вода — "
                "дрібні наживки, "
                "повільна подача."
            )

        if wind < 2:

            result.append(
                f"💨 <b>Вітер:</b> "
                f"штиль "
                f"({wind} м/с, "
                f"{wind_dir})."
            )

        elif wind <= 6:

            result.append(
                f"💨 <b>Вітер:</b> "
                f"сприятливий "
                f"({wind} м/с, "
                f"{wind_dir})."
            )

        else:

            result.append(
                f"💨 <b>Вітер:</b> "
                f"сильний "
                f"({wind} м/с, "
                f"{wind_dir})."
            )

        if precipitation > 1.5:

            result.append(
                f"🌧 <b>Опади:</b> "
                f"{precipitation} мм."
            )

        elif cloud > 65:

            result.append(
                f"☁️ <b>Хмарність:</b> "
                f"{cloud}%."
            )

        predator = fish in [
            "Щука",
            "Окунь",
            "Сом"
        ]

        if predator:

            result.append(
                f"🎯 <b>Для {fish}:</b> "
                "активні проводки "
                "на брівках і перепадах."
            )

        else:

            result.append(
                f"🎯 <b>Для {fish}:</b> "
                "мотиль/опариш/"
                "кукурудза."
            )

        if score >= 78:

            result.append(
                "\n🏆 <b>Підсумок:</b> "
                "Відмінні умови!"
            )

        elif score >= 55:

            result.append(
                "\n⚖️ <b>Підсумок:</b> "
                "Добрі умови."
            )

        else:

            result.append(
                "\n⚠️ <b>Підсумок:</b> "
                "Складні умови."
            )

        return "\n".join(result)


    async def evaluate(
        self,
        fish,
        region,
        hour,
        day_offset
    ):

        data = await self.get_weather()

        if not data:
            return None

        hourly = data.get(
            "hourly",
            {}
        )

        pressures = hourly.get(
            "surface_pressure",
            []
        )

        if not pressures:

            return None

        max_index = (
            len(pressures) - 1
        )

        index = min(
            48
            + day_offset * 24
            + hour,
            max_index
        )

        def get_value(
            name,
            default
        ):

            values = hourly.get(
                name,
                []
            )

            if index >= len(values):

                return default

            value = values[index]

            if value is None:

                return default

            return value

        pressure_hpa = get_value(
            "surface_pressure",
            1013.25
        )

        pressure = (
            pressure_hpa
            * 0.75006
        )

        wind = get_value(
            "wind_speed_10m",
            2.5
        )

        temp = get_value(
            "temperature_2m",
            18
        )

        precipitation = get_value(
            "precipitation",
            0
        )

        wind_degrees = get_value(
            "wind_direction_10m",
            None
        )

        wind_dir = (
            get_wind_direction_text(
                wind_degrees
            )
        )

        humidity = get_value(
            "relative_humidity_2m",
            55
        )

        cloud = get_value(
            "cloud_cover",
            40
        )

        # Расчётная температура воды.
        # Open-Meteo не даёт температуру
        # конкретного украинского водоёма.
        water = round(
            temp * 0.82 + 3.2,
            1
        )

        predator = fish in [
            "Щука",
            "Окунь",
            "Сом"
        ]

        score = 48

        stability, points = (
            self.stability_score(
                pressures,
                index
            )
        )

        score += points

        trend, points = (
            self.pressure_trend(
                pressures,
                index
            )
        )

        score += points

        score += self.pressure_score(
            pressure,
            predator
        )

        score += self.temperature_score(
            water,
            predator
        )

        score += self.wind_score(
            wind,
            wind_dir,
            predator
        )

        score += self.precipitation_score(
            precipitation,
            predator
        )

        score += self.cloud_score(
            cloud,
            predator
        )

        sun_title, sun_desc, sun_points = (
            check_sun_activity(hour)
        )

        score += sun_points

        target_date = (
            datetime.now()
            + timedelta(days=day_offset)
        )

        moon, moon_points = (
            get_moon_phase_info(
                target_date
            )
        )

        score += (
            moon_points
            if predator
            else int(
                moon_points * 0.5
            )
        )

        score = max(
            0,
            min(100, score)
        )

        stars = self.stars(score)

        date_str = (
            target_date.strftime(
                "%d.%m.%Y"
            )
        )

        if day_offset == 0:

            day_text = (
                f"Сьогодні "
                f"({date_str})"
            )

        elif day_offset == 1:

            day_text = (
                f"Завтра "
                f"({date_str})"
            )

        else:

            day_text = (
                f"Післязавтра "
                f"({date_str})"
            )

        expert = self.commentary(
            fish,
            round(pressure, 1),
            trend,
            stability,
            round(wind, 1),
            wind_dir,
            round(precipitation, 1),
            sun_title,
            sun_desc,
            round(temp, 1),
            water,
            score,
            round(cloud),
            moon
        )

        return {

            "fish": fish,

            "forecast_day": day_text,

            "hour": hour,

            "pressure_mm": round(
                pressure,
                1
            ),

            "pressure_stability":
                stability,

            "pressure_trend":
                trend,

            "wind_ms": round(
                wind,
                1
            ),

            "wind_dir": wind_dir,

            "humidity": round(
                humidity
            ),

            "cloud_cover": round(
                cloud
            ),

            "precipitation": round(
                precipitation,
                1
            ),

            "temperature": round(
                temp,
                1
            ),

            "water_temp": water,

            "moon_phase": moon,

            "stars": stars,

            "stars_graphic": (
                "⭐" * stars
                + "☆" * (5 - stars)
            ),

            "expert_commentary":
                expert,

            "sources_used":
                "Open-Meteo (GFS)",

            "score_100":
                score,
        }


# ============================================================
# TELEGRAM
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

def regions_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[

            [
                KeyboardButton(
                    text="Дніпропетровська"
                ),
                KeyboardButton(
                    text="Київська"
                )
            ],

            [
                KeyboardButton(
                    text="Полтавська"
                ),
                KeyboardButton(
                    text="Запорізька"
                )
            ],

            [
                KeyboardButton(
                    text="Черкаська"
                )
            ],

            [
                KeyboardButton(
                    text="📜 Моя історія"
                ),
                KeyboardButton(
                    text="ℹ️ Допомога"
                )
            ],

        ],
        resize_keyboard=True
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
                )
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
                )
            ],

            [
                KeyboardButton(
                    text="Плотва"
                ),
                KeyboardButton(
                    text="◀️ Змінити область"
                )
            ]

        ],
        resize_keyboard=True
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
                f"({date:%d.%m})"
            )

        elif i == 1:

            label = (
                f"Завтра "
                f"({date:%d.%m})"
            )

        else:

            label = (
                f"Післязавтра "
                f"({date:%d.%m})"
            )

        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"day_{i}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="◀️ Назад",
            callback_data="back_to_fish"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


# ============================================================
# HANDLERS
# ============================================================

@dp.message(Command("start"))
async def start(
    message: Message,
    state: FSMContext
):

    await state.clear()

    await state.set_state(
        ForecastStates.choosing_region
    )

    await message.answer(
        "Привіт! 🎣\n"
        "Оберіть область:",
        reply_markup=regions_keyboard()
    )


@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Допомога")
async def help_handler(
    message: Message
):

    await message.answer(
        "<b>Як працює прогноз:</b>\n\n"
        "• Тиск\n"
        "• Тренд тиску\n"
        "• Стабільність тиску\n"
        "• Температура\n"
        "• Вітер\n"
        "• Опади\n"
        "• Хмарність\n"
        "• Час доби\n"
        "• Місяць\n\n"
        "Джерело: Open-Meteo",
        parse_mode="HTML"
    )


@dp.message(F.text == "📜 Моя історія")
async def history(
    message: Message
):

    rows = await asyncio.to_thread(
        get_user_history_from_db,
        message.from_user.id
    )

    if not rows:

        await message.answer(
            "Історія поки порожня."
        )

        return

    text = (
        "<b>📜 Останні прогнози:</b>\n\n"
    )

    for row in rows:

        region, fish, day, hour, stars, timestamp = row

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
            f"📍 {html.escape(region)}\n"
            f"🎣 {html.escape(fish)}\n"
            f"{html.escape(day)} "
            f"о {hour_text}\n"
            f"{graphic}\n"
            f"🕒 {timestamp}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML"
    )


@dp.message(
    F.text.in_(REGIONS.keys())
)
async def region_handler(
    message: Message,
    state: FSMContext
):

    await state.update_data(
        region=message.text
    )

    await state.set_state(
        ForecastStates.choosing_fish
    )

    await message.answer(
        f"Область: "
        f"<b>{html.escape(message.text)}</b>\n"
        "Оберіть рибу:",
        reply_markup=fish_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    F.text == "◀️ Змінити область"
)
async def change_region(
    message: Message,
    state: FSMContext
):

    await start(
        message,
        state
    )


@dp.message(
    F.text.in_(FISH_LIST)
)
async def fish_handler(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    if "region" not in data:

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
        f"<b>{html.escape(message.text)}</b>\n"
        "Оберіть день:",
        reply_markup=day_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(
    F.data == "back_to_fish"
)
async def back_to_fish(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        ForecastStates.choosing_fish
    )

    await callback.message.edit_text(
        "Оберіть рибу:"
    )

    await callback.answer()


@dp.callback_query(
    F.data.startswith("day_")
)
async def day_handler(
    callback: CallbackQuery,
    state: FSMContext
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
                    text="🌅 Світанок 06:00",
                    callback_data="hour_6"
                )
            ],

            [
                InlineKeyboardButton(
                    text="☀️ День 12:00",
                    callback_data="hour_12"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🌇 Захід 20:00",
                    callback_data="hour_20"
                )
            ],

            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="back_to_day"
                )
            ],

        ]
    )

    await callback.message.edit_text(
        "Оберіть час:",
        reply_markup=keyboard
    )

    await callback.answer()


@dp.callback_query(
    F.data == "back_to_day"
)
async def back_to_day(
    callback: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        ForecastStates.choosing_day
    )

    await callback.message.edit_text(
        "Оберіть день:",
        reply_markup=day_keyboard()
    )

    await callback.answer()


@dp.callback_query(
    F.data.startswith("hour_")
)
async def hour_handler(
    callback: CallbackQuery,
    state: FSMContext
):

    try:

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

        day_offset = int(
            data.get(
                "day_offset",
                0
            )
        )

        if not region:

            await callback.answer(
                "Оберіть область.",
                show_alert=True
            )

            return

        if not fish:

            await callback.answer(
                "Оберіть рибу.",
                show_alert=True
            )

            return

        coords = REGIONS[
            region
        ]

        await callback.message.edit_text(
            "⏳ Отримую погоду...\n"
            "🌀 Аналізую тиск...\n"
            "💨 Аналізую вітер...\n"
            "🎣 Розраховую кльов..."
        )

        client = WeatherClient(
            coords["lat"],
            coords["lon"]
        )

        result = await asyncio.wait_for(
            client.evaluate(
                fish,
                region,
                hour,
                day_offset
            ),
            timeout=55
        )

        if not result:

            await callback.message.answer(
                "❌ Не вдалося отримати погоду.\n"
                "Спробуйте ще раз через кілька хвилин.",
                reply_markup=regions_keyboard()
            )

            await state.clear()

            await callback.answer()

            return

        forecast_id = await asyncio.to_thread(
            save_forecast_to_db,
            callback.from_user.id,
            region,
            fish,
            result["forecast_day"],
            result["hour"],
            result["pressure_mm"],
            result["wind_ms"],
            result["temperature"],
            result["stars"]
        )

        response = (

            f"📍 <b>{html.escape(region)}</b>\n"
            f"{result['forecast_day']} "
            f"о {result['hour']:02d}:00\n\n"

            f"🎣 <b>{html.escape(fish)}</b>\n\n"

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
            f"{result['cloud_cover']}%\n\n"

            f"⭐ <b>Кльов: "
            f"{result['stars']}/5</b>\n"

            f"{result['stars_graphic']}\n"

            f"<i>Бал: "
            f"{result['score_100']}/100</i>\n\n"

            f"💡 <b>Аналіз:</b>\n"
            f"{result['expert_commentary']}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[

                [
                    InlineKeyboardButton(
                        text="📢 Поділитися",
                        callback_data=(
                            f"share_"
                            f"{result['stars']}_"
                            f"{fish}_"
                            f"{region}"
                        )
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="💬 Перейти в чат",
                        url=GROUP_URL
                    )
                ],

                [

                    InlineKeyboardButton(
                        text="👍 Точний",
                        callback_data=(
                            f"fb_good_"
                            f"{forecast_id}"
                        )
                    ),

                    InlineKeyboardButton(
                        text="👎 Хибний",
                        callback_data=(
                            f"fb_bad_"
                            f"{forecast_id}"
                        )
                    )

                ]

            ]
        )

        await callback.message.answer(
            response,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

        await state.clear()

        await callback.answer()

    except asyncio.TimeoutError:

        logger.warning(
            "Forecast timeout"
        )

        await callback.message.answer(
            "⏱ Сервер погоди відповідає "
            "занадто довго.\n"
            "Спробуйте ще раз."
        )

        await state.clear()

        await callback.answer()

    except Exception:

        logger.exception(
            "Forecast handler error"
        )

        try:

            await callback.message.answer(
                "❌ Помилка при розрахунку.\n"
                "Спробуйте ще раз."
            )

        except Exception:

            logger.exception(
                "Cannot send error"
            )

        await state.clear()

        try:

            await callback.answer()

        except Exception:

            pass


@dp.callback_query(
    F.data.startswith("fb_")
)
async def feedback(
    callback: CallbackQuery
):

    try:

        parts = callback.data.split("_")

        rating = parts[1]

        forecast_id = int(
            parts[2]
        )

        await asyncio.to_thread(
            save_feedback_to_db,
            callback.from_user.id,
            forecast_id,
            rating
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
            show_alert=True
        )

    except Exception:

        logger.exception(
            "Feedback error"
        )

        await callback.answer(
            "❌ Помилка",
            show_alert=True
        )


@dp.callback_query(
    F.data.startswith("share_")
)
async def share(
    callback: CallbackQuery
):

    try:

        _, stars, fish, region = (
            callback.data.split(
                "_",
                3
            )
        )

        stars = int(stars)

        graphic = (
            "⭐" * stars
            + "☆" * (5 - stars)
        )

        first_name = (
            callback.from_user.first_name
            or "Користувач"
        )

        text = (

            f"📢 <b>"
            f"{html.escape(first_name)} "
            f"поділився прогнозом!</b>\n"

            f"📍 "
            f"{html.escape(region)}\n"

            f"🎣 "
            f"<b>{html.escape(fish)}</b>\n"

            f"⭐ {stars}/5 "
            f"({graphic})\n\n"

            "🎣 Fishing Forecast"
        )

        await bot.send_message(
            GROUP_CHAT_ID,
            text,
            parse_mode="HTML"
        )

        await callback.answer(
            "✅ Надіслано!",
            show_alert=True
        )

    except Exception:

        logger.exception(
            "Share error"
        )

        await callback.answer(
            "❌ Не вдалося надіслати.",
            show_alert=True
        )


@dp.message()
async def fallback(
    message: Message,
    state: FSMContext
):

    current_state = (
        await state.get_state()
    )

    if current_state is None:

        await message.answer(
            "Натисніть /start",
            reply_markup=regions_keyboard()
        )


# ============================================================
# HEALTH SERVER
# ============================================================

async def health(
    request
):

    return web.json_response({

        "status": "ok",

        "bot": "fishing-bot",

        "time": datetime.utcnow().isoformat(),

    })


async def start_health_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    logger.info(
        "HEALTH SERVER: listening on port %s",
        port
    )

    return runner


# ============================================================
# WATCHDOG TELEGRAM
# ============================================================

async def telegram_watchdog():

    while True:

        try:

            me = await bot.get_me()

            logger.info(
                "WATCHDOG: Telegram OK | "
                "@%s | id=%s",
                me.username,
                me.id
            )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "WATCHDOG: Telegram ERROR"
            )

        await asyncio.sleep(
            WATCHDOG_INTERVAL
        )


# ============================================================
# POLLING
# ============================================================

async def polling_loop():

    while True:

        try:

            logger.info(
                "POLLING: starting..."
            )

            await dp.start_polling(
                bot,
                handle_signals=False
            )

            logger.warning(
                "POLLING: stopped. "
                "Restarting in %s sec.",
                POLLING_RESTART_DELAY
            )

        except asyncio.CancelledError:

            logger.info(
                "POLLING: cancelled"
            )

            raise

        except Exception:

            logger.exception(
                "POLLING: crashed!"
            )

        await asyncio.sleep(
            POLLING_RESTART_DELAY
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "================================"
    )

    logger.info(
        "FISHING BOT STARTING"
    )

    logger.info(
        "Python process PID=%s",
        os.getpid()
    )

    logger.info(
        "================================"
    )

    await asyncio.to_thread(
        init_db
    )

    health_runner = (
        await start_health_server()
    )

    watchdog_task = asyncio.create_task(
        telegram_watchdog()
    )

    polling_task = asyncio.create_task(
        polling_loop()
    )

    try:

        await asyncio.gather(
            polling_task,
            watchdog_task
        )

    except asyncio.CancelledError:

        logger.info(
            "MAIN: cancelled"
        )

        raise

    finally:

        logger.info(
            "MAIN: shutting down..."
        )

        watchdog_task.cancel()
        polling_task.cancel()

        await asyncio.gather(
            watchdog_task,
            polling_task,
            return_exceptions=True
        )

        await health_runner.cleanup()

        await bot.session.close()

        logger.info(
            "MAIN: shutdown complete"
        )


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Stopped by keyboard"
        )
