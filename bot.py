from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

from scraper import GroupSchedule, load_cache, refresh_loop

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = 1009377663
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
PAGE_SIZE = 12
DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
SUBSCRIBERS_FILE = Path("subscribers.json")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
groups: list[GroupSchedule] = []
chat_state: dict[int, dict[str, int]] = {}
tracked_messages: dict[int, set[int]] = {}
subscribers: set[int] = set()
pending_changes = False


def load_subscribers() -> set[int]:
    if not SUBSCRIBERS_FILE.exists():
        return set()
    try:
        payload = json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
        return {int(chat_id) for chat_id in payload.get("chat_ids", [])}
    except (OSError, ValueError, TypeError):
        LOGGER.exception("Не удалось загрузить список пользователей")
        return set()


def save_subscribers() -> None:
    SUBSCRIBERS_FILE.write_text(
        json.dumps({"chat_ids": sorted(subscribers)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def remember_subscriber(chat_id: int) -> None:
    if chat_id not in subscribers:
        subscribers.add(chat_id)
        save_subscribers()


def has_lessons(group: GroupSchedule) -> bool:
    return any(is_displayable_lesson(lesson.text) for lessons in group.days.values() for lesson in lessons)


def is_displayable_lesson(text: str) -> bool:
    return "физическая культура" not in text.casefold() and "физкультура" not in text.casefold()


def state_for(chat_id: int) -> dict[str, int]:
    return chat_state.setdefault(chat_id, {"group": -1, "week": 0, "page": 0})


def main_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📚 Расписание недели")],
        [KeyboardButton(text="◀️ Предыдущая неделя"), KeyboardButton(text="Следующая неделя ▶️")],
        [KeyboardButton(text="👥 Выбрать группу"), KeyboardButton(text="🧹 Очистить чат")],
    ]
    if chat_id == ADMIN_ID:
        rows.append([KeyboardButton(text="🔔 Уведомить об изменениях")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def groups_keyboard(chat_id: int, page: int) -> ReplyKeyboardMarkup:
    start = page * PAGE_SIZE
    rows = [[KeyboardButton(text=group.name)] for group in groups[start:start + PAGE_SIZE]]
    navigation = []
    if page > 0:
        navigation.append(KeyboardButton(text="◀️ Предыдущие группы"))
    if (page + 1) * PAGE_SIZE < len(groups):
        navigation.append(KeyboardButton(text="Следующие группы ▶️"))
    if navigation:
        rows.append(navigation)
    rows.append([KeyboardButton(text="↩️ Назад в меню")])
    if chat_id == ADMIN_ID:
        rows.append([KeyboardButton(text="🔔 Уведомить об изменениях")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


async def send_message(chat_id: int, text: str, reply_markup: ReplyKeyboardMarkup | None = None) -> Message:
    message = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
    tracked_messages.setdefault(chat_id, set()).add(message.message_id)
    return message


async def clear_chat(chat_id: int, trigger: Message | None = None) -> None:
    message_ids = set(tracked_messages.pop(chat_id, set()))
    if trigger:
        message_ids.add(trigger.message_id)
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            pass


def format_schedule(group: GroupSchedule, week_offset: int = 0, only_today: bool = False) -> str:
    now = datetime.now(TIMEZONE)
    current_week_start = now - timedelta(days=now.weekday())
    week_start = current_week_start + timedelta(weeks=week_offset)
    wanted_indexes = [now.weekday()] if only_today and week_offset == 0 else range(7)
    days = {day.casefold(): lessons for day, lessons in group.days.items()}
    if only_today:
        title = "Расписание на сегодня"
    elif week_offset == 0:
        title = "Расписание на эту неделю"
    elif week_offset > 0:
        title = "Расписание на следующую неделю"
    else:
        title = "Расписание на предыдущую неделю"
    lines = [f"📚 <b>{html.escape(group.name)}</b>", f"<i>{title}</i>", ""]
    found = False
    for day_index in wanted_indexes:
        day = DAY_NAMES[day_index]
        lessons = [lesson for lesson in days.get(day.casefold(), []) if is_displayable_lesson(lesson.text)]
        if not lessons:
            continue
        found = True
        lesson_date = week_start + timedelta(days=day_index)
        lines.append(f"📅 <b>{day}, {lesson_date:%d.%m}</b>")
        for lesson_number, lesson in enumerate(sorted(lessons, key=lambda item: item.time), start=1):
            lesson_text = lesson.text.split(f" • {group.name}", 1)[0]
            parts = [part.strip() for part in lesson_text.split(" • ")]
            teacher = parts[0] if parts else lesson_text
            subject = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
            lines.append(
                f"{lesson_number}. <b>{html.escape(lesson.time)}</b>\n"
                f"   👤 {html.escape(teacher)}\n"
                f"   📖 {html.escape(subject)}"
            )
        lines.append("")
    if not found:
        return f"📚 <b>{html.escape(group.name)}</b>\n{title}: занятий нет."
    return "\n".join(lines).strip()


async def show_groups(chat_id: int, page: int = 0, trigger: Message | None = None) -> None:
    state_for(chat_id)["page"] = page
    await clear_chat(chat_id, trigger)
    if not groups:
        await send_message(chat_id, "Расписание пока не загружено. Попробуйте позже.", main_keyboard(chat_id))
        return
    total_pages = (len(groups) - 1) // PAGE_SIZE + 1
    await send_message(
        chat_id,
        f"📚 <b>Выберите группу</b>\nСтраница {page + 1} из {total_pages}",
        groups_keyboard(chat_id, page),
    )


async def show_selected_schedule(chat_id: int, only_today: bool = False, trigger: Message | None = None) -> None:
    state = state_for(chat_id)
    if state["group"] < 0 or state["group"] >= len(groups):
        await show_groups(chat_id, trigger=trigger)
        return
    await clear_chat(chat_id, trigger)
    await send_message(chat_id, format_schedule(groups[state["group"]], state["week"], only_today), main_keyboard(chat_id))


async def notify_subscribers() -> int:
    sent = 0
    for chat_id in list(subscribers):
        state = state_for(chat_id)
        if 0 <= state["group"] < len(groups):
            await clear_chat(chat_id)
            await send_message(
                chat_id,
                "🔔 <b>Расписание обновлено</b>\n\n" + format_schedule(groups[state["group"]], state["week"]),
                main_keyboard(chat_id),
            )
            sent += 1
    return sent


@dp.message(CommandStart())
async def start(message: Message) -> None:
    remember_subscriber(message.chat.id)
    await clear_chat(message.chat.id, message)
    await show_groups(message.chat.id)


@dp.message(Command("groups"))
async def list_groups(message: Message) -> None:
    remember_subscriber(message.chat.id)
    await show_groups(message.chat.id, trigger=message)


@dp.message(F.text == "👥 Выбрать группу")
async def choose_group_menu(message: Message) -> None:
    remember_subscriber(message.chat.id)
    await show_groups(message.chat.id, trigger=message)


@dp.message(F.text == "Следующие группы ▶️")
async def next_group_page(message: Message) -> None:
    state = state_for(message.chat.id)
    last_page = max((len(groups) - 1) // PAGE_SIZE, 0)
    await show_groups(message.chat.id, min(state["page"] + 1, last_page), message)


@dp.message(F.text == "◀️ Предыдущие группы")
async def previous_group_page(message: Message) -> None:
    await show_groups(message.chat.id, max(state_for(message.chat.id)["page"] - 1, 0), message)


@dp.message(F.text == "↩️ Назад в меню")
async def back_to_menu(message: Message) -> None:
    await clear_chat(message.chat.id, message)
    await send_message(message.chat.id, "Главное меню", main_keyboard(message.chat.id))


@dp.message(F.text == "📅 Сегодня")
async def today(message: Message) -> None:
    await show_selected_schedule(message.chat.id, only_today=True, trigger=message)


@dp.message(F.text == "📚 Расписание недели")
async def current_week(message: Message) -> None:
    state_for(message.chat.id)["week"] = 0
    await show_selected_schedule(message.chat.id, trigger=message)


@dp.message(F.text == "◀️ Предыдущая неделя")
async def previous_week(message: Message) -> None:
    state_for(message.chat.id)["week"] = -1
    await show_selected_schedule(message.chat.id, trigger=message)


@dp.message(F.text == "Следующая неделя ▶️")
async def next_week(message: Message) -> None:
    state_for(message.chat.id)["week"] = 1
    await show_selected_schedule(message.chat.id, trigger=message)


@dp.message(F.text == "🧹 Очистить чат")
async def clean_chat(message: Message) -> None:
    await clear_chat(message.chat.id, message)
    await send_message(message.chat.id, "Чат очищен.", main_keyboard(message.chat.id))


@dp.message(F.text == "🔔 Уведомить об изменениях")
async def notify_changes(message: Message) -> None:
    global pending_changes
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return
    await clear_chat(message.chat.id, message)
    if not pending_changes:
        await send_message(message.chat.id, "Новых изменений расписания пока нет.", main_keyboard(message.chat.id))
        return
    count = await notify_subscribers()
    pending_changes = False
    await send_message(message.chat.id, f"Уведомление отправлено пользователям: {count}.", main_keyboard(message.chat.id))


@dp.message(F.text)
async def select_group(message: Message) -> None:
    if message.text not in {group.name for group in groups}:
        return
    remember_subscriber(message.chat.id)
    state_for(message.chat.id)["group"] = next(index for index, group in enumerate(groups) if group.name == message.text)
    state_for(message.chat.id)["week"] = 0
    await show_selected_schedule(message.chat.id, only_today=True, trigger=message)


async def main() -> None:
    global groups, subscribers, pending_changes
    subscribers = load_subscribers()
    _, cached_groups = load_cache()
    groups = [group for group in cached_groups if has_lessons(group)]
    refresh_minutes = int(os.getenv("REFRESH_MINUTES", "30"))
    previous_snapshot = repr(groups)

    async def update_groups(updated: list[GroupSchedule]) -> None:
        nonlocal previous_snapshot
        global groups, pending_changes
        filtered = [group for group in updated if has_lessons(group)]
        current_snapshot = repr(filtered)
        if previous_snapshot != current_snapshot:
            pending_changes = True
        previous_snapshot = current_snapshot
        groups = filtered

    asyncio.create_task(refresh_loop(refresh_minutes, update_groups))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
