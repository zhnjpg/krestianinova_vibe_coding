import sqlite3
import logging
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

# ============ CONFIG ============

import os
from dotenv import load_dotenv

load_dotenv()  # reads .env into environment variables

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

# Comma-separated list → list of ints
_raw_admins = os.environ.get("ADMIN_TELEGRAM_IDS", "").strip()
ADMIN_TELEGRAM_IDS = (
    [int(x.strip()) for x in _raw_admins.split(",") if x.strip()]
    if _raw_admins else []
)

DB_PATH = os.environ.get("DB_PATH", "manicure.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
(
    CHOOSE_DATE, CHOOSE_SLOT, CHOOSE_PROCEDURE, ENTER_COMMENT, CONFIRM,
    ADMIN_PASSWORD_STATE, ADMIN_ADD_SLOT_DATE, ADMIN_ADD_SLOT_TIME,
) = range(8)


# ============ DB HELPERS ============
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn





def get_or_create_client(tg_user):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE telegram_id = ?", (tg_user.id,))
    row = cur.fetchone()
    if not row:
        full_name = tg_user.full_name or ""
        cur.execute(
            "INSERT INTO clients (telegram_id, username, full_name) VALUES (?, ?, ?)",
            (tg_user.id, tg_user.username, full_name),
        )
        conn.commit()
        cur.execute("SELECT * FROM clients WHERE telegram_id = ?", (tg_user.id,))
        row = cur.fetchone()
    conn.close()
    return row


def is_admin(user_id):
    if not ADMIN_TELEGRAM_IDS:
        return False
    return user_id in ADMIN_TELEGRAM_IDS


# ============ KEYBOARDS ============
def main_menu_kb(is_admin_user=False):
    buttons = [
        ["📅 Записаться"],
        ["📋 Мои записи"],
    ]
    if is_admin_user:
        buttons.append(["🔐 Режим мастера"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def admin_menu_kb():
    return ReplyKeyboardMarkup(
        [
            ["📆 Записи на сегодня", "📊 Все заявки"],
            ["➕ Добавить слот", "🗓 Слоты на дату"],
            ["🚪 Выйти из режима мастера"],
        ],
        resize_keyboard=True,
    )


def dates_kb():
    today = datetime.now().date()
    buttons = []
    row = []
    for i in range(1, 15):
        d = today + timedelta(days=i)
        label = d.strftime("%d.%m (%a)")
        row.append(InlineKeyboardButton(label, callback_data=f"date_{d.isoformat()}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def slots_kb(slots):
    buttons = []
    row = []
    for s in slots:
        row.append(InlineKeyboardButton(s["slot_time"], callback_data=f"slot_{s['id']}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_dates")])
    return InlineKeyboardMarkup(buttons)


def procedures_kb():
    conn = db()
    procs = conn.execute("SELECT * FROM procedures WHERE is_active = 1").fetchall()
    conn.close()
    buttons = []
    for p in procs:
        buttons.append([
            InlineKeyboardButton(
                f"{p['name']} — {p['price']} ₽",
                callback_data=f"proc_{p['id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def confirm_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_yes")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def booking_action_kb(booking_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Принять", callback_data=f"accept_{booking_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"decline_{booking_id}"),
        ]
    ])


# ============ CLIENT FLOW ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_client(user)
    context.user_data.clear()
    await update.message.reply_text(
        f"Здравствуйте, {user.first_name}! 💅\n"
        "Я бот для записи на маникюр.\n\n"
        "Выберите действие:",
        reply_markup=main_menu_kb(is_admin(user.id)),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_reply_markup(reply_markup=None)
        await update.callback_query.message.reply_text(
            "Отменено. Главное меню:",
            reply_markup=main_menu_kb(is_admin(update.effective_user.id)),
        )
    else:
        await update.message.reply_text(
            "Отменено. Главное меню:",
            reply_markup=main_menu_kb(is_admin(update.effective_user.id)),
        )
    return ConversationHandler.END


async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Выберите дату визита:",
        reply_markup=dates_kb(),
    )
    return CHOOSE_DATE


async def choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        return await cancel(update, context)
    if query.data == "back_to_dates":
        await query.message.edit_text("Выберите дату визита:", reply_markup=dates_kb())
        return CHOOSE_DATE

    date_iso = query.data.replace("date_", "")
    context.user_data["date"] = date_iso

    conn = db()
    slots = conn.execute(
        "SELECT * FROM time_slots WHERE slot_date = ? AND is_available = 1 "
        "AND id NOT IN (SELECT slot_id FROM bookings WHERE status IN ('pending','accepted')) "
        "ORDER BY slot_time",
        (date_iso,),
    ).fetchall()
    conn.close()

    if not slots:
        await query.message.edit_text(
            f"На {date_iso} нет свободных окон. Выберите другую дату:",
            reply_markup=dates_kb(),
        )
        return CHOOSE_DATE

    await query.message.edit_text(
        f"Дата: {date_iso}\nВыберите удобное время:",
        reply_markup=slots_kb(slots),
    )
    return CHOOSE_SLOT


async def choose_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        return await cancel(update, context)
    if query.data == "back_to_dates":
        await query.message.edit_text("Выберите дату визита:", reply_markup=dates_kb())
        return CHOOSE_DATE

    slot_id = int(query.data.replace("slot_", ""))
    conn = db()
    slot = conn.execute("SELECT * FROM time_slots WHERE id = ?", (slot_id,)).fetchone()
    conn.close()
    context.user_data["slot_id"] = slot_id
    context.user_data["slot_time"] = slot["slot_time"]

    await query.message.edit_text(
        f"Дата: {context.user_data['date']}\n"
        f"Время: {slot['slot_time']}\n\n"
        "Выберите процедуру:",
        reply_markup=procedures_kb(),
    )
    return CHOOSE_PROCEDURE


async def choose_procedure(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        return await cancel(update, context)

    proc_id = int(query.data.replace("proc_", ""))
    conn = db()
    proc = conn.execute("SELECT * FROM procedures WHERE id = ?", (proc_id,)).fetchone()
    conn.close()

    context.user_data["procedure_id"] = proc_id
    context.user_data["procedure_name"] = proc["name"]
    context.user_data["procedure_price"] = proc["price"]

    await query.message.edit_text(
        "Хотите добавить комментарий к записи? "
        "Напишите его сообщением или нажмите «Пропустить».",
    )
    await query.message.reply_text(
        "✍️ Введите комментарий:",
        reply_markup=ReplyKeyboardMarkup([["Пропустить"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ENTER_COMMENT


async def enter_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    if comment.lower() in ("пропустить", "/skip"):
        comment = ""
    context.user_data["comment"] = comment

    await update.message.reply_text(
        "Проверьте запись:\n\n"
        f"📅 Дата: {context.user_data['date']}\n"
        f"⏰ Время: {context.user_data['slot_time']}\n"
        f"💅 Процедура: {context.user_data['procedure_name']}\n"
        f"💰 Стоимость: {context.user_data['procedure_price']} ₽\n"
        f"💬 Комментарий: {comment or '—'}\n\n"
        "Подтвердить?",
        reply_markup=confirm_kb(),
    )
    return CONFIRM


async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        return await cancel(update, context)

    user = update.effective_user
    client = get_or_create_client(user)

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bookings (client_id, procedure_id, slot_id, comment, status) "
        "VALUES (?, ?, ?, ?, 'pending')",
        (
            client["id"],
            context.user_data["procedure_id"],
            context.user_data["slot_id"],
            context.user_data.get("comment", ""),
        ),
    )
    conn.commit()
    conn.close()

    await query.message.edit_text(
        "✅ Заявка отправлена!\n"
        "Мастер рассмотрит её и подтвердит запись. "
        "Мы уведомим вас о решении."
    )
    # notify admins
    if ADMIN_TELEGRAM_IDS:
        text = (
            "🔔 Новая заявка!\n\n"
            f"👤 {client['full_name']} (@{client['username'] or '—'})\n"
            f"📅 {context.user_data['date']} в {context.user_data['slot_time']}\n"
            f"💅 {context.user_data['procedure_name']}\n"
            f"💰 {context.user_data['procedure_price']} ₽\n"
            f"💬 {context.user_data.get('comment') or '—'}"
        )
        for admin_id in ADMIN_TELEGRAM_IDS:
            try:
                await context.bot.send_message(admin_id, text)
            except Exception as e:
                logger.warning(f"Could not notify admin {admin_id}: {e}")

    context.user_data.clear()
    await query.message.reply_text(
        "Главное меню:",
        reply_markup=main_menu_kb(is_admin(user.id)),
    )
    return ConversationHandler.END


async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    client = get_or_create_client(user)
    conn = db()
    rows = conn.execute(
        """SELECT b.id, b.status, b.comment, t.slot_date, t.slot_time,
                  p.name AS proc_name, p.price
           FROM bookings b
           JOIN time_slots t ON t.id = b.slot_id
           JOIN procedures p ON p.id = b.procedure_id
           WHERE b.client_id = ?
           ORDER BY t.slot_date DESC, t.slot_time DESC
           LIMIT 20""",
        (client["id"],),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("У вас пока нет записей.")
        return

    status_map = {
        "pending": "⏳ ожидает подтверждения",
        "accepted": "✅ подтверждена",
        "declined": "❌ отклонена",
        "cancelled": "🚫 отменена",
    }
    lines = ["📋 Ваши записи:\n"]
    for r in rows:
        lines.append(
            f"#{r['id']} — {r['slot_date']} {r['slot_time']}\n"
            f"💅 {r['proc_name']} — {r['price']} ₽\n"
            f"Статус: {status_map.get(r['status'], r['status'])}\n"
            f"💬 {r['comment'] or '—'}\n"
        )
    await update.message.reply_text("\n".join(lines))


# ============ ADMIN FLOW ============
async def admin_login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 Введите пароль мастера:",
        reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_PASSWORD_STATE


async def admin_login_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() in ("отмена", "/cancel"):
        await update.message.reply_text(
            "Отменено.",
            reply_markup=main_menu_kb(is_admin(update.effective_user.id)),
        )
        return ConversationHandler.END

    if text == ADMIN_PASSWORD:
        context.user_data["is_admin_mode"] = True
        await update.message.reply_text(
            "✅ Добро пожаловать в режим мастера!",
            reply_markup=admin_menu_kb(),
        )
    else:
        await update.message.reply_text(
            "❌ Неверный пароль.",
            reply_markup=main_menu_kb(is_admin(update.effective_user.id)),
        )
    return ConversationHandler.END


async def admin_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("is_admin_mode", None)
    await update.message.reply_text(
        "🚪 Вы вышли из режима мастера.",
        reply_markup=main_menu_kb(is_admin(update.effective_user.id)),
    )


def _format_booking_row(r):
    return (
        f"#{r['id']} | {r['slot_time']} | {r['status']}\n"
        f"👤 {r['full_name']} (@{r['username'] or '—'})\n"
        f"💅 {r['proc_name']} — {r['price']} ₽\n"
        f"💬 {r['comment'] or '—'}"
    )


async def admin_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().date().isoformat()
    conn = db()
    rows = conn.execute(
        """SELECT b.id, b.status, b.comment, t.slot_time,
                  p.name AS proc_name, p.price,
                  c.full_name, c.username
           FROM bookings b
           JOIN time_slots t ON t.id = b.slot_id
           JOIN procedures p ON p.id = b.procedure_id
           JOIN clients c ON c.id = b.client_id
           WHERE t.slot_date = ?
           ORDER BY t.slot_time""",
        (today,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(f"На {today} записей нет.")
        return

    await update.message.reply_text(f"📆 Записи на {today}:")
    for r in rows:
        await update.message.reply_text(_format_booking_row(r))


async def admin_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        """SELECT b.id, b.status, b.comment, t.slot_date, t.slot_time,
                  p.name AS proc_name, p.price,
                  c.full_name, c.username
           FROM bookings b
           JOIN time_slots t ON t.id = b.slot_id
           JOIN procedures p ON p.id = b.procedure_id
           JOIN clients c ON c.id = b.client_id
           WHERE b.status = 'pending'
           ORDER BY t.slot_date, t.slot_time"""
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Нет новых заявок.")
        return

    await update.message.reply_text(f"📊 Новых заявок: {len(rows)}")
    for r in rows:
        text = (
            f"#{r['id']} | {r['slot_date']} {r['slot_time']}\n"
            f"👤 {r['full_name']} (@{r['username'] or '—'})\n"
            f"💅 {r['proc_name']} — {r['price']} ₽\n"
            f"💬 {r['comment'] or '—'}"
        )
        await update.message.reply_text(text, reply_markup=booking_action_kb(r["id"]))


async def booking_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, bid = query.data.split("_")
    bid = int(bid)
    new_status = "accepted" if action == "accept" else "declined"

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET status = ? WHERE id = ?", (new_status, bid))
    conn.commit()
    row = conn.execute(
        """SELECT b.id, b.status, t.slot_date, t.slot_time,
                  p.name AS proc_name, p.price,
                  c.telegram_id, c.full_name
           FROM bookings b
           JOIN time_slots t ON t.id = b.slot_id
           JOIN procedures p ON p.id = b.procedure_id
           JOIN clients c ON c.id = b.client_id
           WHERE b.id = ?""",
        (bid,),
    ).fetchone()
    conn.close()

    status_text = "✅ принята" if new_status == "accepted" else "❌ отклонена"
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"Заявка #{bid} {status_text}.")

    # Notify client
    try:
        if new_status == "accepted":
            client_msg = (
                f"✅ Ваша запись подтверждена!\n\n"
                f"📅 {row['slot_date']} в {row['slot_time']}\n"
                f"💅 {row['proc_name']} — {row['price']} ₽\n\n"
                "Ждём вас!"
            )
        else:
            client_msg = (
                f"❌ К сожалению, мастер отклонил заявку #{bid} "
                f"на {row['slot_date']} {row['slot_time']}.\n"
                "Пожалуйста, выберите другое время."
            )
        await context.bot.send_message(row["telegram_id"], client_msg)
    except Exception as e:
        logger.warning(f"Could not notify client: {e}")


async def admin_add_slot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите дату в формате ГГГГ-ММ-ДД (например, 2024-12-31):",
        reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_ADD_SLOT_DATE


async def admin_add_slot_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() in ("отмена", "/cancel"):
        await update.message.reply_text("Отменено.", reply_markup=admin_menu_kb())
        return ConversationHandler.END
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введите ГГГГ-ММ-ДД:")
        return ADMIN_ADD_SLOT_DATE
    context.user_data["add_slot_date"] = text
    await update.message.reply_text(
        "Введите время в формате ЧЧ:ММ (например, 14:30).\n"
        "Можно ввести несколько через запятую: 10:00, 12:30, 15:00",
    )
    return ADMIN_ADD_SLOT_TIME


async def admin_add_slot_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() in ("отмена", "/cancel"):
        await update.message.reply_text("Отменено.", reply_markup=admin_menu_kb())
        return ConversationHandler.END

    date_iso = context.user_data["add_slot_date"]
    times = [t.strip() for t in text.split(",") if t.strip()]
    added, errors = 0, []
    conn = db()
    cur = conn.cursor()
    for t in times:
        try:
            datetime.strptime(t, "%H:%M")
        except ValueError:
            errors.append(t)
            continue
        try:
            cur.execute(
                "INSERT INTO time_slots (slot_date, slot_time) VALUES (?, ?)",
                (date_iso, t),
            )
            added += 1
        except sqlite3.IntegrityError:
            errors.append(f"{t} (уже существует)")
    conn.commit()
    conn.close()

    msg = f"✅ Добавлено слотов: {added} на {date_iso}."
    if errors:
        msg += f"\n⚠️ Проблемы: {', '.join(errors)}"
    await update.message.reply_text(msg, reply_markup=admin_menu_kb())
    return ConversationHandler.END


async def admin_view_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().date().isoformat()
    conn = db()
    rows = conn.execute(
        """SELECT t.id, t.slot_date, t.slot_time, t.is_available,
                  (SELECT status FROM bookings b WHERE b.slot_id = t.id
                    AND b.status IN ('pending','accepted') LIMIT 1) AS booking_status
           FROM time_slots t
           WHERE t.slot_date >= ?
           ORDER BY t.slot_date, t.slot_time
           LIMIT 60""",
        (today,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Слотов нет.")
        return

    lines = ["🗓 Ближайшие слоты:\n"]
    current_date = None
    for r in rows:
        if r["slot_date"] != current_date:
            current_date = r["slot_date"]
            lines.append(f"\n📅 {current_date}:")
        mark = "🟢" if (r["is_available"] and not r["booking_status"]) else "🔴"
        lines.append(f"  {mark} {r['slot_time']}")
    await update.message.reply_text("\n".join(lines))


# ============ TEXT ROUTER ============
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    in_admin = context.user_data.get("is_admin_mode", False)

    if in_admin:
        if text == "📆 Записи на сегодня":
            return await admin_today(update, context)
        if text == "📊 Все заявки":
            return await admin_all(update, context)
        if text == "➕ Добавить слот":
            return await admin_add_slot_start(update, context)
        if text == "🗓 Слоты на дату":
            return await admin_view_slots(update, context)
        if text == "🚪 Выйти из режима мастера":
            return await admin_logout(update, context)

    if text == "📅 Записаться":
        return await book_start(update, context)
    if text == "📋 Мои записи":
        return await my_bookings(update, context)
    if text == "🔐 Режим мастера":
        return await admin_login_start(update, context)

    await update.message.reply_text(
        "Пожалуйста, воспользуйтесь меню.",
        reply_markup=main_menu_kb(is_admin(update.effective_user.id)),
    )


# ============ MAIN ============
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    client_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📅 Записаться$"), book_start)],
        states={
            CHOOSE_DATE: [CallbackQueryHandler(choose_date)],
            CHOOSE_SLOT: [CallbackQueryHandler(choose_slot)],
            CHOOSE_PROCEDURE: [CallbackQueryHandler(choose_procedure)],
            ENTER_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_comment)],
            CONFIRM: [CallbackQueryHandler(confirm_booking)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    admin_login_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔐 Режим мастера$"), admin_login_start)],
        states={
            ADMIN_PASSWORD_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_login_check)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    admin_add_slot_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить слот$"), admin_add_slot_start)],
        states={
            ADMIN_ADD_SLOT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_slot_date)],
            ADMIN_ADD_SLOT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_slot_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(client_conv)
    app.add_handler(admin_login_conv)
    app.add_handler(admin_add_slot_conv)
    app.add_handler(CallbackQueryHandler(booking_action, pattern=r"^(accept|decline)_\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()