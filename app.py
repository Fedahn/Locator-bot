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
ADMIN_IDS = [994960688]  # Ваш Telegram ID

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

# --- База данных (PostgreSQL) ---
async def init_db():
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
    logging.info("Database initialized")

async def get_connection():
    return await asyncpg.connect(DATABASE_URL)

def get_start_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton('▶️ Начать'))
    return kb

def get_employee_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton('🙋‍♂️ Я еду'))
    kb.add(KeyboardButton('👀 Где водитель?'))
    kb.add(KeyboardButton('❌ Отменить мою заявку'))
    return kb

def get_driver_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton('📍 Отправить мою геолокацию'))
    kb.add(KeyboardButton('📋 Список заявок'))
    kb.add(KeyboardButton('📊 Кто едет?'))
    kb.add(KeyboardButton('🟢 Включить Live Location'))
    kb.add(KeyboardButton('🔴 Остановить трансляцию'))
    return kb

def get_admin_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton('🗑️ Очистить все заявки'))
    kb.add(KeyboardButton('👥 Очистить всех пользователей'))
    kb.add(KeyboardButton('🗄️ Очистить всё (кроме пользователей)'))
    kb.add(KeyboardButton('📊 Статистика БД'))
    kb.add(KeyboardButton('➕ Добавить тестовую заявку'))
    kb.add(KeyboardButton('🔙 Выйти из админ-панели'))
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
    logging.info(f"Tracker deactivated")

async def auto_expire_tracker(user_id: int, expire_time: datetime):
    delay = (expire_time - datetime.now()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    await deactivate_tracker(user_id, stop_live=True)
    await bot.send_message(user_id, "⏰ 2-часовой локатор истёк. Заявки сброшены.", reply_markup=get_driver_keyboard())
    await bot.send_message(GROUP_CHAT_ID, "⏰ Трансляция местоположения завершена. Заявки сброшены.")

# --- Хендлеры ---
def register_handlers(dp: Dispatcher):
    temp_user_data = {}

    # --- Админ-панель ---
    @dp.message_handler(commands=['admin'])
    async def admin_panel(message: types.Message):
        user_id = message.from_user.id
        if user_id not in ADMIN_IDS:
            await message.answer('⛔ У вас нет доступа к админ-панели.')
            return
        await message.answer('🔐 *Админ-панель*\n\nВыберите действие:', parse_mode='Markdown', reply_markup=get_admin_keyboard())

    @dp.message_handler(Text(equals='🗑️ Очистить все заявки'))
    async def admin_clear_requests(message: types.Message):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer('⛔ Доступ запрещён.')
            return
        conn = await get_connection()
        await conn.execute("DELETE FROM ride_requests")
        await conn.close()
        await message.answer('✅ Все заявки удалены.', reply_markup=get_admin_keyboard())

    @dp.message_handler(Text(equals='👥 Очистить всех пользователей'))
    async def admin_clear_users(message: types.Message):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer('⛔ Доступ запрещён.')
            return
        conn = await get_connection()
        await conn.execute("DELETE FROM users")
        await conn.execute("DELETE FROM ride_requests")
        await conn.execute("DELETE FROM driver_locations")
        await conn.execute("DELETE FROM driver_tracker")
        await conn.close()
        await message.answer('✅ Все пользователи и связанные данные удалены.', reply_markup=get_admin_keyboard())

    @dp.message_handler(Text(equals='🗄️ Очистить всё (кроме пользователей)'))
    async def admin_clear_all(message: types.Message):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer('⛔ Доступ запрещён.')
            return
        conn = await get_connection()
        await conn.execute("DELETE FROM ride_requests")
        await conn.execute("DELETE FROM driver_locations")
        await conn.execute("DELETE FROM driver_tracker")
        await conn.close()
        await message.answer('✅ Данные очищены, пользователи сохранены.', reply_markup=get_admin_keyboard())

    @dp.message_handler(Text(equals='➕ Добавить тестовую заявку'))
    async def admin_add_test_request(message: types.Message):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer('⛔ Доступ запрещён.')
            return
        conn = await get_connection()
        await conn.execute("INSERT INTO ride_requests (user_id, pickup_point, status) VALUES ($1, $2, $3)",
                          994960688, 'Автобаза', 'active')
        await conn.close()
        await message.answer('✅ Тестовая заявка на точку "Автобаза" добавлена!', reply_markup=get_admin_keyboard())

    @dp.message_handler(Text(equals='📊 Статистика БД'))
    async def admin_stats(message: types.Message):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer('⛔ Доступ запрещён.')
            return
        conn = await get_connection()
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        active_requests = await conn.fetchval("SELECT COUNT(*) FROM ride_requests WHERE status = 'active'")
        total_requests = await conn.fetchval("SELECT COUNT(*) FROM ride_requests")
        active_tracker = await conn.fetchval("SELECT COUNT(*) FROM driver_tracker WHERE is_active = 1")
        await conn.close()
        await message.answer(
            f"📊 *Статистика БД*\n\n👥 Пользователей: {users_count}\n📝 Активных заявок: {active_requests}\n📋 Всего заявок: {total_requests}\n🚌 Активный трекер: {'Да' if active_tracker else 'Нет'}",
            parse_mode='Markdown', reply_markup=get_admin_keyboard()
        )

    @dp.message_handler(Text(equals='🔙 Выйти из админ-панели'))
    async def admin_exit(message: types.Message):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer('⛔ Доступ запрещён.')
            return
        conn = await get_connection()
        user = await conn.fetchrow("SELECT role FROM users WHERE user_id = $1", message.from_user.id)
        await conn.close()
        if user and user['role'] == 'employee':
            await message.answer('Выход из админ-панели.', reply_markup=get_employee_keyboard())
        else:
            await message.answer('Выход из админ-панели.', reply_markup=get_driver_keyboard())

    # --- Регистрация ---
    @dp.message_handler(Text(equals='▶️ Начать'))
    async def start_button(message: types.Message):
        await cmd_start(message)

    @dp.message_handler(commands=['start'])
    async def cmd_start(message: types.Message):
        user_id = message.from_user.id
        conn = await get_connection()
        user = await conn.fetchrow("SELECT role FROM users WHERE user_id = $1", user_id)
        await conn.close()
        if user:
            role = user['role']
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
        conn = await get_connection()
        await conn.execute("INSERT INTO users (user_id, telegram_username, first_name, last_name, role) VALUES ($1, $2, $3, $4, $5)",
                          user_id, username, first_name, last_name, role)
        await conn.close()
        await callback.message.edit_text('✅ Регистрация успешно завершена!')
        if role == 'employee':
            await callback.message.answer('Вы зарегистрированы как сотрудник.', reply_markup=get_employee_keyboard())
        else:
            await callback.message.answer('Вы зарегистрированы как водитель.', reply_markup=get_driver_keyboard())
        await callback.answer()

    # --- Сотрудник: Я еду ---
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
        await conn.execute("INSERT INTO ride_requests (user_id, pickup_point) VALUES ($1, $2)",
                          user_id, point)
        user = await conn.fetchrow("SELECT first_name, last_name FROM users WHERE user_id = $1", user_id)
        await conn.close()
        if user:
            user_name = f"{user['first_name']} {user['last_name']}"
            await notify_driver_about_new_request(user_name, point)
        await callback.message.edit_text(f'✅ Заявка на посадку в точке "{point}" создана!')
        await callback.answer()

    # --- Сотрудник: Где водитель? ---
    @dp.message_handler(Text(equals='👀 Где водитель?'))
    async def where_driver(message: types.Message):
        conn = await get_connection()
        loc = await conn.fetchrow("SELECT latitude, longitude FROM driver_locations ORDER BY updated_at DESC LIMIT 1")
        await conn.close()
        if loc and loc['latitude'] is not None:
            await message.answer_location(latitude=loc['latitude'], longitude=loc['longitude'])
        else:
            await message.answer('🚫 Водитель сейчас не в пути или не поделился своей геолокацией.')

    # --- Сотрудник: Отменить мою заявку ---
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

    # --- Водитель: ручная отправка геолокации ---
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
            await message.answer('✅ Ваша геолокация обновлена!', reply_markup=get_driver_keyboard())
        else:
            await message.answer('⛔ Вы не зарегистрированы как водитель.')
        await conn.close()

    # --- Водитель: включение Live Location ---
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
        await message.answer("✅ Трансляция запущена на 2 часа!", reply_markup=get_driver_keyboard())
        await bot.send_message(GROUP_CHAT_ID, "🚌 Водитель начал маршрут! Отправляйте заявки в личку боту.", parse_mode='Markdown')

    # --- Водитель: остановка трансляции ---
    @dp.message_handler(Text(equals='🔴 Остановить трансляцию'))
    async def stop_live_location(message: types.Message):
        await deactivate_tracker(message.from_user.id, stop_live=True)
        await message.answer("🔴 Трансляция остановлена. Все заявки сброшены.", reply_markup=get_driver_keyboard())

    # --- Водитель: список заявок ---
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

    # --- Водитель: статистика ---
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

    # --- Отмена при регистрации ---
    @dp.message_handler(Text(equals='Отмена'))
    async def cancel_registration(message: types.Message, state: FSMContext):
        current_state = await state.get_state()
        if current_state is not None:
            await state.finish()
            await message.answer('Действие отменено.', reply_markup=get_start_keyboard())
        else:
            await message.answer('Нет активного действия для отмены.')

# --- Запуск ---
async def on_startup(dp):
    await init_db()
    webhook_url = os.getenv('RENDER_EXTERNAL_URL') + '/webhook'
    await bot.set_webhook(webhook_url)
    logging.info(f"Webhook set to {webhook_url}")

async def on_shutdown(dp):
    await bot.delete_webhook()
    await dp.storage.close()
    await dp.storage.wait_closed()

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
