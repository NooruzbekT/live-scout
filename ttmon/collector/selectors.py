"""Все селекторы и URL TikTok. Основа: data-e2e и href; исключения помечены причиной."""

import re
from urllib.parse import quote

BASE_URL = "https://www.tiktok.com"
LIVE_URL = f"{BASE_URL}/live"
LOGIN_URL = f"{BASE_URL}/login"


def search_url(query: str) -> str:
    return f"{BASE_URL}/search/live?q={quote(query)}"


def room_url(username: str) -> str:
    return f"{BASE_URL}/@{username}/live"


# разведка 15.10.2026 под входом: 12 карточек search_live-item на экран.
# a[href*="/live"] без контейнера ловит и пункт меню LIVE (href="/live")
SEARCH_CARD = '[data-e2e="search_live-item"]'
SEARCH_CARD_LINK = f'{SEARCH_CARD} a[href^="/@"][href*="/live"]'
# ник идёт в селектор, имя файла и БД: только символы, допустимые в нике TikTok
USERNAME = re.compile(r"[A-Za-z0-9_.]+")
ROOM_HREF = re.compile(rf"^/@({USERNAME.pattern})/live(?:[/?#]|$)")


def search_card_link(username: str) -> str:
    # username уже прошёл ROOM_HREF: кавычек и пробелов в нём нет
    return f'{SEARCH_CARD} a[href^="/@{username}/live"]'


# клик по карточке — полноценная загрузка документа (не SPA), SIGI_STATE на странице эфира есть
ROOM_CONTENT = '[data-e2e="live-room-content"]'
ROOM_VIDEO = f"{ROOM_CONTENT} video"
SIGI_SCRIPT_ID = "SIGI_STATE"
# во всех 8 эфирах разведки «Комментарии выключены»: селекторы сообщений вживую не проверены
CHAT_MESSAGE = '[data-e2e="chat-message"]'
CHAT_OWNER = '[data-e2e="message-owner-name"]'

SESSION_COOKIE = "sessionid"

# data-e2e у кнопки входа нет (разведка 15.10.2026): при ширине 1440 — кнопка в шапке,
# в узком окне — иконка в левой панели
LOGIN_BUTTON = '#header-login-button, button[aria-label="Войти"]'
# есть на /live в любом состоянии входа: признак, что страница отрисовалась
PAGE_READY = '[data-e2e="nav-more-menu"]'

# капча — сторонний виджет без data-e2e (наяву не видели, на 15.10.2026 на /live 0 совпадений);
# :visible — скрытый прелоадер виджета капчей не считается
CAPTCHA = (
    '[id*="captcha" i]:visible, [class*="captcha" i]:visible, iframe[src*="captcha" i]:visible'
)
CAPTCHA_DIALOG = '[role="dialog"]:visible'
CAPTCHA_DIALOG_TEXTS = ("Verify", "Проверка", "Перетащите", "пазл")

# текст страницы ошибки при HTTP 403 (наблюдался 15.10.2026); ё/е нормализуются в guards
FORBIDDEN_TEXTS = (
    "Доступ к www.tiktok.com запрещен",
    "Доступ к сайту www.tiktok.com запрещен",
    "HTTP ERROR 403",
)
FORBIDDEN_TEXT_RE = re.compile(r"Доступ к (сайту )?www\.tiktok\.com запрещ[её]н|HTTP ERROR 403")

# предложения TikTok поверх выдачи (21.10.2026: «Создайте ключ для быстрого входа») перекрывают
# карточки и прокрутку; появляются не на каждой загрузке, DOM не снят — ищем кнопку отказа по тексту
DISMISS_PROMPT_RE = re.compile(r"^\s*(Возможно, позже|Не сейчас|Maybe later|Not now)\s*$")
