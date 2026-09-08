from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Browser, Page, async_playwright

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://adminbook.top-academy.ru"
SCHEDULE_URL = f"{BASE_URL}/schedule#/groups"
CACHE_FILE = Path("schedule.json")


@dataclass(slots=True)
class Lesson:
    time: str
    text: str


@dataclass(slots=True)
class GroupSchedule:
    name: str
    days: dict[str, list[Lesson]]


class AdminbookScraper:
    def __init__(self) -> None:
        self.login = os.environ["ADMINBOOK_LOGIN"]
        self.password = os.environ["ADMINBOOK_PASSWORD"]
        self.headless = os.getenv("ADMINBOOK_HEADLESS", "true").lower() != "false"

    async def fetch(self) -> list[GroupSchedule]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.headless)
            try:
                page = await browser.new_page(locale="ru-RU")
                await self._login(page)
                groups = await self._read_schedule(page)
                if not groups:
                    raise RuntimeError("Не найдены таблицы расписания или группы")
                self._save_cache(groups)
                return groups
            finally:
                await browser.close()

    async def _login(self, page: Page) -> None:
        await page.goto(f"{BASE_URL}/#/", wait_until="domcontentloaded", timeout=60000)
        login_field = page.locator("#login, input[name='username'], input[type='email'], input[type='text']").first
        await login_field.wait_for(state="visible", timeout=60000)
        await login_field.fill(self.login)
        await page.locator("#password, input[name='password'], input[type='password']").first.fill(self.password)
        await page.get_by_role("button", name="Войти").click()
        await page.wait_for_timeout(2000)

        city = page.locator("#select_city_min")
        if await city.count():
            await city.select_option("585")
            await page.wait_for_timeout(2000)

        await page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        await self._select_college(page)

    async def _select_college(self, page: Page) -> None:
        selector = page.locator(".education-form").first
        if not await selector.count():
            return
        selected = await selector.inner_text()
        if "Колледж" in selected:
            return
        await selector.click()
        option = page.get_by_text("Колледж", exact=True).last
        if await option.count():
            await option.click()
            await page.wait_for_timeout(1500)

    async def _read_schedule(self, page: Page) -> list[GroupSchedule]:
        container = page.locator(".groups-table-scroll")
        if not await container.count():
            raise RuntimeError("Не найден контейнер расписания")

        # Angular infinite-scroll adds more tables while the container is scrolled.
        previous = -1
        for _ in range(25):
            count = await page.locator(".groups-table-scroll table.group").count()
            if count == previous:
                break
            previous = count
            await container.evaluate("element => element.scrollTop = element.scrollHeight")
            await page.wait_for_timeout(250)

        raw_groups: list[dict[str, Any]] = await page.locator(".groups-table-scroll table.group").evaluate_all(
            """
            tables => tables.map(table => {
              const headers = [...table.querySelectorAll('thead th')].map(node => node.innerText.trim());
              const days = headers.slice(1);
                            const lessons = [...table.querySelectorAll('tbody tr')].flatMap(row => {
                const cells = [...row.querySelectorAll('td')];
                                cells.shift();
                return cells.map((cell, index) => ({
                  day: days[index] || `day-${index}`,
                                    text: cell.innerText.replace(/\\s+/g, ' ').trim()
                })).filter(item => item.text);
              });
              return { name: headers[0] || '', lessons };
            })
            """
        )

        grouped: dict[str, GroupSchedule] = {}
        for raw in raw_groups:
            name = raw["name"]
            if not name:
                continue
            schedule = grouped.setdefault(name, GroupSchedule(name=name, days={}))
            for item in raw["lessons"]:
                match = re.search(r"\d{2}:\d{2}\s*-\s*\d{2}:\d{2}", item["text"])
                time = match.group(0) if match else ""
                text = item["text"].replace(time, "", 1).strip(" -")
                schedule.days.setdefault(item["day"], []).append(Lesson(time=time, text=text))
        return sorted(grouped.values(), key=lambda item: item.name.lower())

    @staticmethod
    def _save_cache(groups: list[GroupSchedule]) -> None:
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "groups": [{"name": item.name, "days": {
                day: [asdict(lesson) for lesson in lessons]
                for day, lessons in item.days.items()
            }} for item in groups],
        }
        CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cache() -> tuple[datetime | None, list[GroupSchedule]]:
    if not CACHE_FILE.exists():
        return None, []
    payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    groups = [
        GroupSchedule(
            name=item["name"],
            days={day: [Lesson(**lesson) for lesson in lessons] for day, lessons in item["days"].items()},
        )
        for item in payload.get("groups", [])
    ]
    updated_at = datetime.fromisoformat(payload["updated_at"]) if payload.get("updated_at") else None
    return updated_at, groups


async def refresh_loop(
    refresh_minutes: int,
    on_refresh: Callable[[list[GroupSchedule]], Awaitable[None]] | None = None,
) -> None:
    while True:
        try:
            groups = await AdminbookScraper().fetch()
            if on_refresh:
                await on_refresh(groups)
            LOGGER.info("Расписание обновлено")
        except Exception:
            LOGGER.exception("Не удалось обновить расписание")
        await asyncio.sleep(refresh_minutes * 60)
