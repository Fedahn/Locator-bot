import logging
import asyncio
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.dispatcher.filters import Text
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.executor import start_webhook
from dotenv import load_dotenv
import asyncpg

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')

# === НАСТРОЙКИ ===
GROUP_CHAT_ID = -1003593347493

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# --- Состояния ---
class Registration(StatesGroup):
    first_name = State()
    last_name = State()

class LiveLocationStates(StatesGroup):
    waiting_for_location = State()

temp_user_data = {}

# --- База данных ---
async def init_db():
    """Создаёт таблицы, если их нет"""
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            telegram_username TEXT,
            first_name TEXT,
            last_name TEXT,
            role TEXT CHECK(role IN ('employee', 'driver'))
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS ride_requests (
            request_id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            pickup_point TEXT CHECK(pickup_point IN ('Автобаза', 'КДП', 'Город')),
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS driver_locations (
            user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
            latitude REAL,
            longitude REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS driver_tracker (
            user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
            is_active INTEGER DEFAULT 0,
            start_time TIMESTAMP,
            expire_time TIMESTAMP,
            live_message_id BIGINT,
            live_chat_id BIGINT
        )
    ''')
    await conn.close()
    logging.info("✅ Database tables created")

async def clear_db():
    """Очищает все данные (заявки, трекеры, геолокации), но оставляет пользователей"""
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DELETE FROM ride_requests")
    await conn.execute("DELETE FROM driver_tracker")
    await conn.execute("DELETE FROM driver_locations")
    await conn.close()
    logging.info("🗑️ Database cleared (ride_requests, driver_tracker, driver_locations)")

async def get_connection():
    return await asyncpg.connect(DATABASE_URL)

def get_start_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton('▶️ Начать'))
    return kb

def get_main_menu(role=None):
    """Главное меню после регистрации"""
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    if role == 'employee':
        kb.add(KeyboardButton('🙋‍♂️ Я еду'))
        kb.add(KeyboardButton('👀 Где водитель?'))
        kb.add(KeyboardButton('❌ Отменить мою заявку'))
        kb.add(KeyboardButton('🔄 Сменить роль'))
    elif role == 'driver':
        kb.add(KeyboardButton('📍 Отправить мою геолокацию'))
        kb.add(KeyboardButton('📋 Список заявок'))
        kb.add(KeyboardButton('📊 Кто едет?'))
        kb.add(KeyboardButton('🟢 Включить Live Location'))
        kb.add(KeyboardButton('🔴 Остановить трансляцию'))
        kb.add(KeyboardButton('🔄 Сменить роль'))
    else:
        kb.add(KeyboardButton('▶️ Начать'))
    return kb

async def notify_driver_about_new_request(user_name, pickup_point):
    conn = await get_connection()
    driver = await conn.fetchrow("SELECT user_id FROM users WHERE role = 'driver' LIMIT 1")
    await conn.close()
    if driver:
        await bot.send_message(
            driver['user_id'],
            f"🆕 *Новая заявка!*\n\n👤 {user_name}\n📍 Точка: {pickup_point}",
            parse_mode='Markdown'
        )

async def deactivate_tracker(user_id: int, stop_live: bool = True):
    conn = await get_connection()
    row = await conn.fetchrow("SELECT live_chat_id, live_message_id FROM driver_tracker WHERE user_id = $1", user_id)
    if row and stop_live:
        try:
            await bot.stop_message_live_location(chat_id=row['live_chat_id'], message_id=row['live_message_id'])
        except Exception as e:
            logging.error(f"Ошибка остановки Live Location: {e}")
    await conn.execute("DELETE FROM driver_tracker WHERE user_id = $1", user_id)
    await conn.execute("DELETE FROM ride_requests WHERE status = 'active'")
    await conn.execute("DELETE FROM driver_locations WHERE user_id = $1", user_id)
    await conn.close()
    logging.info(f"Tracker deactivated for user {user_id}")

async def auto_expire_tracker(user_id: int, expire_time: datetime):
    delay = (expire_time - datetime.now()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    await deactivate_tracker(user_id, stop_live=True)
    await bot.send_message(user_id, "⏰ 2-часовой локатор истёк. Заявки сброшены.")
    await bot.send_message(GROUP_CHAT_ID, "⏰ Трансляция местоположения завершена. Заявки сброшены.")

# --- Хендлеры ---
def register_handlers(dp: Dispatcher):
    temp_user_data = {}

    @dp.message_handler(commands=['start'])
    async def cmd_start(message: types.Message):
        user_id = message.from_user.id
        conn = await get_connection()
        user = await conn.fetchrow("SELECT role FROM users WHERE user_id = $1", user_id)
        await conn.close()
        
        welcome_text = (
            "👋 *Добро пожаловать в бот Локатор!*\n\n"
            "Я помогу водителю автобуса узнавать, кто и где хочет сесть, "
            "а сотрудникам — быстро отправлять заявки.\n\n"
            "📍 *Как это работает:*\n"
            "• Водитель включает Live Location\n"
            "• Сотрудники отправляют заявки\n"
            "• Водитель видит список желающих\n\n"
            "👇 *Чтобы начать, нажмите кнопку «Начать»*"
        )
        
        if user:
            role = user['role']
            await message.answer(
                f"👋 С возвращением! Вы зарегистрированы как {'сотрудник' if role == 'employee' else 'водитель'}.", 
                reply_markup=get_main_menu(role)
            )
        else:
            await message.answer(welcome_text, parse_mode='Markdown', reply_markup=get_start_keyboard())

    @dp.message_handler(Text(equals='▶️ Начать'))
    async def start_button(message: types.Message):
        user_id = message.from_user.id
        conn = await get_connection()
        user = await conn.fetchrow("SELECT role FROM users WHERE user_id = $1", user_id)
        await conn.close()
        
        if user:
            role = user['role']
            await message.answer(
                f"👋 Вы уже зарегистрированы как {'сотрудник' if role == 'employee' else 'водитель'}.",
                reply_markup=get_main_menu(role)
            )
        else:
            await message.answer("Давайте зарегистрируемся! Введите ваше имя:", reply_markup=ReplyKeyboardMarkup(resize_keyboard=True).add(KeyboardButton('Отмена')))
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
        conn = await get_connection()
        await conn.execute("INSERT INTO users (user_id, telegram_username, first_name, last_name, role) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (user_id) DO NOTHING",
                           user_id, username, first_name, last_name, role)
        await conn.close()
        await callback.message.edit_text('✅ Регистрация успешно завершена!')
        await callback.message.answer(
            f'Вы зарегистрированы как {"сотрудник" if role == "employee" else "водитель"}.',
            reply_markup=get_main_menu(role)
        )
        await callback.answer()

    @dp.message_handler(Text(equals='🔄 Сменить роль'))
    async def change_role(message: types.Message):
        user_id = message.from_user.id
        conn = await get_connection()
        user = await conn.fetchrow("SELECT role FROM users WHERE user_id = $1", user_id)
        if user:
            current_role = user['role']
            new_role = 'driver' if current_role == 'employee' else 'employee'
            await conn.execute("UPDATE users SET role = $1 WHERE user_id = $2", new_role, user_id)
            await conn.execute("DELETE FROM ride_requests WHERE user_id = $1", user_id)
            role_text = "водитель" if new_role == 'driver' else "сотрудник"
            await message.answer(
                f'✅ Ваша роль изменена на "{role_text}".\n\n'
                f'Теперь вам доступно соответствующее меню.',
                reply_markup=get_main_menu(new_role)
            )
        else:
            await message.answer('❌ Пользователь не найден. Пожалуйста, пройдите регистрацию через /start')
        await conn.close()

    @dp.message_handler(Text(equals='🙋‍♂️ Я еду'))
    async def i_go(message: types.Message):
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton('🚌 Автобаза', callback_data='point_Автобаза'))
        kb.add(InlineKeyboardButton('🏢 КДП', callback_data='point_КДП'))
        kb.add(InlineKeyboardButton('🏙️ Город', callback_data='point_Город'))
        await message.answer('Выберите точку посадки:', reply_markup=kb)

    @dp.callback_query_handler(lambda c: c.data and c.data.startswith('point_'))
    async def point_selected(callback: types.CallbackQuery):
        point = callback.data.split('_')[1]
        user_id = callback.from_user.id
        conn = await get_connection()
        driver_active = await conn.fetchrow("SELECT user_id FROM driver_tracker WHERE is_active = 1 LIMIT 1")
        if not driver_active:
            await callback.message.edit_text('❌ Водитель не в пути. Дождитесь начала маршрута.')
            await conn.close()
            await callback.answer()
            return
        await conn.execute("INSERT INTO ride_requests (user_id, pickup_point) VALUES ($1, $2)", user_id, point)
        user = await conn.fetchrow("SELECT first_name, last_name FROM users WHERE user_id = $1", user_id)
        await conn.close()
        if user:
            user_name = f"{user['first_name']} {user['last_name']}"
            await notify_driver_about_new_request(user_name, point)
        await callback.message.edit_text(f'✅ Заявка на посадку в точке "{point}" создана!')
        await callback.answer()

    @dp.message_handler(Text(equals='👀 Где водитель?'))
    async def where_driver(message: types.Message):
        conn = await get_connection()
        loc = await conn.fetchrow("SELECT latitude, longitude FROM driver_locations ORDER BY updated_at DESC LIMIT 1")
        await conn.close()
        if loc and loc['latitude'] is not None:
            await message.answer_location(latitude=loc['latitude'], longitude=loc['longitude'])
        else:
            await message.answer('🚫 Водитель сейчас не в пути или не поделился своей геолокацией.')

    @dp.message_handler(Text(equals='❌ Отменить мою заявку'))
    async def cancel_my_request(message: types.Message):
        user_id = message.from_user.id
        conn = await get_connection()
        request = await conn.fetchrow("SELECT request_id, pickup_point FROM ride_requests WHERE user_id = $1 AND status = 'active'", user_id)
        if request:
            await conn.execute("DELETE FROM ride_requests WHERE request_id = $1", request['request_id'])
            await message.answer(f'❌ Ваша заявка на точку "{request["pickup_point"]}" отменена.')
            driver = await conn.fetchrow("SELECT user_id FROM users WHERE role = 'driver' LIMIT 1")
            if driver:
                await bot.send_message(driver['user_id'], f"❌ Заявка отменена!\n👤 {message.from_user.first_name} {message.from_user.last_name}\n📍 {request['pickup_point']}")
        else:
            await message.answer('❌ У вас нет активных заявок.')
        await conn.close()

    @dp.message_handler(content_types=['location'])
    async def driver_location(message: types.Message):
        user_id = message.from_user.id
        lat = message.location.latitude
        lon = message.location.longitude
        conn = await get_connection()
        user = await conn.fetchrow("SELECT role FROM users WHERE user_id = $1", user_id)
        if user and user['role'] == 'driver':
            await conn.execute("INSERT INTO driver_locations (user_id, latitude, longitude, updated_at) VALUES ($1, $2, $3, CURRENT_TIMESTAMP) ON CONFLICT (user_id) DO UPDATE SET latitude = $2, longitude = $3, updated_at = CURRENT_TIMESTAMP",
                               user_id, lat, lon)
            await message.answer('✅ Ваша геолокация обновлена!', reply_markup=get_main_menu('driver'))
        else:
            await message.answer('⛔ Вы не зарегистрированы как водитель.')
        await conn.close()

    @dp.message_handler(Text(equals='🟢 Включить Live Location'))
    async def ask_live_location(message: types.Message):
        user_id = message.from_user.id
        conn = await get_connection()
        row = await conn.fetchrow("SELECT is_active FROM driver_tracker WHERE user_id = $1", user_id)
        await conn.close()
        if row and row['is_active'] == 1:
            await message.answer('🟢 Локатор уже включён.')
            return
        markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(KeyboardButton('📍 Поделиться геолокацией', request_location=True))
        await message.answer(
            "📍 Нажмите на кнопку ниже и отправьте ваше текущее местоположение, чтобы начать 2-часовую трансляцию.\n\n"
            "После этого сотрудники получат уведомление и смогут отправлять заявки.",
            reply_markup=markup
        )
        await LiveLocationStates.waiting_for_location.set()

    @dp.message_handler(content_types=['location'], state=LiveLocationStates.waiting_for_location)
    async def start_live_location(message: types.Message, state: FSMContext):
        user_id = message.from_user.id
        lat = message.location.latitude
        lon = message.location.longitude
        conn = await get_connection()
        await conn.execute("INSERT INTO driver_locations (user_id, latitude, longitude, updated_at) VALUES ($1, $2, $3, CURRENT_TIMESTAMP) ON CONFLICT (user_id) DO UPDATE SET latitude = $2, longitude = $3, updated_at = CURRENT_TIMESTAMP",
                           user_id, lat, lon)
        await conn.close()
        await message.delete()
        live_message = await bot.send_location(chat_id=GROUP_CHAT_ID, latitude=lat, longitude=lon, live_period=7200)
        expire_time = datetime.now() + timedelta(seconds=7200)
        conn = await get_connection()
        await conn.execute("INSERT INTO driver_tracker (user_id, is_active, start_time, expire_time, live_message_id, live_chat_id) VALUES ($1, 1, $2, $3, $4, $5) ON CONFLICT (user_id) DO UPDATE SET is_active = 1, start_time = $2, expire_time = $3, live_message_id = $4, live_chat_id = $5",
                           user_id, datetime.now(), expire_time, live_message.message_id, live_message.chat.id)
        await conn.close()
        asyncio.create_task(auto_expire_tracker(user_id, expire_time))
        await state.finish()
        await message.answer("✅ Трансляция запущена на 2 часа!", reply_markup=get_main_menu('driver'))
        await bot.send_message(GROUP_CHAT_ID, "🚌 Водитель начал маршрут! Отправляйте заявки в личку боту.", parse_mode='Markdown')

    @dp.message_handler(Text(equals='🔴 Остановить трансляцию'))
    async def stop_live_location(message: types.Message):
        await deactivate_tracker(message.from_user.id, stop_live=True)
        await message.answer("🔴 Трансляция остановлена. Все заявки сброшены.", reply_markup=get_main_menu('driver'))

    @dp.message_handler(Text(equals='📋 Список заявок'))
    async def show_requests(message: types.Message):
        user_id = message.from_user.id
        conn = await get_connection()
        user = await conn.fetchrow("SELECT role FROM users WHERE user_id = $1", user_id)
        if not user or user['role'] != 'driver':
            await message.answer('⛔ Только для водителей.')
            await conn.close()
            return
        rows = await conn.fetch('''
            SELECT users.first_name, users.last_name, ride_requests.pickup_point, ride_requests.created_at
            FROM ride_requests
            JOIN users ON ride_requests.user_id = users.user_id
            WHERE ride_requests.status = 'active'
            ORDER BY 
                CASE ride_requests.pickup_point
                    WHEN 'Автобаза' THEN 1
                    WHEN 'КДП' THEN 2
                    WHEN 'Город' THEN 3
                END,
                ride_requests.created_at ASC
        ''')
        await conn.close()
        if not rows:
            await message.answer('📭 Нет активных заявок.')
            return
        text = "📋 *Активные заявки (в порядке маршрута):*\n\n"
        for row in rows:
            text += f"👤 {row['first_name']} {row['last_name']}\n📍 {row['pickup_point']}\n🕒 {row['created_at']}\n---\n"
        await message.answer(text, parse_mode='Markdown')

    @dp.message_handler(Text(equals='📊 Кто едет?'))
    async def show_stats(message: types.Message):
        conn = await get_connection()
        rows = await conn.fetch('''
            SELECT pickup_point, COUNT(*), STRING_AGG(first_name || ' ' || last_name, ', ') as names
            FROM ride_requests
            JOIN users ON ride_requests.user_id = users.user_id
            WHERE status = 'active'
            GROUP BY pickup_point
            ORDER BY 
                CASE pickup_point
                    WHEN 'Автобаза' THEN 1
                    WHEN 'КДП' THEN 2
                    WHEN 'Город' THEN 3
                END
        ''')
        await conn.close()
        if not rows:
            await message.answer('📭 Нет активных заявок.')
            return
        text = "📊 *Кто едет:*\n\n"
        for row in rows:
            text += f"*{row['pickup_point']}*: {row['count']} чел.\n👥 {row['names']}\n\n"
        await message.answer(text, parse_mode='Markdown')

    @dp.message_handler(Text(equals='Отмена'))
    async def cancel_registration(message: types.Message, state: FSMContext):
        current_state = await state.get_state()
        if current_state is not None:
            await state.finish()
            await message.answer('Действие отменено.', reply_markup=get_start_keyboard())
        else:
            await message.answer('Нет активного действия для отмены.')

# --- Запуск с автоматическим сбросом вебхука и очисткой БД ---
async def on_startup(dp):
    # 1. Удаляем старый вебхук
    await bot.delete_webhook()
    logging.info("⏹️ Old webhook deleted")
    
    # 2. Пауза для Telegram
    await asyncio.sleep(2)
    
    # 3. Очищаем базу данных (заявки, трекеры, геолокации)
    await clear_db()
    logging.info("🗑️ Database cleared")
    
    # 4. Создаём таблицы (если их нет)
    await init_db()
    
    # 5. Устанавливаем новый вебхук
    webhook_url = os.getenv('RENDER_EXTERNAL_URL') + '/webhook'
    await bot.set_webhook(webhook_url)
    logging.info(f"✅ Webhook set to {webhook_url}")

async def on_shutdown(dp):
    await bot.delete_webhook()
    await dp.storage.close()
    await dp.storage.wait_closed()
    await bot.close()
    logging.info("🤖 Bot stopped")

if __name__ == '__main__':
    register_handlers(dp)
    start_webhook(
        dispatcher=dp,
        webhook_path='/webhook',
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        host='0.0.0.0',
        port=int(os.environ.get("PORT", 10000))
    )
