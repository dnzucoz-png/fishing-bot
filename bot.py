import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import aiohttp

# --- НАЛАШТУВАННЯ ---
API_TOKEN = "8373587458:AAGYqyAPJyJpeeKevP-LJ76lJ9KB0-AHAvY"  # Замініть на свій реальний токен від BotFather
GROUP_CHAT_ID = -1004434293069  # Замініть на числовий ID вашої групи Telegram
GROUP_URL = "https://t.me/+rKxYkNg85aAwNzFi"  # Посилання на ваш чат

REGIONS = {
    "Дніпропетровська": {"lat": 48.4647, "lon": 35.0462},
    "Київська": {"lat": 50.4501, "lon": 30.5234},
    "Полтавська": {"lat": 49.5895, "lon": 34.5514},
    "Запорізька": {"lat": 47.8388, "lon": 35.1396},
    "Черкаська": {"lat": 49.4444, "lon": 32.0598}
}

FISH_LIST = ["Лящ", "Карась", "Короп", "Щука", "Окунь", "Сом", "Плотва"]

# --- РОБОТА З БАЗОЮ ДАНИХ (SQLite) ---
def init_db():
    """Створення таблиць у базі даних, якщо вони ще не існують."""
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            region TEXT,
            fish_type TEXT,
            forecast_day TEXT,
            pressure REAL,
            wind REAL,
            temp REAL,
            stars INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            forecast_id INTEGER,
            rating TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_forecast_to_db(user_id, region, fish_type, forecast_day, pressure, wind, temp, stars):
    """Збереження згенерованого прогнозу в базу даних."""
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO forecasts (user_id, region, fish_type, forecast_day, pressure, wind, temp, stars)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, region, fish_type, forecast_day, pressure, wind, temp, stars))
    forecast_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return forecast_id

def save_feedback_to_db(user_id, forecast_id, rating):
    """Збереження відгуку користувача (👍 або 👎)."""
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO feedback (user_id, forecast_id, rating)
        VALUES (?, ?, ?)
    """, (user_id, forecast_id, rating))
    conn.commit()
    conn.close()

def get_user_history_from_db(user_id):
    """Отримання останніх 5 прогнозів користувача."""
    conn = sqlite3.connect("fishing_forecast.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT region, fish_type, forecast_day, stars, timestamp 
        FROM forecasts WHERE user_id = ? ORDER BY id DESC LIMIT 5
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- ДОПОМІЖНІ ФУНКЦІЇ ---
def get_wind_direction_text(degrees):
    """Конвертація градусів напрямку вітру в текстову абревіатуру."""
    if degrees is None:
        return "Н/Д"
    directions = ["Пн", "Пн-Сх", "Сх", "Пд-Сх", "Пд", "Пд-Зх", "Зх", "Пн-Зх"]
    return directions[round(degrees / 45) % 8]

def get_moon_phase(date_obj):
    """Розрахунок фази місяця за датою."""
    known_new_moon = datetime(2024, 1, 11)
    phase_days = (date_obj - known_new_moon).days % 29.53
    if phase_days < 1.8: return "Новомісяць 🌑 (поганий кльов)"
    elif phase_days < 7.4: return "Зростаючий місяць 🌒"
    elif phase_days < 11.1: return "Перша чверть 🌓"
    elif phase_days < 16.5: return "Повня 🌕 (активний хижак)"
    elif phase_days < 22.1: return "Спадаючий місяць 🌖"
    elif phase_days < 25.8: return "Остання чверть 🌗"
    else: return "Старий місяць 🌘"

def check_sun_activity(hour):
    """Перевірка сонячної активності (золоті години)."""
    if 4 <= hour <= 7:
        return "🌅 Світанок (золота година)", "Період максимальної ранкової активності риби.", 15
    elif 19 <= hour <= 21:
        return "🌇 Захід сонця (вечірній вихід)", "Час виходу великого хижака та ляща на мілководдя.", 15
    else:
        return "☀️ Денний/Нічний період", "Стандартна активність, риба тримається звичних глибин.", 0

# --- МЕТЕОРОЛОГІЧНИЙ КЛІЄНТ ТА АНАЛІЗУВАТОР ---
class MultiSourceWeatherClient:
    def __init__(self, lat, lon):
        self.lat = lat
        self.lon = lon

    async def fetch_open_meteo(self, session):
        """Отримання даних з основної моделі Open-Meteo GFS."""
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={self.lat}&longitude={self.lon}"
            "&hourly=temperature_2m,apparent_temperature,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m,cloud_cover,precipitation,sea_surface_temperature"
            "&timezone=auto&past_days=2&forecast_days=3"
        )
        try:
            async with session.get(url) as response:
                if response.status == 200: return await response.json()
        except Exception as e:
            logging.error(f"Помилка Open-Meteo: {e}")
        return None

    async def fetch_open_meteo_ecmwf(self, session):
        """Отримання даних з європейської моделі ECMWF."""
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={self.lat}&longitude={self.lon}"
            "&hourly=temperature_2m,apparent_temperature,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m,cloud_cover,precipitation"
            "&models=ecmwf_ifs04&timezone=auto&past_days=2&forecast_days=3"
        )
        try:
            async with session.get(url) as response:
                if response.status == 200: return await response.json()
        except Exception as e:
            logging.error(f"Помилка ECMWF: {e}")
        return None

    async def get_averaged_weather(self):
        """Усереднення показників двох погодних моделей."""
        async with aiohttp.ClientSession() as session:
            res1, res2 = await asyncio.gather(self.fetch_open_meteo(session), self.fetch_open_meteo_ecmwf(session))
        
        if not res1: return res2
        if not res2: return res1

        try:
            h1, h2 = res1["hourly"], res2["hourly"]
            averaged_hourly = {}
            keys = ["temperature_2m", "apparent_temperature", "relative_humidity_2m", "surface_pressure", "wind_speed_10m", "wind_direction_10m", "cloud_cover", "precipitation"]
            for key in keys:
                if key in h1 and key in h2:
                    averaged_hourly[key] = [(v1 + v2) / 2 if v1 is not None and v2 is not None else (v1 or v2) for v1, v2 in zip(h1[key], h2[key])]
            res1["hourly"] = {**h1, **averaged_hourly}
            return res1
        except Exception:
            return res1

    def evaluate_pressure_stability(self, pressures, current_index):
        """Аналіз стабільності атмосферного тиску за останні 48 годин."""
        if current_index < 48: return "Недостатньо історії", 0
        valid_pressures = [p for p in pressures[current_index - 48:current_index + 1] if p is not None]
        if not valid_pressures: return "Недостатньо даних", 0
        
        diff = max(valid_pressures) - min(valid_pressures)
        if diff <= 4: return "Стабільний (ідеально) ✅", 15
        elif diff <= 8: return "Помірно мінливий ⚠️", 0
        else: return "Стрибкоподібний (погано) ❌", -25

    def calculate_star_score(self, score_100):
        """Переведення 100-бальної шкали в 5 зірок."""
        if score_100 >= 80: return 5
        elif score_100 >= 60: return 4
        elif score_100 >= 40: return 3
        elif score_100 >= 20: return 2
        elif score_100 > 0: return 1
        else: return 0

    def generate_expert_commentary(self, fish_type, region, pressure_mm, wind_ms, wind_dir, precip, sun_title, sun_desc, temp, water_temp, score, humidity, cloud_cover):
        """Генерація РОЗШИРЕНОГО експертного аналізу для рибалки."""
        comments = []

        # 1. Час доби та освітлення
        comments.append(f"⏱ **Час та освітлення:** {sun_title}. {sun_desc}")

        # 2. Атмосферний тиск
        pressure_diff = round(pressure_mm - 750.0, 1)
        if abs(pressure_diff) <= 3:
            press_comment = f"Тиск близько норми ({pressure_mm} мм). Риба почувається комфортно по всій товщі води."
        elif pressure_diff > 3:
            press_comment = f"Підвищений тиск ({pressure_mm} мм). Мирна риба може притискатися до дна, шукайте її в ямах."
        else:
            press_comment = f"Знижений тиск ({pressure_mm} мм). Хижак стає більш активним, а мирна риба може підніматися у середні шари."
        comments.append(f"🌀 **Атмосферний тиск:** {press_comment}")

        # 3. Температура та водне середовище
        comments.append(f"🌡 **Температурний режим:** Повітря {temp}°C, вода ~{water_temp}°C. ")
        if temp > 25:
            comments.append("   • *Порада:* Через спеку шукайте рибу в тінистих місцях, під кущами або на течії, де більше кисню.")
        elif temp < 10:
            comments.append("   • *Порада:* Вода прохолодна, риба млява. Використовуйте дрібні наживки та акцентуйте на тваринних принадах.")

        # 4. Вітер та стан води
        if wind_ms < 2:
            wind_comment = f"Штиль або легкий вітерець ({wind_ms} м/с, {wind_dir}). Вода дзеркальна, риба обережна — потрібне делікатне оснащення."
        elif wind_ms <= 6:
            wind_comment = f"Помірний вітер ({wind_ms} м/с, {wind_dir}). Створює сприятливу ряб, що маскує жилку і збагачує воду киснем."
        else:
            wind_comment = f"Сильний вітер ({wind_ms} м/с, {wind_dir}). Складні умови для закидання. Шукайте затишні затоки або підвітряний берег."
        comments.append(f"💨 **Вітер та хвиля:** {wind_comment}")

        # 5. Опади та хмарність
        if precip > 1.0:
            comments.append(f"🌧 **Опади ({precip} мм):** Дощ змиває в воду кормові об'єкти та підвищує мутність. Це хороший шанс для лову сома, щуки та великого ляща.")
        elif cloud_cover > 70:
            comments.append(f"☁️ **Хмарність ({cloud_cover}%):** Похмура погода сприятлива для тривалого кльову хижака протягом усього дня.")
        else:
            comments.append("☀️ **Ясна погода:** Яскраве сонце зменшує активність риби на мілководді в середині дня.")

        # 6. Специфіка для вибраної риби
        is_predator = fish_type in ["Щука", "Окунь", "Сом"]
        if is_predator:
            comments.append(f"🎯 **Порада для лову ({fish_type}):** Використовуйте активні проводки на брівках та перепадах глибин.")
        else:
            comments.append(f"🎯 **Порада для лову ({fish_type}):** Додавайте у прикормку дрібну фракцію та експериментуйте з комбінаціями насадок (мотиль/опариш/кукурудза).")

        # 7. Загальний висновок
        if score >= 75:
            comments.append("\n🏆 **Підсумок:** Ідеальні умови! Не втрачайте шанс вирушити на водойму.")
        elif score >= 50:
            comments.append("\n⚖️ **Підсумок:** Добре середовище. Успіх залежатиме від правильного вибору місця та підбору наживки.")
        else:
            comments.append("\n⚠️ **Підсумок:** Погодні умови складні. Знадобиться максимум майстерності та терпіння.")

        return "\n".join(comments)

    async def evaluate_biting(self, fish_type, region, target_hour_index, day_offset=0):
        """Головний розрахунок прогнозу кльову."""
        data = await self.get_averaged_weather()
        if not data: return None
        
        hourly = data["hourly"]
        pressures = hourly["surface_pressure"]
        target_index = min(48 + (day_offset * 24) + target_hour_index, len(pressures) - 1)

        # Безпечне отримання значень (перевірка на None)
        raw_pressure = pressures[target_index]
        pressure_mm = (raw_pressure * 0.75006) if raw_pressure is not None else 750.0

        raw_wind = hourly["wind_speed_10m"][target_index]
        wind_ms = raw_wind if raw_wind is not None else 2.0

        raw_temp = hourly["temperature_2m"][target_index]
        temp = raw_temp if raw_temp is not None else 20.0

        raw_precip = hourly["precipitation"][target_index]
        precip = raw_precip if raw_precip is not None else 0.0

        raw_wind_dir = hourly["wind_direction_10m"][target_index]
        wind_dir = get_wind_direction_text(raw_wind_dir)

        raw_humidity = hourly["relative_humidity_2m"][target_index]
        humidity = raw_humidity if raw_humidity is not None else 50

        raw_cloud = hourly["cloud_cover"][target_index]
        cloud_cover = raw_cloud if raw_cloud is not None else 0

        water_temp = hourly.get("sea_surface_temperature", [None])[target_index] or round(temp * 0.8 + 4.0, 1)

        stability_text, stability_score = self.evaluate_pressure_stability(pressures, target_index)
        sun_title, sun_desc, sun_score = check_sun_activity(target_hour_index)
        
        # Обчислення підсумкового балу
        score = 100
        if 745 <= pressure_mm <= 758: score += 0
        else: score -= 20
        score += stability_score
        score += sun_score
        if precip > 2.0: score -= 25
        if wind_ms > 8: score -= 35
        elif wind_ms > 5: score -= 10

        final_score_100 = min(100, max(0, score))
        stars = self.calculate_star_score(final_score_100)
        
        # Розрахунок текстового відображення дня з датою для звіту
        target_date_obj = datetime.now() + timedelta(days=day_offset)
        date_formatted = target_date_obj.strftime("%d.%m.%Y")
        if day_offset == 0:
            forecast_day_text = f"Сьогодні ({date_formatted})"
        elif day_offset == 1:
            forecast_day_text = f"Завтра ({date_formatted})"
        else:
            forecast_day_text = date_formatted

        expert_commentary = self.generate_expert_commentary(
            fish_type, region, round(pressure_mm, 1), round(wind_ms, 1), wind_dir, 
            round(precip, 1), sun_title, sun_desc, round(temp, 1), round(water_temp, 1), 
            final_score_100, round(humidity), round(cloud_cover)
        )

        return {
            "fish": fish_type,
            "forecast_day": forecast_day_text,
            "pressure_mm": round(pressure_mm, 1),
            "pressure_stability": stability_text,
            "wind_ms": round(wind_ms, 1),
            "wind_dir": wind_dir,
            "humidity": round(humidity),
            "cloud_cover": round(cloud_cover),
            "precipitation": round(precip, 1),
            "temperature": round(temp, 1),
            "water_temp": round(water_temp, 1),
            "moon_phase": get_moon_phase(datetime.now() + timedelta(days=day_offset)),
            "stars": stars,
            "stars_graphic": "⭐" * stars + "☆" * (5 - stars),
            "expert_commentary": expert_commentary,
            "sources_used": "Open-Meteo (GFS + ECMWF)"
        }

user_states = {}
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- ХЕНДЛЕРИ ТЕЛЕГРАМУ ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Дніпропетровська"), KeyboardButton(text="Київська")],
            [KeyboardButton(text="Полтавська"), KeyboardButton(text="Запорізька")],
            [KeyboardButton(text="Черкаська")],
            [KeyboardButton(text="📜 Моя історія прогнозів")]
        ],
        resize_keyboard=True
    )
    await message.answer("Привіт! Оберіть область для риболовлі або перегляньте історію:", reply_markup=keyboard)

@dp.message(F.text == "📜 Моя історія прогнозів")
async def show_history(message: Message):
    rows = get_user_history_from_db(message.from_user.id)
    if not rows:
        await message.answer("У вас поки немає збережених прогнозів.")
        return
    
    text = "📜 **Ваші останні збережені прогнози:**\n\n"
    for row in rows:
        region, fish, day, stars, ts = row
        graphic = "⭐" * stars + "☆" * (5 - stars)
        text += f"📍 {region} | 🎣 {fish} ({day})\n Оцінка: {graphic} | 🕒 {ts}\n\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text.in_(REGIONS.keys()))
async def handle_region_choice(message: Message):
    user_states[message.from_user.id] = {"region": message.text}
    fish_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Лящ"), KeyboardButton(text="Карась"), KeyboardButton(text="Короп")],
            [KeyboardButton(text="Щука"), KeyboardButton(text="Окунь"), KeyboardButton(text="Сом")],
            [KeyboardButton(text="Плотва"), KeyboardButton(text="Змінити область")]
        ],
        resize_keyboard=True
    )
    await message.answer(f"Вибрано область: **{message.text}**.\nТепер оберіть рибу:", reply_markup=fish_keyboard, parse_mode="Markdown")

@dp.message(F.text == "Змінити область")
async def change_region(message: Message):
    await cmd_start(message)

@dp.message(F.text.in_(FISH_LIST))
async def handle_fish_choice(message: Message):
    user_id = message.from_user.id
    if user_id not in user_states or "region" not in user_states[user_id]:
        await message.answer("Будь ласка, спочатку оберіть область через команду /start")
        return
    user_states[user_id]["fish"] = message.text
    
    # Генеруємо динамічні інлайн-кнопки з реальними датами для наступних 3 днів
    today = datetime.now()
    keyboard_buttons = []
    
    for i in range(3):
        target_date = today + timedelta(days=i)
        date_str = target_date.strftime("%d.%m") # Формат день.місяць (наприклад, 24.08)
        
        if i == 0:
            label = f"Сьогодні ({date_str})"
        elif i == 1:
            label = f"Завтра ({date_str})"
        else:
            label = f"Післязавтра ({date_str})"
            
        keyboard_buttons.append([InlineKeyboardButton(text=label, callback_data=f"day_{i}")])

    inline_kb = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer(f"Риба: **{message.text}**.\nОберіть день для прогнозу:", reply_markup=inline_kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("day_"))
async def handle_day_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_states: user_states[user_id] = {}
    
    day_offset = int(callback.data.split("_")[1])
    user_states[user_id]["day_offset"] = day_offset
    
    # Зберігаємо точну дату у стані для подальшого використання
    target_date = datetime.now() + timedelta(days=day_offset)
    user_states[user_id]["target_date_str"] = target_date.strftime("%d.%m.%Y")
    
    time_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌅 Світанок (06:00)", callback_data="hour_6")],
            [InlineKeyboardButton(text="☀️ День (12:00)", callback_data="hour_12")],
            [InlineKeyboardButton(text="🌇 Захід сонця (20:00)", callback_data="hour_20")]
        ]
    )
    await callback.message.edit_text("Оберіть час доби (золоті години дають кращий кльов):", reply_markup=time_kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("hour_"))
async def handle_hour_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    hour = int(callback.data.split("_")[1])
    state = user_states.get(user_id, {})
    
    region = state.get("region", "Дніпропетровська")
    fish_type = state.get("fish", "Лящ")
    day_offset = state.get("day_offset", 0)
    
    coords = REGIONS[region]
    client = MultiSourceWeatherClient(coords["lat"], coords["lon"])
    result = await client.evaluate_biting(fish_type, region, hour, day_offset)
    
    if not result:
        await callback.message.answer("Не вдалося отримати метеодані.")
        await callback.answer()
        return

    forecast_id = save_forecast_to_db(user_id, region, fish_type, result['forecast_day'], result['pressure_mm'], result['wind_ms'], result['temperature'], result['stars'])

    response_text = (
        f"📍 **Область:** {region} | **День:** {result['forecast_day']}\n"
        f"🎣 **Прогноз кльову: {fish_type}** ({result['sources_used']})\n\n"
        f"🌕 Фаза місяця: {result['moon_phase']}\n"
        f"🌡 Температура: {result['temperature']}°C (води: ~{result['water_temp']}°C)\n"
        f"🌀 Тиск: {result['pressure_mm']} мм рт. ст. | {result['pressure_stability']}\n"
        f"💨 Вітер: {result['wind_ms']} м/с ({result['wind_dir']}) | 🌧 Опади: {result['precipitation']} мм\n\n"
        f"⭐ **Оцінка кльову:** {result['stars']}/5 ({result['stars_graphic']})\n\n"
        f"💡 **Розширений експертний аналіз:**\n{result['expert_commentary']}"
    )
    
    actions_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Поділитися в чаті клубу", callback_data=f"share_{result['stars']}_{fish_type}_{region}")],
            [InlineKeyboardButton(text="💬 Перейти в чат", url=GROUP_URL)],
            [
                InlineKeyboardButton(text="👍 Точний прогноз", callback_data=f"fb_good_{forecast_id}"),
                InlineKeyboardButton(text="👎 Хибний", callback_data=f"fb_bad_{forecast_id}")
            ]
        ]
    )
    
    await callback.message.answer(response_text, reply_markup=actions_kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("fb_"))
async def handle_feedback_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    rating = parts[1]
    forecast_id = int(parts[2])
    
    save_feedback_to_db(callback.from_user.id, forecast_id, rating)
    if rating == "good":
        await callback.answer("Дякуємо! Ваш відгук допоможе покращити алгоритми прогнозування 👍", show_alert=True)
    else:
        await callback.answer("Дякуємо за зворотний зв'язок! Ми врахуємо це у майбутніх оновленнях 👎", show_alert=True)

@dp.callback_query(F.data.startswith("share_"))
async def handle_share_callback(callback: CallbackQuery):
    try:
        _, stars, fish, region = callback.data.split("_", 3)
        graphic = "⭐" * int(stars) + "☆" * (5 - int(stars))
        user = callback.from_user

        share_text = (
            f"📢 **Рибалка {user.first_name} поділився прогнозом!**\n"
            f"📍 Область: {region} | Риба: 🎣 **{fish}**\n"
            f"⭐ Оцінка кльову: **{stars}/5 ({graphic})**\n"
            f"💬 Приєднуйтесь до обговорення!"
        )
        await bot.send_message(GROUP_CHAT_ID, share_text, parse_mode="Markdown")
        await callback.answer("✅ Прогноз успішно надіслано в чат клубу!", show_alert=True)
    except Exception as e:
        logging.error(f"Помилка поширення: {e}")
        await callback.answer("❌ Не вдалося надіслати. Перевірте ID групи.", show_alert=True)

# --- ГОЛОВНА ФУНКЦІЯ ЗАПУСКУ ---
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())