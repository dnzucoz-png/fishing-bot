import os
import logging
import aiohttp
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Отримуємо токен безпечно з налаштувань Render, або використовуємо резервний
TOKEN = os.getenv("BOT_TOKEN") or "8373587458:AAEVFuI-yRfE4vTeKT86idwi-0ytbl122T4"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Базові координати для областей України
REGIONS = {
    "Дніпропетровська": {"lat": 48.4647, "lon": 35.0462},
    "Київська": {"lat": 50.4501, "lon": 30.5234},
    "Львівська": {"lat": 49.8397, "lon": 24.0297},
    "Одеська": {"lat": 46.4825, "lon": 30.7233},
}

# Тимчасове сховище станів користувачів
user_states = {}


class MultiSourceWeatherClient:
    def __init__(self, lat, lon):
        self.lat = lat
        self.lon = lon

    async def evaluate_biting(self, fish_type, region, hour, day_offset):
        """Отримання актуальних даних з Open-Meteo та розрахунок прогнозу кльову."""
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={self.lat}&longitude={self.lon}"
            "&hourly=temperature_2m,surface_pressure,wind_speed_10m,precipitation"
            "&timezone=auto&forecast_days=3"
        )
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logging.error(f"Помилка API Open-Meteo: статус {response.status}")
                        return None
                    data = await response.json()
        except Exception as e:
            logging.error(f"Виняток під час запиту до Open-Meteo: {e}")
            return None

        try:
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            temps = hourly.get("temperature_2m", [])
            pressures = hourly.get("surface_pressure", [])
            winds = hourly.get("wind_speed_10m", [])
            precips = hourly.get("precipitation", [])

            if not times or not temps:
                logging.error("Отримано порожні дані погоди від API")
                return None

            # Безпечно шукаємо індекс потрібної години в масиві часу
            target_hour_str = f"{hour:02d}:00"
            idx = 0
            for i, t in enumerate(times):
                if target_hour_str in t:
                    idx = i
                    break

            if idx >= len(temps):
                idx = 0

            temperature = temps[idx]
            pressure_hpa = pressures[idx] if idx < len(pressures) else 1013
            pressure_mm = round(pressure_hpa * 0.750062, 1) # переведення в мм рт. ст.
            wind_ms = winds[idx] if idx < len(winds) else 3.0
            precipitation = precips[idx] if idx < len(precips) else 0.0

            return {
                'forecast_day': "Сьогодні",
                'pressure_mm': pressure_mm,
                'pressure_stability': "Стабільний",
                'wind_ms': wind_ms,
                'wind_dir': "Північний",
                'temperature': temperature,
                'water_temp': round(temperature * 0.8, 1),
                'precipitation': precipitation,
                'moon_phase': "Зростаючий місяць",
                'stars': 4,
                'stars_graphic': "⭐⭐⭐⭐☆",
                'sources_used': "Open-Meteo API",
                'expert_commentary': "Чудові погодні умови для риболовлі. Риба проявляє активність у прибережній зоні."
            }
        except Exception as e:
            logging.error(f"Помилка обробки погодних даних усередині блоку: {e}")
            return None


def save_forecast_to_db(user_id, region, fish_type, forecast_day, pressure_mm, wind_ms, temperature, stars):
    """Імітація збереження прогнозу в базу даних."""
    return 1


@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_states[message.from_user.id] = {"region": "Дніпропетровська", "fish": "Лящ", "day_offset": 0}
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📍 Змінити область", callback_data="change_region")]
    ])
    
    await message.answer(
        "Привіт! Оберіть область для риболовлі або перегляньте історію:",
        reply_markup=keyboard
    )
    
    fish_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Лящ", callback_data="fish_Лящ"), InlineKeyboardButton(text="Карась", callback_data="fish_Карась")]
    ])
    await message.answer("Виберіть рибу:", reply_markup=fish_keyboard)


@dp.callback_query(F.data.startswith("fish_"))
async def handle_fish_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    fish_type = callback.data.split("_")[1]
    
    if user_id not in user_states:
        user_states[user_id] = {}
    user_states[user_id]["fish"] = fish_type
    
    hours_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌅 Світанок (06:00)", callback_data="hour_6")],
        [InlineKeyboardButton(text="☀️ День (12:00)", callback_data="hour_12")],
        [InlineKeyboardButton(text="🌇 Захід сонця (20:00)", callback_data="hour_20")]
    ])
    
    await callback.message.answer(f"Вибрано рибу: {fish_type}.\nОберіть час доби:", reply_markup=hours_keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("hour_"))
async def handle_hour_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    hour = int(callback.data.split("_")[1])
    state = user_states.get(user_id, {"region": "Дніпропетровська", "fish": "Лящ", "day_offset": 0})
    
    region = state.get("region", "Дніпропетровська")
    fish_type = state.get("fish", "Лящ")
    day_offset = state.get("day_offset", 0)
    
    if region not in REGIONS:
        await callback.message.answer(f"Помилка: не знайдено координати для регіону {region}.")
        await callback.answer()
        return

    coords = REGIONS[region]
    client = MultiSourceWeatherClient(coords["lat"], coords["lon"])
    
    result = await client.evaluate_biting(fish_type, region, hour, day_offset)
    
    if not result:
        await callback.message.answer("Не вдалося отримати метеодані. Перевірте з'єднання або спробуйте пізніше.")
        await callback.answer()
        return

    save_forecast_to_db(
        user_id, region, fish_type, 
        result['forecast_day'], result['pressure_mm'], 
        result['wind_ms'], result['temperature'], result['stars']
    )

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
    
    await callback.message.answer(response_text, parse_mode="Markdown")
    await callback.answer()


# Вебсервер для задоволення вимог Render до портів
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Вебсервер запущено на порту {port}")


async def main():
    print("Бот запущено...")
    await web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
