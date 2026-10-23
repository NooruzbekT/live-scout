import base64
import hmac
import time
from collections import deque
from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ttmon.settings import WebConfig

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SAME_SITE = {"same-origin", "none"}
MIN_PASSWORD_LENGTH = 12
AUTH_FAILS_MAX = 10
AUTH_WINDOW_S = 300.0
# выше — чистка IP с истёкшими попытками: словарь не растёт без предела
AUTH_TRACKED_IPS_MAX = 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

CallNext = Callable[[Request], Awaitable[Response]]
Middleware = Callable[[Request, CallNext], Awaitable[Response]]


class UnsafeExposure(ValueError):
    pass


def check_exposure(web: WebConfig, password: str | None) -> None:
    """Не loopback (0.0.0.0 в Docker) или public_url (прокси на той же машине) — только
    с паролем: иначе панель и запуск сбора открыты всем, кто достучится до порта."""
    if password is not None and len(password) < MIN_PASSWORD_LENGTH:
        raise UnsafeExposure(f"PANEL_PASSWORD короче {MIN_PASSWORD_LENGTH} символов")
    if password is None and not web.is_loopback:
        raise UnsafeExposure(f"web.host {web.host} без PANEL_PASSWORD: панель была бы открыта")
    if password is None and web.public_url:
        raise UnsafeExposure("web.public_url без PANEL_PASSWORD: панель была бы открыта")


def _unspecified(host: str) -> bool:
    try:
        return ip_address(host).is_unspecified
    except ValueError:
        return False


def allowed_hosts(web: WebConfig) -> list[str]:
    """Проверка Host — защита от DNS-rebinding: чужой домен, перепривязанный на 127.0.0.1,
    иначе стал бы «тем же источником», что и панель. 0.0.0.0 не разрешаем: браузеры
    открывают его как localhost."""
    hosts = {"127.0.0.1", "localhost"}
    if not _unspecified(web.host):
        hosts.add(web.host)
    if web.public_url and (public := urlsplit(web.public_url).hostname):
        hosts.add(public)
    return sorted(hosts)


def csrf_guard(web: WebConfig) -> Middleware:
    """Любой открытый в браузере сайт может послать простой POST (форма или fetch no-cors)
    на панель и запустить сбор — пароль Basic браузер подставит сам. Изменяющие запросы
    принимаются только с JSON (форма и no-cors его не выставят) и только со своего источника."""
    origins = {f"http://{host}:{web.port}" for host in allowed_hosts(web)}
    if web.public_url:
        origins.add(web.public_url)

    async def middleware(request: Request, call_next: CallNext) -> Response:
        if request.method not in SAFE_METHODS:
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            content_type = request.headers.get("content-type", "")
            if (
                (site is not None and site not in SAME_SITE)
                or (origin is not None and origin not in origins)
                or not content_type.startswith("application/json")
            ):
                return JSONResponse({"detail": "запрос не из панели"}, status_code=403)
        return await call_next(request)

    return middleware


def _basic_password(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not token:
        return None
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except ValueError:  # binascii.Error, UnicodeDecodeError, не-ASCII в токене
        return ""
    return decoded.partition(":")[2]


def basic_auth(password: str, clock: Callable[[], float] = time.monotonic) -> Middleware:
    """HTTP Basic: логин любой, проверяется пароль. Неверные попытки считаются по IP в окне
    AUTH_WINDOW_S; запрос без заголовка (первый заход браузера) попыткой не считается.
    Блокировка держит и верный пароль: иначе 200/429 продолжали бы перебор. За docker-proxy
    у всех клиентов один IP — блокировка общая."""
    expected = password.encode("utf-8")
    fails: dict[str, deque[float]] = {}

    async def middleware(request: Request, call_next: CallNext) -> Response:
        ip = request.client.host if request.client else "unknown"
        now = clock()
        recent = fails.get(ip)
        if recent is not None:
            while recent and now - recent[0] > AUTH_WINDOW_S:
                recent.popleft()
            if not recent:
                del fails[ip]
            elif len(recent) >= AUTH_FAILS_MAX:
                retry = int(AUTH_WINDOW_S - (now - recent[0])) + 1
                return JSONResponse(
                    {"detail": "слишком много неверных паролей"},
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                )
        given = _basic_password(request)
        if given is not None and hmac.compare_digest(given.encode("utf-8"), expected):
            return await call_next(request)
        if given is not None:
            if len(fails) >= AUTH_TRACKED_IPS_MAX:
                for stale in [k for k, v in fails.items() if now - v[-1] > AUTH_WINDOW_S]:
                    del fails[stale]
            fails.setdefault(ip, deque()).append(now)
        return JSONResponse(
            {"detail": "нужен пароль панели"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="ttmon", charset="UTF-8"'},
        )

    return middleware


async def security_headers(request: Request, call_next: CallNext) -> Response:
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response
