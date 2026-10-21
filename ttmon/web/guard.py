from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ttmon.settings import WebConfig

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SAME_SITE = {"same-origin", "none"}

CallNext = Callable[[Request], Awaitable[Response]]
Middleware = Callable[[Request, CallNext], Awaitable[Response]]


def allowed_hosts(web: WebConfig) -> list[str]:
    """Проверка Host — защита от DNS-rebinding: чужой домен, перепривязанный на 127.0.0.1,
    иначе стал бы «тем же источником», что и панель."""
    return sorted({"127.0.0.1", "localhost", web.host})


def csrf_guard(web: WebConfig) -> Middleware:
    """Панель без авторизации: любой открытый в браузере сайт может послать простой POST
    (форма или fetch no-cors) на 127.0.0.1 и запустить сбор. Изменяющие запросы принимаются
    только с JSON (форма и no-cors его не выставят) и только со своего источника."""
    origins = {f"http://{host}:{web.port}" for host in allowed_hosts(web)}

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
