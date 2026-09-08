from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

from scraper import GroupSchedule, load_cache, refresh_loop

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
PAGE_SIZE = 12
DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
groups: list[GroupSchedule] = []


def group_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    buttons = [
        [InlineKeyboardButton(text=group.name, callback_data=f"group:{index}")]
        for index, group in enumerate(groups[start:start + PAGE_SIZE], start=start)
    ]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="<- Назад", callback_data=f"page:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(groups):
        navigation.append(InlineKeyboardButton(text="Далее ->", callback_data=f"page:{page + 1}"))
    if navigation:
        buttons.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def has_lessons(group: GroupSchedule) -> bool:
    return any(is_displayable_lesson(lesson.text) for lessons in group.days.values() for lesson in lessons)


def is_displayable_lesson(text: str) -> bool:
    return "физическая культура" not in text.casefold() and "физкультура" not in text.casefold()


def format_schedule(group: GroupSchedule, only_today: bool = False) -> str:
    now = datetime.now(TIMEZONE)
    wanted_indexes = [now.weekday()] if only_today else range(7)
    week_start = now - timedelta(days=now.weekday())
    days = {day.casefold(): lessons for day, lessons in group.days.items()}
    title = "Расписание на сегодня" if only_today else "Расписание на неделю"
    lines = [f"📚 <b>{html.escape(group.name)}</b>", f"<i>{title}</i>", ""]
    found = False
    for day_index in wanted_indexes:
        day = DAY_NAMES[day_index]
        lessons = days.get(day.casefold(), [])
        lessons = [lesson for lesson in lessons if is_displayable_lesson(lesson.text)]
        if not lessons:
            continue
        found = True
        lesson_date = week_start + timedelta(days=day_index)
        day_lessons = [f"📅 <b>{day}, {lesson_date:%d.%m}</b>"]
        sorted_lessons = sorted(lessons, key=lambda item: item.time)
        for lesson_number, lesson in enumerate(sorted_lessons, start=1):
            lesson_text = lesson.text.split(f" • {group.name}", 1)[0]
            parts = [part.strip() for part in lesson_text.split(" • ")]
            teacher = parts[0] if parts else lesson_text
            subject = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "")
            day_lessons.append(
                f"{lesson_number}. <b>{html.escape(lesson.time)}</b>\n"
                f"   👤 {html.escape(teacher)}\n"
                f"   📖 {html.escape(subject)}"
            )
        if len(lines) > 3:
            lines.append("")
        lines.extend(day_lessons)
    if not found:
        return f"📚 <b>{html.escape(group.name)}</b>\nНа этой неделе занятий нет."
    return "\n".join(lines)


async def show_groups(message: Message, page: int = 0) -> None:
    await send_groups(message.chat.id, page)


async def send_groups(chat_id: int, page: int = 0) -> None:
    if not groups:
        await bot.send_message(chat_id, "Расписание пока не загружено. Попробуйте позже.")
        return
    await bot.send_message(
        chat_id,
        f"📚 <b>Выберите группу</b>\nДоступно групп: {len(groups)}",
        parse_mode="HTML",
        reply_markup=group_keyboard(page),
    )


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer("Привет! Я покажу расписание вашей группы.")
    await show_groups(message)


@dp.message(Command("groups"))
async def list_groups(message: Message) -> None:
    await show_groups(message)


@dp.callback_query(F.data.startswith("page:"))
async def change_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":", 1)[1])
    await callback.message.edit_reply_markup(reply_markup=group_keyboard(page))
    await callback.answer()


@dp.callback_query(F.data.startswith("group:"))
async def choose_group(callback: CallbackQuery) -> None:
    index = int(callback.data.split(":", 1)[1])
    if index >= len(groups):
        await callback.answer("Группа больше недоступна", show_alert=True)
        return
    group = groups[index]
    await delete_bot_message(callback.message)
    await bot.send_message(
        callback.message.chat.id,
        format_schedule(group, only_today=True),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Вся неделя", callback_data=f"week:{index}"),
            InlineKeyboardButton(text="Другие группы", callback_data="groups"),
        ]]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("week:"))
async def show_week(callback: CallbackQuery) -> None:
    index = int(callback.data.split(":", 1)[1])
    await delete_bot_message(callback.message)
    await bot.send_message(callback.message.chat.id, format_schedule(groups[index]), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "groups")
async def groups_callback(callback: CallbackQuery) -> None:
    await delete_bot_message(callback.message)
    await send_groups(callback.message.chat.id)
    await callback.answer()


async def delete_bot_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def main() -> None:
    global groups
    _, cached_groups = load_cache()
    groups = [group for group in cached_groups if has_lessons(group)]
    refresh_minutes = int(os.getenv("REFRESH_MINUTES", "30"))

    async def update_groups(updated: list[GroupSchedule]) -> None:
        global groups
        groups = [group for group in updated if has_lessons(group)]

    asyncio.create_task(refresh_loop(refresh_minutes, update_groups))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
