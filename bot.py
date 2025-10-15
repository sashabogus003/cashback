"""
Телеграм-бот для кешбека (ник + скрины депозитов/выводов)
Совместим с aiogram >= 3.7 (parse_mode через DefaultBotProperties)

В этом варианте:
- Больше эмодзи в сообщениях.
- «⬅️ Назад» на каждом этапе (ник → депозиты → выводы → подтверждение).
- На подтверждении есть «📤 Отправить сейчас» (альтернатива «подтверждаю»).
- Убрана кнопка «Нет выводов» — скрин вкладки «Выводы» обязателен.
- Если юзер отвечает медиа по тикету — админам уходит само медиа (фото/док) + подпись.
- Лимит 3 активные заявки на пользователя.
- Помощь (/help) без HTML-ошибок.

Запуск:
1) pip install -U "aiogram>=3.7" aiosqlite python-dotenv
2) Рядом с файлом .env:
   BOT_TOKEN=123:ABC...
   ADMINS=11111111
   ADMIN_GROUP_ID=
3) python bot.py (при ошибке окно не закроется — попросит нажать Enter)
"""

import asyncio
import os
import textwrap
import logging
from datetime import datetime
from typing import Optional, List, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ContentType, InputMediaPhoto, InputMediaDocument
)
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from pathlib import Path

# ================== CONFIG ==================
load_dotenv(dotenv_path=Path(__file__).with_name('.env'))
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Put it in .env next to bot.py")

ADMINS = {int(x.strip()) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0")) or None

# ================== DB LAYER ==================
DB_PATH = "bot.db"

CREATE_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS referrers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    title TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    referrer_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(referrer_id) REFERENCES referrers(id)
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL,
    referrer_id INTEGER,
    email TEXT,              -- поле оставлено на будущее, сейчас не используется
    project_nick TEXT,
    status TEXT NOT NULL DEFAULT 'new', -- new|needs_info|approved|rejected|paid
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(referrer_id) REFERENCES referrers(id)
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    kind TEXT, -- deposit_photo|deposit_doc|withdraw_photo|withdraw_doc
    file_id TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    sender TEXT NOT NULL, -- user|admin
    text TEXT,
    file_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def get_or_create_user(tg_id: int, username: Optional[str], ref_code: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, referrer_id FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row:
            user_id, _ = row
            await db.execute("UPDATE users SET username=? WHERE id=?", (username, user_id))
            await db.commit()
            return user_id
        cur = await db.execute(
            "INSERT INTO users (tg_id, username, referrer_id) VALUES (?,?,NULL)",
            (tg_id, username)
        )
        await db.commit()
        return cur.lastrowid

async def gen_ticket_code(db: aiosqlite.Connection) -> str:
    today = datetime.now().strftime("%Y%m%d")
    cur = await db.execute("SELECT COUNT(*) FROM tickets WHERE substr(code,5,8)=?", (today,))
    (cnt,) = await cur.fetchone()
    return f"TCK-{today}-{cnt+1:04d}"

async def create_ticket(user_tg_id: int, data: dict) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, referrer_id FROM users WHERE tg_id=?", (user_tg_id,))
        row = await cur.fetchone()
        if not row:
            raise RuntimeError("User not found while creating ticket")
        user_id, referrer_id = row
        code = await gen_ticket_code(db)
        cur = await db.execute(
            """
            INSERT INTO tickets (code, user_id, referrer_id, email, project_nick, status)
            VALUES (?,?,?,?,?, 'new')
            """,
            (code, user_id, referrer_id, None, data.get('project_nick'))
        )
        ticket_id = cur.lastrowid
        for att in data.get('attachments', []):
            await db.execute(
                "INSERT INTO attachments (ticket_id, kind, file_id) VALUES (?,?,?)",
                (ticket_id, att['kind'], att['file_id'])
            )
        await db.commit()
        return code

async def find_ticket_by_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, referrer_id, status, email, project_nick, created_at FROM tickets WHERE code=?",
            (code,)
        )
        return await cur.fetchone()

async def list_user_tickets(tg_id: int) -> List[Tuple[str,str,Optional[str],Optional[str],str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT t.code, t.status, t.email, t.project_nick, t.created_at
            FROM tickets t
            JOIN users u ON u.id=t.user_id
            WHERE u.tg_id=?
            ORDER BY t.id DESC
            """,
            (tg_id,)
        )
        return await cur.fetchall()

async def update_ticket_status(code: str, new_status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET status=?, updated_at=datetime('now') WHERE code=?",
            (new_status, code)
        )
        await db.commit()

async def ticket_id_from_code(code: str) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM tickets WHERE code=?", (code,))
        row = await cur.fetchone()
        return row[0] if row else None

async def add_message(ticket_id: int, sender: str, text: Optional[str], file_id: Optional[str]=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (ticket_id, sender, text, file_id) VALUES (?,?,?,?)",
            (ticket_id, sender, text, file_id)
        )
        await db.commit()

# ================== BOT LAYER ==================

# --- Anti-spam settings ---
MAX_ACTIVE_TICKETS = 3
ACTIVE_STATUSES = ("new", "needs_info", "approved")

async def count_active_tickets(tg_id: int) -> int:
    q = ",".join(["?"] * len(ACTIVE_STATUSES))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"SELECT COUNT(*) FROM tickets t JOIN users u ON u.id=t.user_id WHERE u.tg_id=? AND t.status IN ({q})",
            (tg_id, *ACTIVE_STATUSES)
        )
        (cnt,) = await cur.fetchone()
        return cnt

async def list_active_user_tickets(tg_id: int) -> List[Tuple[str,str,str]]:
    q = ",".join(["?"] * len(ACTIVE_STATUSES))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"""
            SELECT t.code, t.status, t.created_at
            FROM tickets t JOIN users u ON u.id=t.user_id
            WHERE u.tg_id=? AND t.status IN ({q})
            ORDER BY t.id DESC
            """,
            (tg_id, *ACTIVE_STATUSES)
        )
        return await cur.fetchall()

# aiogram >=3.7: parse_mode через default properties
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

# ---- States (FSM) ----
class CashbackForm(StatesGroup):
    project_nick = State()
    dep_attachments = State()
    wd_attachments = State()
    confirm = State()

class AdminReply(StatesGroup):
    wait_text = State()

class AdminReject(StatesGroup):
    wait_reason = State()

# ---- Keyboards ----

STATUS_EMOJI = {
    'new': '🟡',
    'needs_info': '🟠',
    'approved': '🟢',
    'rejected': '🔴',
    'paid': '💎',
}

def kb_main_user():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Подать заявку на кешбек", callback_data="cb:new")],
        [InlineKeyboardButton(text="🧾 Мои заявки", callback_data="cb:list")]
    ])

def kb_admin_ticket(code: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm:{code}:approve"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm:{code}:reject")
        ],
        [
            InlineKeyboardButton(text="ℹ️ Запросить инфо", callback_data=f"adm:{code}:needinfo"),
            InlineKeyboardButton(text="💬 Ответить", callback_data=f"adm:{code}:reply")
        ],
        [
            InlineKeyboardButton(text="📎 Скриншоты", callback_data=f"adm:{code}:files")
        ]
    ])

def kb_nick_stage() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:home"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

def kb_done(stage: str) -> InlineKeyboardMarkup:
    # stage: 'dep' | 'wd'
    row1 = [InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{stage}")]
    row2 = [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back:{'nick' if stage=='dep' else 'dep'}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2])

def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Отправить сейчас", callback_data="confirm:send")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:wd"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

# ---- Helpers ----

def fmt_ticket_brief(code: str, status: str, email: Optional[str], project_nick: Optional[str], created_at: str) -> str:
    icon = STATUS_EMOJI.get(status, '📌')
    parts = [f"{icon} <b>{code}</b> — {status.upper()}"]
    if project_nick:
        parts.append(f"🎮 Ник: <code>{project_nick}</code>")
    parts.append(f"🗓 Создан: {created_at}")
    return "\n".join(parts)

async def notify_admins(text: str, markup: Optional[InlineKeyboardMarkup]=None):
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception:
            pass
    if ADMIN_GROUP_ID:
        try:
            await bot.send_message(ADMIN_GROUP_ID, text, reply_markup=markup)
        except Exception:
            pass

def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i+size]

async def send_ticket_attachments(chat_id: int, code: str):
    t = await find_ticket_by_code(code)
    if not t:
        await bot.send_message(chat_id, "⚠️ Тикет не найден.")
        return
    ticket_id = await ticket_id_from_code(code)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT kind, file_id FROM attachments WHERE ticket_id=? ORDER BY id", (ticket_id,))
        rows = await cur.fetchall()
    if not rows:
        await bot.send_message(chat_id, f"По {code} вложений нет.")
        return

    dep_files, wd_files = [], []
    for kind, fid in rows:
        if kind.startswith("deposit_"):
            dep_files.append((kind, fid))
        elif kind.startswith("withdraw_"):
            wd_files.append((kind, fid))
        else:
            dep_files.append((kind, fid))

    async def send_group(title: str, files: List[tuple]):
        if not files:
            return
        total = len(files)
        first_caption = f"{title} для {code} (всего {total})"
        for idx, chunk in enumerate(chunked(files, 10)):
            if len(chunk) == 1:
                kind, fid = chunk[0]
                caption = first_caption if idx == 0 else None
                if kind.endswith("photo"):
                    await bot.send_photo(chat_id, fid, caption=caption)
                else:
                    await bot.send_document(chat_id, fid, caption=caption)
            else:
                media = []
                for j, (kind, fid) in enumerate(chunk):
                    cap = first_caption if idx == 0 and j == 0 else None
                    if kind.endswith("photo"):
                        media.append(InputMediaPhoto(media=fid, caption=cap))
                    else:
                        media.append(InputMediaDocument(media=fid, caption=cap))
                await bot.send_media_group(chat_id, media=media)

    await send_group("💳 Скрины депозитов", dep_files)
    await send_group("📤 Скрины выводов", wd_files)

# ---- Handlers ----

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await get_or_create_user(m.from_user.id, m.from_user.username, None)
    await m.answer(
        "🎰💸 Добро пожаловать! Здесь ты можешь получить кешбек по депозитам.\nВыбери действие ниже:",
        reply_markup=kb_main_user()
    )

@router.callback_query(F.data == "cb:new")
async def cb_new_ticket(cq: CallbackQuery, state: FSMContext):
    # анти-спам
    active_cnt = await count_active_tickets(cq.from_user.id)
    if active_cnt >= MAX_ACTIVE_TICKETS:
        active = await list_active_user_tickets(cq.from_user.id)
        lines = ["❗ У тебя уже есть активные заявки (максимум 3):"]
        for code, st, created in active:
            lines.append(f"— <code>{code}</code> · {st} · {created}")
        lines.append("Когда одна из них закроется (paid/rejected) — можно будет создать новую.")
        await cq.message.answer("\n".join(lines))
        await cq.answer()
        return
    await state.set_state(CashbackForm.project_nick)
    await cq.message.answer("✍️ Напиши <b>ник в казино</b> (на проекте):", reply_markup=kb_nick_stage())
    await cq.answer()

@router.callback_query(F.data == "back:home")
async def back_home(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.answer("🏠 Главное меню:", reply_markup=kb_main_user())
    await cq.answer()

@router.message(CashbackForm.project_nick)
async def form_nick(m: Message, state: FSMContext):
    nick = (m.text or '').strip()
    if not nick:
        await m.answer("Нужен ник в казино — напиши его текстом.", reply_markup=kb_nick_stage())
        return
    await state.update_data(project_nick=nick)
    await state.set_state(CashbackForm.dep_attachments)
    await m.answer(
        "💳 Пришли скриншоты <b>депозитов</b> (фото или файл). Можно несколько.\nКогда закончишь — нажми <b>✅ Готово</b>.",
        reply_markup=kb_done('dep')
    )

# --- Deposits ---
@router.message(CashbackForm.dep_attachments, F.content_type.in_({ContentType.PHOTO, ContentType.DOCUMENT}))
async def dep_attach_media(m: Message, state: FSMContext):
    data = await state.get_data()
    atts = data.get('dep_atts', [])
    if m.content_type == ContentType.PHOTO:
        atts.append({"kind": "deposit_photo", "file_id": m.photo[-1].file_id})
    else:
        atts.append({"kind": "deposit_doc", "file_id": m.document.file_id})
    await state.update_data(dep_atts=atts)
    await m.answer(f"✅ Принято! Депозитных скринов: <b>{len(atts)}</b>.\nМожно добавить ещё или жми <b>✅ Готово</b>.", reply_markup=kb_done('dep'))

@router.message(CashbackForm.dep_attachments)
async def dep_attach_done_text(m: Message, state: FSMContext):
    if m.text and m.text.strip().lower() in {"готово", "done", "ок", "ok"}:
        await _go_to_withdraw_stage(m.chat.id, state)
    else:
        await m.answer("Если закончил с депозитами — нажми <b>✅ Готово</b>.", reply_markup=kb_done('dep'))

@router.callback_query(F.data == "back:nick")
async def back_to_nick(cq: CallbackQuery, state: FSMContext):
    await state.set_state(CashbackForm.project_nick)
    await cq.message.answer("✍️ Измени или введи снова ник:", reply_markup=kb_nick_stage())
    await cq.answer()

@router.callback_query(F.data == "done:dep")
async def dep_done_btn(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    dep_n = len(data.get('dep_atts', []))
    if dep_n == 0:
        await cq.answer("Нужен хотя бы один скрин депозита", show_alert=True)
        return
    await _go_to_withdraw_stage(cq.from_user.id, state)
    await cq.answer()

async def _go_to_withdraw_stage(chat_id: int, state: FSMContext):
    await state.set_state(CashbackForm.wd_attachments)
    try:
        await bot.send_message(
            chat_id,
            "📤 Теперь пришли <b>скрин(ы) вкладки «Выводы»</b> — даже если она пустая. Можно несколько.\n"
            "Когда загрузишь — жми <b>✅ Готово</b>.",
            reply_markup=kb_done('wd')
        )
    except Exception:
        pass

# --- Withdrawals ---
@router.message(CashbackForm.wd_attachments, F.content_type.in_({ContentType.PHOTO, ContentType.DOCUMENT}))
async def wd_attach_media(m: Message, state: FSMContext):
    data = await state.get_data()
    atts = data.get('wd_atts', [])
    if m.content_type == ContentType.PHOTO:
        atts.append({"kind": "withdraw_photo", "file_id": m.photo[-1].file_id})
    else:
        atts.append({"kind": "withdraw_doc", "file_id": m.document.file_id})
    await state.update_data(wd_atts=atts)
    await m.answer(f"✅ Принято! Скринов выводов: <b>{len(atts)}</b>.\nМожно добавить ещё или жми <b>✅ Готово</b>.", reply_markup=kb_done('wd'))

@router.message(CashbackForm.wd_attachments)
async def wd_attach_done_text(m: Message, state: FSMContext):
    if m.text and m.text.strip().lower() in {"готово", "done", "ок", "ok"}:
        data = await state.get_data()
        wd_n = len(data.get('wd_atts', []))
        if wd_n == 0:
            await m.answer("⚠️ Нужен как минимум <b>один</b> скрин из раздела «Выводы». Пришли его и затем жми <b>✅ Готово</b>.",
                           reply_markup=kb_done('wd'))
            return
        await _show_summary_and_confirm(m)
    else:
        await m.answer("Если закончил с выводами — нажми <b>✅ Готово</b>.", reply_markup=kb_done('wd'))

@router.callback_query(F.data == "back:dep")
async def back_to_dep(cq: CallbackQuery, state: FSMContext):
    await state.set_state(CashbackForm.dep_attachments)
    await cq.message.answer("💳 Вернулись к депозитам. Пришли скрины и жми <b>✅ Готово</b>.", reply_markup=kb_done('dep'))
    await cq.answer()

@router.callback_query(F.data == "done:wd")
async def wd_done_btn(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    wd_n = len(data.get('wd_atts', []))
    if wd_n == 0:
        await cq.answer("Нужен хотя бы один скрин вкладки «Выводы».", show_alert=True)
        return
    await _show_summary_and_confirm(cq.message)
    await cq.answer()

@router.callback_query(F.data == "cancel")
async def cancel_flow(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.answer("🚫 Заявка отменена. Если передумаешь — нажми «🎁 Подать заявку на кешбек».", reply_markup=kb_main_user())
    await cq.answer()

async def _show_summary_and_confirm(m_or_msg: Message):
    state = dp.fsm.get_context(bot=bot, user_id=m_or_msg.chat.id, chat_id=m_or_msg.chat.id)
    data = await state.get_data()
    dep_n = len(data.get('dep_atts', []))
    wd_n = len(data.get('wd_atts', []))
    await state.set_state(CashbackForm.confirm)
    summary = textwrap.dedent(f"""
    🔎 Проверь заявку:
    🎮 Ник: <b>{data.get('project_nick')}</b>
    💳 Скриншотов депозитов: <b>{dep_n}</b>
    📤 Скриншотов выводов: <b>{wd_n}</b>

    Если всё верно — нажми кнопку <b>📤 Отправить сейчас</b> или напиши <b>подтверждаю</b>.
    """)
    await m_or_msg.answer(summary, reply_markup=kb_confirm())

async def _finalize_submission(user_id: int, chat_id: int, state: FSMContext, username: Optional[str]):
    # Повторная антиспам-проверка
    if await count_active_tickets(user_id) >= MAX_ACTIVE_TICKETS:
        active = await list_active_user_tickets(user_id)
        lines = ["❗ У тебя уже есть активные заявки (максимум 3):"]
        for code2, st2, created2 in active:
            lines.append(f"— <code>{code2}</code> · {st2} · {created2}")
        await bot.send_message(chat_id, "\n".join(lines))
        await state.clear()
        return

    data = await state.get_data()
    attachments = []
    attachments.extend(data.get('dep_atts', []))
    attachments.extend(data.get('wd_atts', []))
    payload = {
        'project_nick': data.get('project_nick'),
        'attachments': attachments,
    }
    code = await create_ticket(user_id, payload)
    await state.clear()
    await bot.send_message(chat_id, f"✅ Заявка создана! Номер: <b>{code}</b>\nМы свяжемся с тобой при необходимости.")

    # уведомление админам
    t = await find_ticket_by_code(code)
    _, _user_id, _referrer_id, status, email, project_nick, created_at = t
    brief = fmt_ticket_brief(code, status, email, project_nick, created_at)
    dep_n = len([a for a in attachments if a['kind'].startswith('deposit_')])
    wd_n = len([a for a in attachments if a['kind'].startswith('withdraw_')])
    await notify_admins(
        textwrap.dedent(f"""
        🆕 Новая заявка на кешбек
        {brief}
        💳 Депозиты: {dep_n} | 📤 Выводы: {wd_n}
        👤 Пользователь: @{username or user_id}
        """),
        markup=kb_admin_ticket(code)
    )

@router.message(CashbackForm.confirm)
async def form_confirm(m: Message, state: FSMContext):
    if m.text and m.text.strip().lower() == "подтверждаю":
        await _finalize_submission(m.from_user.id, m.chat.id, state, m.from_user.username)
    else:
        await m.answer("Чтобы подтвердить — нажми <b>📤 Отправить сейчас</b> или напиши <b>подтверждаю</b>.", reply_markup=kb_confirm())

@router.callback_query(F.data == "confirm:send")
async def confirm_send(cq: CallbackQuery, state: FSMContext):
    await _finalize_submission(cq.from_user.id, cq.message.chat.id, state, cq.from_user.username)
    await cq.answer()

@router.callback_query(F.data == "back:wd")
async def back_to_wd(cq: CallbackQuery, state: FSMContext):
    await state.set_state(CashbackForm.wd_attachments)
    await cq.message.answer("📤 Вернулись к шагу с выводами. Пришли скрины и нажми <b>✅ Готово</b>.", reply_markup=kb_done('wd'))
    await cq.answer()

@router.callback_query(F.data == "cb:list")
async def cb_list_my(cq: CallbackQuery):
    rows = await list_user_tickets(cq.from_user.id)
    if not rows:
        await cq.message.answer("🧾 У тебя пока нет заявок. Нажми «🎁 Подать заявку».")
    else:
        txt = ["<b>🧾 Мои заявки:</b>"]
        for code, status, email, nick, created in rows:
            txt.append("— " + fmt_ticket_brief(code, status, email, nick, created))
        await cq.message.answer("\n".join(txt))
    await cq.answer()

# ---- Admin commands ----

@router.message(Command("cashback"))
async def cmd_cashback(m: Message, state: FSMContext):
    await get_or_create_user(m.from_user.id, m.from_user.username, None)
    if await count_active_tickets(m.from_user.id) >= MAX_ACTIVE_TICKETS:
        active = await list_active_user_tickets(m.from_user.id)
        lines = ["❗ У тебя уже есть активные заявки (максимум 3):"]
        for code, st, created in active:
            lines.append(f"— <code>{code}</code> · {st} · {created}")
        lines.append("Когда одна из них закроется (paid/rejected) — можно будет создать новую.")
        await m.answer("\n".join(lines))
        return
    await state.set_state(CashbackForm.project_nick)
    await m.answer("🚀 Начнём заявку. Напиши ник в казино:", reply_markup=kb_nick_stage())

@router.message(Command("ticket"))
async def cmd_ticket(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: /ticket TCK-YYYYMMDD-####")
        return
    code = parts[1].strip().upper()
    t = await find_ticket_by_code(code)
    if not t:
        await m.answer("Тикет не найден")
        return
    _, user_id, referrer_id, status, email, project_nick, created_at = t
    await m.answer(
        textwrap.dedent(f"""
        {fmt_ticket_brief(code, status, email, project_nick, created_at)}
        """),
        reply_markup=kb_admin_ticket(code)
    )

@router.message(Command("files"))
async def cmd_files(m: Message):
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: /files TCK-YYYYMMDD-####")
        return
    code = parts[1].strip().upper()
    await send_ticket_attachments(m.chat.id, code)

@router.message(Command("tickets"))
async def cmd_tickets(m: Message):
    if not is_admin(m.from_user.id):
        return
    args = (m.text or "").split()[1:]
    status = args[0].lower() if args else "pending"
    allowed = {"pending", "all", "new", "needs_info", "approved", "rejected", "paid"}
    if status not in allowed:
        status = "pending"

    limit = 10
    offset = 0

    if status == "pending":
        where_sql = "WHERE t.status IN ('new','needs_info')"
        params = []
    elif status == "all":
        where_sql = ""
        params = []
    else:
        where_sql = "WHERE t.status = ?"
        params = [status]

    sql = f"""
      SELECT t.code, t.status, t.email, t.project_nick, t.created_at
      FROM tickets t
      {where_sql}
      ORDER BY t.id DESC
      LIMIT ? OFFSET ?
    """

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params + [limit, offset])
        rows = await cur.fetchall()

    if not rows:
        await m.answer(
            "Нет заявок в ожидании (new/needs_info)." if status == "pending"
            else f"Тикетов со статусом {status} нет."
        )
        return

    header = "в ожидании" if status == "pending" else status
    lines = [f"<b>📚 Заявки ({header})</b>"]
    for i, (code, st, _email, nick, created) in enumerate(rows, 1):
        icon = STATUS_EMOJI.get(st, '📌')
        lines.append(f"{i}. {icon} <code>{code}</code> — {st.upper()} | {nick or '-'} | {created}")

    buttons = []
    if len(rows) == limit:
        buttons.append([InlineKeyboardButton(text="▶️ Дальше", callback_data=f"adm_list:{status}:{offset + limit}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await m.answer("\n".join(lines), reply_markup=kb)

@router.callback_query(F.data.startswith("adm_list:"))
async def adm_list_page(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Только для админов", show_alert=True)
        return

    _, status, offset_str = cq.data.split(":", 2)
    allowed = {"pending", "all", "new", "needs_info", "approved", "rejected", "paid"}
    if status not in allowed:
        status = "pending"
    try:
        offset = int(offset_str)
    except Exception:
        offset = 0

    limit = 10

    if status == "pending":
        where_sql = "WHERE t.status IN ('new','needs_info')"
        params = []
    elif status == "all":
        where_sql = ""
        params = []
    else:
        where_sql = "WHERE t.status = ?"
        params = [status]

    sql = f"""
      SELECT t.code, t.status, t.email, t.project_nick, t.created_at
      FROM tickets t
      {where_sql}
      ORDER BY t.id DESC
      LIMIT ? OFFSET ?
    """

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params + [limit, offset])
        rows = await cur.fetchall()

    if not rows:
        await cq.answer("Больше нет", show_alert=False)
        return

    header = "в ожидании" if status == "pending" else status
    lines = [f"<b>📚 Заявки ({header})</b>"]
    for i, (code, st, _email, nick, created) in enumerate(rows, 1):
        icon = STATUS_EMOJI.get(st, '📌')
        lines.append(f"{i}. {icon} <code>{code}</code> — {st.upper()} | {nick or '-'} | {created}")

    buttons = []
    row = []
    if offset > 0:
        row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_list:{status}:{max(0, offset - limit)}"))
    if len(rows) == limit:
        row.append(InlineKeyboardButton(text="▶️ Дальше", callback_data=f"adm_list:{status}:{offset + limit}"))
    if row:
        buttons.append(row)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    await cq.message.edit_text("\n".join(lines))
    await cq.message.edit_reply_markup(reply_markup=kb)
    await cq.answer()

@router.message(Command("help"))
async def cmd_help(m: Message):
    if is_admin(m.from_user.id):
        await m.answer(textwrap.dedent(
            """
            <b>🛠 Админ-команды</b>
            /tickets — заявки <i>в ожидании</i> (new+needs_info)
            /tickets new — только NEW
            /tickets needs_info — только NEEDS_INFO
            /tickets approved — одобренные
            /tickets rejected — отклонённые
            /tickets paid — выплаченные
            /tickets all — все подряд
            /ticket <code>КОД</code> — открыть конкретный тикет
            /files <code>КОД</code> — показать вложения тикета
            """
        ))
    else:
        await m.answer("🪙 /cashback — подать заявку на кешбек. Кнопка «🧾 Мои заявки» показывает статус.")

# ==== Admin actions (buttons) ====

@router.callback_query(F.data.startswith("adm:"))
async def admin_actions(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("Только для админов", show_alert=True)
        return
    _, code, action = cq.data.split(":", 2)

    t = await find_ticket_by_code(code)
    if not t:
        await cq.answer("Тикет не найден", show_alert=True)
        return
    ticket_row = t
    ticket_id = await ticket_id_from_code(code)

    if action == "approve":
        await update_ticket_status(code, "approved")
        await add_message(ticket_id, "admin", "Заявка одобрена")
        # Уведомим пользователя
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT tg_id FROM users WHERE id=?", (ticket_row[1],))
            user_tg_id = (await cur.fetchone())[0]
        try:
            await bot.send_message(user_tg_id, f"🟢 Твоя заявка <b>{code}</b> одобрена. Ожидай начисления кешбека.")
        except Exception:
            pass
        await cq.message.edit_reply_markup(reply_markup=kb_admin_ticket(code))
        await cq.answer("Одобрено")

    elif action == "reject":
        await state.set_state(AdminReject.wait_reason)
        await state.update_data(ticket_code=code)
        await cq.message.answer("Укажи <b>причину отклонения</b> (одним сообщением). Это увидит пользователь.")
        await cq.answer()

    elif action == "needinfo":
        await update_ticket_status(code, "needs_info")
        await state.set_state(AdminReply.wait_text)
        await state.update_data(ticket_code=code)
        await cq.message.answer("Напиши текст, который отправим пользователю (запрос доп. инфо).")
        await cq.answer()

    elif action == "reply":
        await state.set_state(AdminReply.wait_text)
        await state.update_data(ticket_code=code)
        await cq.message.answer("Введи сообщение пользователю по этому тикету.")
        await cq.answer()

    elif action == "files":
        await send_ticket_attachments(cq.from_user.id, code)
        await cq.answer()

@router.message(AdminReply.wait_text)
async def admin_send_text(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    code = data.get("ticket_code")
    if not code:
        await m.answer("Ошибка состояния. Попробуй снова из кнопки.")
        await state.clear()
        return
    t = await find_ticket_by_code(code)
    if not t:
        await m.answer("Тикет не найден")
        await state.clear()
        return
    ticket_id = await ticket_id_from_code(code)

    # tg_id пользователя
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE id=?", (t[1],))
        user_tg_id = (await cur.fetchone())[0]

    try:
        await bot.send_message(user_tg_id, f"💬 Сообщение по заявке <b>{code}</b>:\n{m.text}")
        await add_message(ticket_id, "admin", m.text)
        await m.answer("✅ Сообщение отправлено пользователю.")
    except Exception as e:
        await m.answer(f"Не удалось отправить сообщение: {e}")
    finally:
        await state.clear()

@router.message(AdminReject.wait_reason)
async def admin_reject_with_reason(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    code = data.get("ticket_code")
    if not code:
        await m.answer("Ошибка состояния. Попробуй снова из кнопки.")
        await state.clear()
        return
    t = await find_ticket_by_code(code)
    if not t:
        await m.answer("Тикет не найден")
        await state.clear()
        return
    ticket_id = await ticket_id_from_code(code)

    # tg_id пользователя
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE id=?", (t[1],))
        user_tg_id = (await cur.fetchone())[0]

    reason = (m.text or '').strip() or "Причина не указана"

    await update_ticket_status(code, "rejected")
    await add_message(ticket_id, "admin", f"Отклонено. Причина: {reason}")
    try:
        await bot.send_message(user_tg_id, f"🔴 Твоя заявка <b>{code}</b> отклонена.\nПричина: {reason}")
    except Exception:
        pass
    await m.answer("Готово: тикет отклонён, причина отправлена пользователю.")
    await state.clear()

# Пользователь может отвечать в ЛС боту — сохраняем и реально пересылаем медиа админам
@router.message()
async def catch_user_messages(m: Message):
    if is_admin(m.from_user.id):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT t.id, t.code
            FROM tickets t JOIN users u ON u.id=t.user_id
            WHERE u.tg_id=? AND t.status IN ('new','needs_info')
            ORDER BY t.id DESC LIMIT 1
            """,
            (m.from_user.id,)
        )
        row = await cur.fetchone()

    if not row:
        return

    ticket_id, code = row

    # текст берём из caption у медиа или обычного текста
    msg_text = m.caption or m.text
    file_id = None
    content_type = None
    if m.content_type == ContentType.PHOTO:
        file_id = m.photo[-1].file_id
        content_type = ContentType.PHOTO
    elif m.content_type == ContentType.DOCUMENT:
        file_id = m.document.file_id
        content_type = ContentType.DOCUMENT

    await add_message(ticket_id, "user", msg_text, file_id)

    header = f"📨 Ответ от пользователя по <b>{code}</b>:"
    if file_id:
        # сначала шапка текстом
        await notify_admins(f"{header}\n{msg_text or '(без подписи)'}")
        # затем само медиа всем админам (и в группу, если указана)
        for admin_id in ADMINS:
            try:
                if content_type == ContentType.PHOTO:
                    await bot.send_photo(admin_id, file_id, caption=msg_text)
                else:
                    await bot.send_document(admin_id, file_id, caption=msg_text or "")
            except Exception:
                pass
        if ADMIN_GROUP_ID:
            try:
                if content_type == ContentType.PHOTO:
                    await bot.send_photo(ADMIN_GROUP_ID, file_id, caption=msg_text)
                else:
                    await bot.send_document(ADMIN_GROUP_ID, file_id, caption=msg_text or "")
            except Exception:
                pass
    else:
        await notify_admins(f"{header}\n{msg_text or '(без текста)'}")

# ================== ENTRYPOINT ==================

async def main():
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
    await init_db()
    dp.include_router(router)
    print("Bot is up.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
    except Exception:
        logging.exception("Fatal error while running bot")
        try:
            input("\nПроизошла ошибка. Нажми Enter, чтобы закрыть окно...")
        except Exception:
            pass
