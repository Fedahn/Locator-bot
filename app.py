import logging
import sqlite3
import asyncio
import aiohttp
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.filters import Text
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from dotenv import load_dotenv
import os
import threading
from flask import Flask

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

# --- Состояния ---
class Registration(StatesGroup):
    first_name = State()
    last_name = State()

class LiveLocationStates(StatesGroup):
    waiting_for_location = State()

temp_user_data = {}

# --- База данных ---
def init_db():
    conn = sqlite3.connect('locator.db')
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            telegram_username TEXT,
            first_name TEXT,
            last_name TEXT,
            role TEXT CHECK(role IN ('employee', 'driver'))
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS ride_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            pickup_point TEXT CHECK(pickup_point IN ('ПРЦ', 'ОРЛ А', 'КДП', 'РЭМ', 'Город')),
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS driver_locations (
            user_id INTEGER PRIMARY KEY,
            latitude REAL,
            longitude REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS driver_tracker (
            user_id INTEGER PRIMARY KEY,
            is_active INTEGER DEFAULT 0,
            start_time TIMESTAMP,
            expire_time TIMESTAMP,
            live_message_id INTEGER,
            live_chat_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    conn.commit()
    conn.close()

def get_start_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton('▶️ Начать'))
    return kb

def get_employee_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton('🙋‍♂️ Я еду'))
    kb.add(KeyboardButton('👀 Где водитель?'))
    return kb

def get_driver_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton('📍 Отправить мою геолокацию'))
    kb.add(KeyboardButton('📋 Список заявок'))
    kb.add(KeyboardButton('🟢 Включить Live Location'))
    kb.add(KeyboardButton('🔴 Остановить трансляцию'))
    return kb

def expire_ride_requests():
    conn = sqlite3.connect('locator.db')
    cur = conn.cursor()
    cur.execute("UPDATE ride_requests SET status = 'expired' WHERE status = 'active'")
    conn.commit()
    conn.close()

async def deactivate_tracker(bot, user_id: int, stop_live: bool = True):
    conn = sqlite3.connect('locator.db')
    cur = conn.cursor()
    cur.execute("SELECT live_chat_id, live_message_id FROM driver_tracker WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        live_chat_id, live_message_id = row
        if stop_live and live_chat_id and live_message_id:
            try:
                await bot.stop_message_live_location(chat_id=live_chat_id, message_id=live_message_id)
            except Exception as e:
                logging.error(f"Ошибка остановки Live Location: {e}")
    cur.execute("DELETE FROM driver_tracker WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    expire_ride_requests()
    conn = sqlite3.connect('locator.db')
    cur = conn.cursor()
    cur.execute("DELETE FROM driver_locations WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    logging.info(f"Tracker deactivated for driver {user_id}")

async def auto_expire_tracker(bot, user_id: int, expire_time: datetime):
    delay = (expire_time - datetime.now()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    await deactivate_tracker(bot, user_id, stop_live=True)
    await bot.send_message(user_id, "⏰ Ваш 2-часовой локатор истёк. Все заявки сброшены. Чтобы продолжить, включите локатор заново.", reply_markup=get_driver_keyboard())

def adapt_datetime(dt: datetime):
    return dt.isoformat()
sqlite3.register_adapter(datetime, adapt_datetime)

# --- Хендлеры ---
def register_handlers(dp: Dispatcher):
    @dp.message_handler(Text(equals='▶️ Начать'))
    async def start_button(message: types.Message):
        await cmd_start(message)

    @dp.message_handler(commands=['start'])
    async def cmd_start(message: types.Message):
        user_id = message.from_user.id
        conn = sqlite3.connect('locator.db')
        cur = conn.cursor()
        cur.execute('SELECT role FROM users WHERE user_id = ?', (user_id,))
        user = cur.fetchone()
        conn.close()
        if user:
            role = user[0]
            if role == 'employee':
                await message.answer('👋 Добро пожаловать! Вы зарегистрированы как сотрудник.', reply_markup=get_employee_keyboard())
            else:
                await message.answer('👋 Добро пожаловать! Вы зарегистрированы как водитель.', reply_markup=get_driver_keyboard())
        else:
            await message.answer('Добро пожаловать! Давайте зарегистрируемся. Введите ваше имя:', reply_markup=ReplyKeyboardMarkup(resize_keyboard=True).add(KeyboardButton('Отмена')))
            await Registration.first_name.set()

    @dp.message_handler(state=Registration.first_name)
    async def reg_first_name(message: types.Message, state: FSMContext):
        if message.text == 'Отмена':
            await state.finish()
            await message.answer('Регистрация отменена.', reply_markup=get_start_keyboard())
            return
        await state.update_data(first_name=message.text)
        await message.answer('Спасибо! Теперь введите вашу фамилию:')
        await Registration.last_name.set()

    @dp.message_handler(state=Registration.last_name)
    async def reg_last_name(message: types.Message, state: FSMContext):
        if message.text == 'Отмена':
            await state.finish()
            await message.answer('Регистрация отменена.', reply_markup=get_start_keyboard())
            return
        data = await state.get_data()
        first_name = data['first_name']
        last_name = message.text
        temp_user_data[message.from_user.id] = {
            'first_name': first_name,
            'last_name': last_name,
            'telegram_username': message.from_user.username
        }
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton('👨‍💼 Сотрудник', callback_data='role_employee'))
        kb.add(InlineKeyboardButton('🚗 Водитель', callback_data='role_driver'))
        await message.answer(f'Приятно познакомиться, {first_name} {last_name}! Кто вы?', reply_markup=kb)
        await state.finish()

    @dp.callback_query_handler(lambda c: c.data in ['role_employee', 'role_driver'])
    async def role_selected(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        role = 'employee' if callback.data == 'role_employee' else 'driver'
        user = temp_user_data.get(user_id, {})
        first_name = user.get('first_name', '')
        last_name = user.get('last_name', '')
        username = user.get('telegram_username', '')
        conn = sqlite3.connect('locator.db')
        cur = conn.cursor()
        cur.execute('INSERT INTO users (user_id, telegram_username, first_name, last_name, role) VALUES (?, ?, ?, ?, ?)',
                    (user_id, username, first_name, last_name, role))
        conn.commit()
        conn.close()
        await callback.message.edit_text('✅ Регистрация успешно завершена!')
        if role == 'employee':
            await callback.message.answer('Вы зарегистрированы как сотрудник.', reply_markup=get_employee_keyboard())
        else:
            await callback.message.answer('Вы зарегистрированы как водитель.', reply_markup=get_driver_keyboard())
        await callback.answer()

    @dp.message_handler(Text(equals='🙋‍♂️ Я еду'))
    async def i_go(message: types.Message):
        kb = InlineKeyboardMarkup(row_width=2)
        for point in ['ПРЦ', 'ОРЛ А', 'КДП', 'РЭМ', 'Город']:
            kb.add(InlineKeyboardButton(point, callback_data=f'point_{point}'))
        await message.answer('Выберите точку маршрута:', reply_markup=kb)

    @dp.callback_query_handler(lambda c: c.data and c.data.startswith('point_'))
    async def point_selected(callback: types.CallbackQuery):
        point = callback.data.split('_')[1]
        user_id = callback.from_user.id
        conn = sqlite3.connect('locator.db')
        cur = conn.cursor()
        cur.execute('INSERT INTO ride_requests (user_id, pickup_point) VALUES (?, ?)', (user_id, point))
        conn.commit()
        conn.close()
        await callback.message.edit_text(f'✅ Заявка на посадку в точке "{point}" создана!')
        await callback.answer()

    @dp.message_handler(Text(equals='👀 Где водитель?'))
    async def where_driver(message: types.Message):
        conn = sqlite3.connect('locator.db')
        cur = conn.cursor()
        cur.execute('SELECT latitude, longitude FROM driver_locations ORDER BY updated_at DESC LIMIT 1')
        loc = cur.fetchone()
        conn.close()
        if loc and loc[0] is not None:
            lat, lon = loc
            await message.answer_location(latitude=lat, longitude=lon)
        else:
            await message.answer('🚫 Водитель сейчас не в пути или не поделился своей геолокацией.')

    @dp.message_handler(content_types=['location'])
    async def driver_location(message: types.Message):
        user_id = message.from_user.id
        lat = message.location.latitude
        lon = message.location.longitude
        conn = sqlite3.connect('locator.db')
        cur = conn.cursor()
        cur.execute('SELECT role FROM users WHERE user_id = ?', (user_id,))
        user = cur.fetchone()
        if user and user[0] == 'driver':
            cur.execute('INSERT OR REPLACE INTO driver_locations (user_id, latitude, longitude, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)',
                        (user_id, lat, lon))
            conn.commit()
            await message.answer('✅ Ваша геолокация обновлена!', reply_markup=get_driver_keyboard())
        else:
            await message.answer('⛔ Вы не зарегистрированы как водитель.')
        conn.close()

    @dp.message_handler(Text(equals='🟢 Включить Live Location'))
    async def ask_live_location(message: types.Message):
        user_id = message.from_user.id
        conn = sqlite3.connect('locator.db')
        cur = conn.cursor()
        cur.execute('SELECT is_active FROM driver_tracker WHERE user_id = ?', (user_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0] == 1:
            await message.answer('🟢 Локатор уже включён. Остановите текущую трансляцию перед включением новой.')
            return
        markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(KeyboardButton('📍 Поделиться геолокацией', request_location=True))
        await message.answer(
            "📍 Пожалуйста, нажмите на кнопку ниже и отправьте ваше текущее местоположение, чтобы начать 2-часовую трансляцию.\n"
            "После этого заявки сотрудников будут активны.",
            reply_markup=markup
        )
        await LiveLocationStates.waiting_for_location.set()

    @dp.message_handler(content_types=['location'], state=LiveLocationStates.waiting_for_location)
    async def start_live_location(message: types.Message, state: FSMContext):
        user_id = message.from_user.id
        await message.delete()
        live_message = await dp.bot.send_location(
            chat_id=message.chat.id,
            latitude=message.location.latitude,
            longitude=message.location.longitude,
            live_period=7200
        )
        expire_time = datetime.now() + timedelta(seconds=7200)
        conn = sqlite3.connect('locator.db')
        cur = conn.cursor()
        cur.execute('''
            INSERT OR REPLACE INTO driver_tracker (user_id, is_active, start_time, expire_time, live_message_id, live_chat_id)
            VALUES (?, 1, ?, ?, ?, ?)
        ''', (user_id, datetime.now(), expire_time, live_message.message_id, live_message.chat.id))
        conn.commit()
        conn.close()
        asyncio.create_task(auto_expire_tracker(dp.bot, user_id, expire_time))
        await state.finish()
        await message.answer(
            "✅ Трансляция вашего местоположения запущена на 2 часа!\n"
            "Теперь заявки сотрудников будут активны. Вы можете просмотреть их по кнопке '📋 Список заявок'.\n"
            "Чтобы остановить досрочно, нажмите '🔴 Остановить трансляцию'.",
            reply_markup=get_driver_keyboard()
        )

    @dp.message_handler(Text(equals='🔴 Остановить трансляцию'))
    async def stop_live_location(message: types.Message):
        user_id = message.from_user.id
        conn = sqlite3.connect('locator.db')
        cur = conn.cursor()
        cur.execute('SELECT is_active FROM driver_tracker WHERE user_id = ?', (user_id,))
        row = cur.fetchone()
        conn.close()
        if not row or row[0] != 1:
            await message.answer("🔴 У вас нет активной трансляции.")
            return
        await deactivate_tracker(dp.bot, user_id, stop_live=True)
        await message.answer("🔴 Трансляция остановлена. Все заявки сброшены.", reply_markup=get_driver_keyboard())

    @dp.message_handler(Text(equals='📋 Список заявок'))
    async def show_requests(message: types.Message):
        user_id = message.from_user.id
        conn = sqlite3.connect('locator.db')
        cur = conn.cursor()
        cur.execute('SELECT role FROM users WHERE user_id = ?', (user_id,))
        user = cur.fetchone()
        if not user or user[0] != 'driver':
            await message.answer('⛔ Только для водителей.')
            conn.close()
            return
        cur.execute('SELECT is_active, expire_time FROM driver_tracker WHERE user_id = ?', (user_id,))
        tracker = cur.fetchone()
        if not tracker or tracker[0] != 1:
            await message.answer("🚫 Список заявок доступен только когда активен локатор. Включите Live Location.")
            conn.close()
            return
        expire_time = datetime.fromisoformat(tracker[1])
        if datetime.now() > expire_time:
            await deactivate_tracker(dp.bot, user_id, stop_live=False)
            await message.answer("⏰ Время действия локатора истекло. Заявки сброшены. Включите Live Location заново.")
            conn.close()
            return
        cur.execute('''
            SELECT ride_requests.request_id, users.first_name, users.last_name, ride_requests.pickup_point, ride_requests.created_at
            FROM ride_requests
            JOIN users ON ride_requests.user_id = users.user_id
            WHERE ride_requests.status = 'active'
            ORDER BY ride_requests.created_at DESC
        ''')
        rows = cur.fetchall()
        conn.close()
        if not rows:
            await message.answer('📭 Нет активных заявок.')
            return
        text = "📋 *Активные заявки:*\n\n"
        for req_id, first_name, last_name, point, created in rows:
            text += f"👤 {first_name} {last_name}\n📍 Точка: {point}\n🕒 {created}\n---\n"
        await message.answer(text, parse_mode='Markdown')

    @dp.message_handler(Text(equals='Отмена'))
    async def cancel_registration(message: types.Message, state: FSMContext):
        current_state = await state.get_state()
        if current_state is not None:
            await state.finish()
            await message.answer('Действие отменено.', reply_markup=get_start_keyboard())
        else:
            await message.answer('Нет активного действия для отмены.')

# --- Запуск бота в потоке для Flask ---
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = Bot(token=BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(bot, storage=storage)
    dp.middleware.setup(LoggingMiddleware())
    register_handlers(dp)
    init_db()
    executor.start_polling(dp, skip_updates=True)

# --- Flask приложение для Render ---
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is running!"

def start_bot_thread():
    thread = threading.Thread(target=run_bot)
    thread.daemon = True
    thread.start()

if __name__ == '__main__':
    start_bot_thread()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
