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
python -m ttmon serve                    # панель http://127.0.0.1:8765 + расписание
python -m ttmon collect --slot morning
python -m ttmon report --date 2026-10-16
python -m ttmon report --test           # отчёт по тестовому слоту
python -m ttmon doctor
```

## Docker (Linux-сервер)

Данные контейнера — в `runtime/` (не `data/`): профиль Chrome у Linux свой, вход в TikTok — заново.
Порты панели (8765) и входа (6080) открыты только на `127.0.0.1` сервера, снаружи — SSH-туннель:
`ssh -L 8765:127.0.0.1:8765 -L 6080:127.0.0.1:6080 user@server`. Локальный порт занят — другой
(`-L 18765:127.0.0.1:8765`) и `web.public_url: http://127.0.0.1:18765` в `docker/config.docker.yaml`,
иначе кнопки панели ответят 403.

```bash
mkdir -p runtime/data runtime/logs runtime/chrome-profile runtime/secrets
sudo chown -R 1000:1000 runtime          # контейнер пишет под uid 1000 (другой — TTMON_UID/TTMON_GID в .env)
cp .env.example .env                     # ключи и PANEL_PASSWORD='…' (≥ 12 символов, в одинарных кавычках)
cp /путь/к/credentials.json /путь/к/token.json runtime/secrets/   # Google: токен с Windows подходит
docker compose build
docker compose run --rm --service-ports login   # http://127.0.0.1:6080/vnc.html, пароль — первые 8 символов
                                                # PANEL_PASSWORD; войти в TikTok (удобно по QR), закрыть окно
docker compose run --rm ttmon login --check
docker compose up -d                     # панель http://127.0.0.1:8765 (логин любой, пароль PANEL_PASSWORD)
docker compose logs -f
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
