# Telegram-бот расписания

Бот получает расписание из Adminbook, выбирает город `Королёв Колледж`, читает все доступные группы и показывает расписание в Telegram.

## Запуск в Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py -m playwright install chromium
Copy-Item .env.example .env
```

Заполните `.env`:

- `TELEGRAM_BOT_TOKEN` - токен, выданный `@BotFather`;
- `ADMINBOOK_LOGIN` и `ADMINBOOK_PASSWORD` - учётные данные Adminbook.

Затем запустите:

```powershell
py bot.py
```

## Запуск в Docker

Для деплоя используйте Dockerfile из репозитория: он устанавливает Chromium и системные зависимости Playwright во время сборки.

```powershell
docker build -t colledge-top-bot .
docker run --env-file .env colledge-top-bot
```

На хостинге выберите сборку через Dockerfile. Если хостинг использует собственную команду сборки без Docker, добавьте в неё `playwright install --with-deps chromium` после установки зависимостей Python.

При первом обновлении бот войдёт на сайт, выберет колледж, прокрутит список с infinite scroll и сохранит результат в `schedule.json`. Навигация работает через постоянную клавиатуру снизу: можно выбрать группу, открыть сегодня, текущую, предыдущую или следующую неделю. Перед новым экраном бот удаляет свои предыдущие сообщения и сообщение с нажатой командой.

Пользователи, открывшие бота, сохраняются в `subscribers.json`. Если расписание изменилось, администратор с Telegram ID `1009377663` увидит кнопку `Уведомить об изменениях` и сможет отправить актуальное расписание всем пользователям с выбранной группой.

## Важно

Секреты хранятся только в `.env`, который исключён из Git. Учётные данные, опубликованные в переписке, лучше заменить после настройки. Если сайт изменит HTML или добавит CAPTCHA/2FA, селекторы в `scraper.py` потребуют обновления.
