# TT Live Monitor

Сбор продающих TikTok LIVE эфиров (Алматы) 3 раза в день, AI классификация, отчёт в Google Таблицу.

## Установка (Windows, Python 3.13.5)

```powershell
py -3.13 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt      # на боевой машине достаточно requirements.txt
copy .env.example .env                   # заполнить ключи
python -m alembic upgrade head           # создать/обновить data/ttmon.db
python -m ttmon login                    # вход в TikTok (рабочий аккаунт)
python -m ttmon google-login             # вход в Google: нужен secrets/credentials.json (OAuth Desktop client)
python scripts/upload_template.py шаблон.xlsx   # шаблон отчёта → Google Таблица, ID → .env
```

## Запуск

```powershell
python -m ttmon --help
python -m ttmon serve                    # панель http://127.0.0.1:8000 + расписание
python -m ttmon collect --slot morning
python -m ttmon report --date 2026-10-16
python -m ttmon report --test           # отчёт по тестовому слоту
python -m ttmon doctor
```

## Проверки

```powershell
python -m ruff check .
python -m mypy ttmon tests migrations/env.py scripts
python -m pytest
```

## Миграции

```powershell
python -m alembic revision --autogenerate -m "описание"
python -m alembic upgrade head
```
