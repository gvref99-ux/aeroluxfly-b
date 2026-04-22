import os
import sqlite3
import tempfile
from datetime import datetime, timezone

import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ==============================
# НАСТРОЙКИ
# ==============================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
BOOKING_CHANNEL_ID = int(os.getenv("BOOKING_CHANNEL_ID"))

DB_PATH = "bookings.db"
DATETIME_FORMAT = "%m-%d %H:%M"
PARSE_DATETIME_FORMAT = "%Y-%m-%d %H:%M"
TIMEZONE_LABEL = "UTC"

PDF_FONT_NAME = "BookingPDFRegular"
PDF_FONT_BOLD_NAME = "BookingPDFBold"

ALLOWED_CANCEL_ROLES = {"CEO", "COO", "COO executive"}


# ==============================
# ВРЕМЯ
# ==============================
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc_datetime(value: str) -> datetime:
    cleaned_value = value.strip()
    current_year_utc = utc_now().year
    dt = datetime.strptime(f"{current_year_utc}-{cleaned_value}", PARSE_DATETIME_FORMAT)
    return dt.replace(tzinfo=timezone.utc)


def format_utc_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(DATETIME_FORMAT)


def format_utc_datetime_full(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ==============================
# БАЗА ДАННЫХ
# ==============================
def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_sql: str):
    existing = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing_names = {row[1] for row in existing}
    if column_name not in existing_names:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                callsign TEXT NOT NULL,
                flight_number TEXT NOT NULL,
                board_number TEXT NOT NULL,
                dep_icao TEXT NOT NULL,
                arr_icao TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                estimated_return_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                returned_at TEXT,
                return_confirmed_by INTEGER,
                booking_message_id INTEGER
            )
        """)

        ensure_column(conn, "bookings", "cancelled_by_user_id", "cancelled_by_user_id INTEGER")
        ensure_column(conn, "bookings", "cancelled_by_name", "cancelled_by_name TEXT")
        ensure_column(conn, "bookings", "returned_by_name", "returned_by_name TEXT")
        ensure_column(conn, "bookings", "creator_display_name", "creator_display_name TEXT")
        ensure_column(conn, "bookings", "cancel_reason", "cancel_reason TEXT")

        conn.commit()


def normalize_code(value: str) -> str:
    return value.strip().upper()


# ==============================
# SQL-ФУНКЦИИ
# ==============================
def create_booking(
    user_id: int,
    username: str,
    creator_display_name: str,
    callsign: str,
    flight_number: str,
    board_number: str,
    dep_icao: str,
    arr_icao: str,
    departure_time: datetime,
    estimated_return_time: datetime,
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO bookings (
                user_id,
                username,
                creator_display_name,
                callsign,
                flight_number,
                board_number,
                dep_icao,
                arr_icao,
                departure_time,
                estimated_return_time,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """, (
            user_id,
            username,
            creator_display_name,
            callsign,
            flight_number,
            board_number,
            dep_icao,
            arr_icao,
            format_utc_datetime(departure_time),
            format_utc_datetime(estimated_return_time),
            format_utc_datetime_full(utc_now()),
        ))
        conn.commit()
        return cur.lastrowid


def get_booking_by_id(booking_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM bookings WHERE id = ? LIMIT 1",
            (booking_id,)
        ).fetchone()


def get_user_bookings(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("""
            SELECT *
            FROM bookings
            WHERE user_id = ?
            ORDER BY departure_time DESC
            LIMIT 10
        """, (user_id,)).fetchall()


def get_active_bookings():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("""
            SELECT *
            FROM bookings
            WHERE status = 'active'
            ORDER BY departure_time ASC
        """).fetchall()


def get_all_bookings_history():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("""
            SELECT *
            FROM bookings
            ORDER BY created_at DESC, departure_time DESC
        """).fetchall()


def set_booking_message_id(booking_id: int, message_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE bookings SET booking_message_id = ? WHERE id = ?",
            (message_id, booking_id)
        )
        conn.commit()


def mark_booking_returned(
    booking_id: int,
    confirmed_by_user_id: int,
    confirmed_by_name: str,
    returned_at: datetime | None = None,
):
    actual_return = returned_at or utc_now()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE bookings
            SET status = 'returned',
                returned_at = ?,
                return_confirmed_by = ?,
                returned_by_name = ?
            WHERE id = ?
        """, (
            format_utc_datetime_full(actual_return),
            confirmed_by_user_id,
            confirmed_by_name,
            booking_id,
        ))
        conn.commit()


def auto_mark_booking_returned(booking_id: int, returned_at: datetime | None = None):
    actual_return = returned_at or utc_now()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE bookings
            SET status = 'returned',
                returned_at = ?,
                returned_by_name = ?
            WHERE id = ?
        """, (
            format_utc_datetime_full(actual_return),
            "AUTO UTC RETURN",
            booking_id,
        ))
        conn.commit()


def cancel_booking(
    booking_id: int,
    cancelled_by_user_id: int,
    cancelled_by_name: str,
    cancel_reason: str,
    cancelled_at: datetime | None = None,
):
    actual_cancel_time = cancelled_at or utc_now()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE bookings
            SET status = 'cancelled',
                returned_at = ?,
                cancelled_by_user_id = ?,
                cancelled_by_name = ?,
                cancel_reason = ?
            WHERE id = ?
        """, (
            format_utc_datetime_full(actual_cancel_time),
            cancelled_by_user_id,
            cancelled_by_name,
            cancel_reason,
            booking_id,
        ))
        conn.commit()


def find_conflict(board_number: str, new_departure: datetime, new_return: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT *
            FROM bookings
            WHERE UPPER(board_number) = UPPER(?)
              AND status = 'active'
            ORDER BY departure_time ASC
        """, (board_number,)).fetchall()

    for row in rows:
        existing_departure = parse_utc_datetime(row["departure_time"])
        existing_return = parse_utc_datetime(row["estimated_return_time"])
        overlaps = new_departure < existing_return and new_return > existing_departure
        if overlaps:
            return row

    return None


def get_booking_for_manual_return(board_number: str, now_utc: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT *
            FROM bookings
            WHERE UPPER(board_number) = UPPER(?)
              AND status = 'active'
            ORDER BY departure_time ASC
        """, (board_number,)).fetchall()

    for row in rows:
        departure = parse_utc_datetime(row["departure_time"])
        estimated_return = parse_utc_datetime(row["estimated_return_time"])
        if departure <= now_utc < estimated_return:
            return row

    return None


def get_expired_bookings(now_utc: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT *
            FROM bookings
            WHERE status = 'active'
            ORDER BY estimated_return_time ASC
        """).fetchall()

    expired = []
    for row in rows:
        estimated_return = parse_utc_datetime(row["estimated_return_time"])
        if estimated_return <= now_utc:
            expired.append(row)
    return expired


def get_booking_for_cancel(board_number: str, departure_time: datetime):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("""
            SELECT *
            FROM bookings
            WHERE UPPER(board_number) = UPPER(?)
              AND departure_time = ?
              AND status = 'active'
            LIMIT 1
        """, (
            board_number,
            format_utc_datetime(departure_time),
        )).fetchone()


# ==============================
# PDF
# ==============================
def register_pdf_fonts():
    regular_candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    bold_candidates = [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]

    regular_path = next((path for path in regular_candidates if os.path.exists(path)), None)
    bold_path = next((path for path in bold_candidates if os.path.exists(path)), None)

    if not regular_path:
        raise RuntimeError("Не найден TTF-шрифт с поддержкой кириллицы для PDF.")

    if PDF_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(PDF_FONT_NAME, regular_path))

    if bold_path and PDF_FONT_BOLD_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(PDF_FONT_BOLD_NAME, bold_path))

    if PDF_FONT_BOLD_NAME not in pdfmetrics.getRegisteredFontNames():
        return PDF_FONT_NAME, PDF_FONT_NAME

    return PDF_FONT_NAME, PDF_FONT_BOLD_NAME


def build_history_pdf() -> str:
    rows = get_all_bookings_history()
    created_at_utc = utc_now()
    timestamp = created_at_utc.strftime("%Y-%m-%d_%H-%M-%S")
    output_path = os.path.join(tempfile.gettempdir(), f"booking_history_{timestamp}_UTC.pdf")

    regular_font, bold_font = register_pdf_fonts()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=landscape(A4),
        leftMargin=6 * mm,
        rightMargin=6 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )

    styles = getSampleStyleSheet()
    styles["Title"].fontName = bold_font
    styles["Normal"].fontName = regular_font

    story = []
    story.append(Paragraph("История броней бортов", styles["Title"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"Сформировано: {format_utc_datetime_full(created_at_utc)} UTC", styles["Normal"]))
    story.append(Spacer(1, 10))

    if not rows:
        story.append(Paragraph("История пуста.", styles["Normal"]))
        doc.build(story)
        return output_path

    table_data = [[
        "Статус",
        "Борт",
        "Рейс",
        "Позывной",
        "Маршрут",
        "Вылет UTC",
        "Прибытие UTC",
        "Создал",
        "Завершил/отменил",
        "Время действия UTC",
        "Причина отмены",
    ]]

    for row in rows:
        status_text = {
            "active": "Активна",
            "returned": "Возвращён",
            "cancelled": "Отменена",
        }.get(str(row["status"]), str(row["status"]))

        actor_name = "-"
        if row["status"] == "returned":
            actor_name = row["returned_by_name"] or "-"
        elif row["status"] == "cancelled":
            actor_name = row["cancelled_by_name"] or "-"

        cancel_reason = row["cancel_reason"] or "-"
        action_time = row["returned_at"] or "-"

        table_data.append([
            status_text,
            str(row["board_number"]),
            str(row["flight_number"]),
            str(row["callsign"]),
            f"{row['dep_icao']}-{row['arr_icao']}",
            str(row["departure_time"]),
            str(row["estimated_return_time"]),
            str(row["creator_display_name"] or row["username"]),
            actor_name,
            action_time,
            cancel_reason,
        ])

    table = Table(
        table_data,
        repeatRows=1,
        colWidths=[
            20 * mm, 18 * mm, 18 * mm, 20 * mm, 22 * mm,
            26 * mm, 26 * mm, 26 * mm, 28 * mm, 30 * mm, 42 * mm
        ],
    )
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), bold_font),
        ("FONTNAME", (0, 1), (-1, -1), regular_font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 6.5),
        ("LEADING", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.beige]),
    ]))

    story.append(table)
    doc.build(story)
    return output_path


# ==============================
# DISCORD BOT
# ==============================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def get_member_display_name(member: discord.abc.User | discord.Member) -> str:
    if isinstance(member, discord.Member):
        return member.display_name
    return getattr(member, "display_name", str(member))


def has_cancel_role(member: discord.Member) -> bool:
    member_role_names = {role.name for role in member.roles}
    return any(role_name in member_role_names for role_name in ALLOWED_CANCEL_ROLES)


async def get_booking_channel():
    channel = bot.get_channel(BOOKING_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(BOOKING_CHANNEL_ID)
    return channel


async def send_booking_message(booking_row):
    channel = await get_booking_channel()

    creator_name = booking_row["creator_display_name"] or booking_row["username"]

    embed = discord.Embed(
        title="Активная бронь борта",
        description=f"Бронь создана. Всё время указано в {TIMEZONE_LABEL}.",
        timestamp=utc_now(),
    )
    embed.add_field(name="Позывной", value=booking_row["callsign"], inline=True)
    embed.add_field(name="Рейс", value=booking_row["flight_number"], inline=True)
    embed.add_field(name="Номер борта", value=booking_row["board_number"], inline=True)
    embed.add_field(name="Вылет ICAO", value=booking_row["dep_icao"], inline=True)
    embed.add_field(name="Прибытие ICAO", value=booking_row["arr_icao"], inline=True)
    embed.add_field(name=f"Дата и время вылета ({TIMEZONE_LABEL})", value=booking_row["departure_time"], inline=False)
    embed.add_field(name=f"Дата и время прибытия ({TIMEZONE_LABEL})", value=booking_row["estimated_return_time"], inline=False)
    embed.add_field(name="Кто забронировал", value=creator_name, inline=False)
    embed.set_footer(text=f"ID брони: {booking_row['id']}")

    message = await channel.send(embed=embed)
    set_booking_message_id(booking_row["id"], message.id)


async def delete_booking_message(message_id: int | None):
    if not message_id:
        return

    try:
        channel = await get_booking_channel()
        message = await channel.fetch_message(message_id)
        await message.delete()
    except Exception:
        pass


@tasks.loop(minutes=1)
async def auto_return_check():
    now_utc = utc_now()
    expired_rows = get_expired_bookings(now_utc)

    for row in expired_rows:
        auto_mark_booking_returned(row["id"], returned_at=now_utc)
        await delete_booking_message(row["booking_message_id"])


@auto_return_check.before_loop
async def before_auto_return_check():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user}")
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        print(f"Команды синхронизированы: {len(synced)}")
    except Exception as e:
        print("Ошибка синхронизации команд:", e)

    if not auto_return_check.is_running():
        auto_return_check.start()


@bot.event
async def on_message(message: discord.Message):
    if message.channel.id == BOOKING_CHANNEL_ID:
        if message.author.id != bot.user.id:
            try:
                await message.delete()
            except Exception:
                pass

    await bot.process_commands(message)


# ==============================
# SLASH-КОМАНДЫ
# ==============================
@bot.tree.command(
    name="booking_flight",
    description="Создать бронь борта",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    callsign="Позывной",
    flight_number="Рейс",
    board_number="Номер борта",
    dep_icao="ICAO вылета",
    arr_icao="ICAO прибытия",
    departure_time=f"Дата и время вылета в формате MM-DD HH:MM ({TIMEZONE_LABEL})",
    estimated_return_time=f"Дата и время прибытия в формате MM-DD HH:MM ({TIMEZONE_LABEL})",
)
async def booking_flight(
    interaction: discord.Interaction,
    callsign: str,
    flight_number: str,
    board_number: str,
    dep_icao: str,
    arr_icao: str,
    departure_time: str,
    estimated_return_time: str,
):
    await interaction.response.defer(ephemeral=True)

    callsign = normalize_code(callsign)
    flight_number = normalize_code(flight_number)
    board_number = normalize_code(board_number)
    dep_icao = normalize_code(dep_icao)
    arr_icao = normalize_code(arr_icao)

    creator_display_name = get_member_display_name(interaction.user)

    if len(dep_icao) != 4 or len(arr_icao) != 4:
        await interaction.followup.send(
            "ICAO коды должны быть по 4 символа. Пример: UUEE, ULLI.",
            ephemeral=True,
        )
        return

    try:
        departure_dt = parse_utc_datetime(departure_time)
        return_dt = parse_utc_datetime(estimated_return_time)
    except ValueError:
        await interaction.followup.send(
            f"Неверный формат даты. Используй MM-DD HH:MM. Всё время вводится в {TIMEZONE_LABEL}.",
            ephemeral=True,
        )
        return

    if return_dt <= departure_dt:
        await interaction.followup.send(
            "Дата и время прибытия должно быть позже времени вылета.",
            ephemeral=True,
        )
        return

    conflict = find_conflict(board_number, departure_dt, return_dt)
    if conflict:
        await interaction.followup.send(
            (
                "Этот борт уже занят на пересекающееся время.\n"
                f"Текущая бронь: рейс {conflict['flight_number']}, позывной {conflict['callsign']}.\n"
                f"Интервал ({TIMEZONE_LABEL}): {conflict['departure_time']} — {conflict['estimated_return_time']}\n"
                "Можно бронировать этот же борт только на непересекающееся время."
            ),
            ephemeral=True,
        )
        return

    booking_id = create_booking(
        user_id=interaction.user.id,
        username=str(interaction.user),
        creator_display_name=creator_display_name,
        callsign=callsign,
        flight_number=flight_number,
        board_number=board_number,
        dep_icao=dep_icao,
        arr_icao=arr_icao,
        departure_time=departure_dt,
        estimated_return_time=return_dt,
    )

    booking = get_booking_by_id(booking_id)
    if booking:
        await send_booking_message(booking)

    await interaction.followup.send(
        (
            "Бронь создана.\n"
            f"Позывной: {callsign}\n"
            f"Рейс: {flight_number}\n"
            f"Борт: {board_number}\n"
            f"Маршрут: {dep_icao} → {arr_icao}\n"
            f"Вылет ({TIMEZONE_LABEL}): {format_utc_datetime(departure_dt)}\n"
            f"Расчётный возврат ({TIMEZONE_LABEL}): {format_utc_datetime(return_dt)}\n"
            f"Создал: {creator_display_name}"
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="return_flight",
    description="Подтвердить возвращение борта",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(board_number="Номер борта")
async def return_flight(interaction: discord.Interaction, board_number: str):
    await interaction.response.defer(ephemeral=True)

    board_number = normalize_code(board_number)
    now_utc = utc_now()
    booking = get_booking_for_manual_return(board_number, now_utc)

    if not booking:
        await interaction.followup.send(
            (
                f"Для борта {board_number} сейчас нет активного полёта для возврата.\n"
                f"Проверь номер борта или дождись времени вылета. Всё время у бота в {TIMEZONE_LABEL}."
            ),
            ephemeral=True,
        )
        return

    if booking["user_id"] != interaction.user.id:
        await interaction.followup.send(
            "Завершить полёт может только тот, кто создал эту бронь.",
            ephemeral=True,
        )
        return

    actor_name = get_member_display_name(interaction.user)

    mark_booking_returned(
        booking["id"],
        confirmed_by_user_id=interaction.user.id,
        confirmed_by_name=actor_name,
        returned_at=now_utc,
    )
    await delete_booking_message(booking["booking_message_id"])

    await interaction.followup.send(
        f"Возврат борта {board_number} подтверждён. Борт снова доступен для бронирования.",
        ephemeral=True,
    )


@bot.tree.command(
    name="cancel_flight",
    description="Отменить бронь по борту и времени вылета",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    board_number="Номер борта",
    departure_time=f"Время вылета в формате MM-DD HH:MM ({TIMEZONE_LABEL})",
    reason="Причина отмены",
)
async def cancel_flight(
    interaction: discord.Interaction,
    board_number: str,
    departure_time: str,
    reason: str,
):
    await interaction.response.defer(ephemeral=True)

    board_number = normalize_code(board_number)
    reason = reason.strip()

    if not reason:
        await interaction.followup.send(
            "Нужно указать причину отмены.",
            ephemeral=True,
        )
        return

    try:
        departure_dt = parse_utc_datetime(departure_time)
    except ValueError:
        await interaction.followup.send(
            f"Неверный формат даты. Используй MM-DD HH:MM. Всё время вводится в {TIMEZONE_LABEL}.",
            ephemeral=True,
        )
        return

    booking = get_booking_for_cancel(board_number, departure_dt)
    if not booking:
        await interaction.followup.send(
            f"Активная бронь для борта {board_number} с вылетом {format_utc_datetime(departure_dt)} {TIMEZONE_LABEL} не найдена.",
            ephemeral=True,
        )
        return

    member = interaction.user
    is_creator = booking["user_id"] == interaction.user.id
    has_role_permission = isinstance(member, discord.Member) and has_cancel_role(member)

    if not is_creator and not has_role_permission:
        await interaction.followup.send(
            "Отменить бронь может только создатель или пользователь с ролью CEO / COO / COO executive.",
            ephemeral=True,
        )
        return

    actor_name = get_member_display_name(interaction.user)

    cancel_booking(
        booking["id"],
        cancelled_by_user_id=interaction.user.id,
        cancelled_by_name=actor_name,
        cancel_reason=reason,
        cancelled_at=utc_now(),
    )
    await delete_booking_message(booking["booking_message_id"])

    await interaction.followup.send(
        (
            f"Бронь борта {board_number} с вылетом {format_utc_datetime(departure_dt)} {TIMEZONE_LABEL} отменена.\n"
            f"Причина: {reason}"
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="check_booking",
    description="Показать активные брони только тебе",
    guild=discord.Object(id=GUILD_ID),
)
async def check_booking(interaction: discord.Interaction):
    rows = get_active_bookings()

    if not rows:
        await interaction.response.send_message(
            f"Сейчас нет активных броней. Всё время отображается в {TIMEZONE_LABEL}.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title=f"Активные брони ({TIMEZONE_LABEL})")

    for row in rows[:25]:
        creator_name = row["creator_display_name"] or row["username"]
        embed.add_field(
            name=f"{row['board_number']} | {row['flight_number']}",
            value=(
                f"Позывной: {row['callsign']}\n"
                f"Маршрут: {row['dep_icao']} → {row['arr_icao']}\n"
                f"Вылет: {row['departure_time']}\n"
                f"Возврат: {row['estimated_return_time']}\n"
                f"Создал: {creator_name}"
            ),
            inline=False,
        )

    if len(rows) > 25:
        embed.set_footer(text=f"Показаны первые 25 из {len(rows)} активных броней")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="history_booking",
    description="Отправить PDF с историей всех броней",
    guild=discord.Object(id=GUILD_ID),
)
async def history_booking(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    pdf_path = build_history_pdf()
    filename = os.path.basename(pdf_path)
    file = discord.File(pdf_path, filename=filename)

    await interaction.followup.send(
        content="Готово. Вот PDF с историей всех броней.",
        file=file,
        ephemeral=True,
    )

    try:
        os.remove(pdf_path)
    except Exception:
        pass


@bot.tree.command(
    name="help",
    description="Показать все команды и возможности бота",
    guild=discord.Object(id=GUILD_ID),
)
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Справка по боту бронирования",
        description=(
            f"Бот работает только со временем в {TIMEZONE_LABEL}. Формат времени: MM-DD HH:MM.\n"
            "Команды можно использовать в любом канале сервера."
        ),
    )

    embed.add_field(
        name="/booking_flight",
        value=(
            "Создаёт бронь борта.\n"
            "Параметры: callsign, flight_number, board_number, dep_icao, arr_icao, departure_time, estimated_return_time."
        ),
        inline=False,
    )
    embed.add_field(
        name="/return_flight",
        value="Завершает полёт. Может только создатель брони.",
        inline=False,
    )
    embed.add_field(
        name="/cancel_flight",
        value="Отменяет бронь. Обязательно нужно указать причину. Может создатель или роль CEO / COO / COO executive.",
        inline=False,
    )
    embed.add_field(
        name="/check_booking",
        value="Показывает активные брони только тебе.",
        inline=False,
    )
    embed.add_field(
        name="/history_booking",
        value="Отправляет горизонтальный PDF со всей историей броней.",
        inline=False,
    )
    embed.add_field(
        name="/my_brons",
        value="Показывает твои последние брони и их статусы.",
        inline=False,
    )
    embed.add_field(
        name="Канал бронирований",
        value="В канале бронирований остаются только сообщения этого бота. Все чужие сообщения удаляются.",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="my_brons",
    description="Показать мои последние брони",
    guild=discord.Object(id=GUILD_ID),
)
async def my_brons(interaction: discord.Interaction):
    rows = get_user_bookings(interaction.user.id)

    if not rows:
        await interaction.response.send_message("У тебя пока нет броней.", ephemeral=True)
        return

    embed = discord.Embed(title=f"Мои брони ({TIMEZONE_LABEL})")

    for row in rows:
        status_text = row["status"]
        actor_line = ""

        if row["status"] == "returned" and row["returned_by_name"]:
            actor_line = f"\nЗавершил: {row['returned_by_name']}"
        elif row["status"] == "cancelled" and row["cancelled_by_name"]:
            actor_line = f"\nОтменил: {row['cancelled_by_name']}"

        returned_line = ""
        if row["returned_at"]:
            returned_line = f"\nВремя действия: {row['returned_at']} UTC"

        cancel_reason_line = ""
        if row["status"] == "cancelled" and row["cancel_reason"]:
            cancel_reason_line = f"\nПричина отмены: {row['cancel_reason']}"

        embed.add_field(
            name=f"ID {row['id']} | {row['flight_number']} | {row['board_number']}",
            value=(
                f"Позывной: {row['callsign']}\n"
                f"Маршрут: {row['dep_icao']} → {row['arr_icao']}\n"
                f"Вылет: {row['departure_time']}\n"
                f"Расчётный возврат: {row['estimated_return_time']}\n"
                f"Статус: {status_text}{returned_line}{actor_line}{cancel_reason_line}"
            ),
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN or TOKEN == "ВСТАВЬ_СЮДА_НОВЫЙ_ТОКЕН":
        raise RuntimeError("Вставь токен бота в переменную TOKEN.")

    init_db()
    bot.run(TOKEN)