#!/bin/sh
# Виртуальный экран для Chrome; vnc-login — ещё и noVNC (порт 6080) для входа в TikTok.
set -e

# после restart контейнера /tmp сохраняется: старый lock не даст Xvfb стартовать
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp >/dev/null 2>&1 &
i=0
while [ ! -e /tmp/.X11-unix/X99 ]; do
    i=$((i + 1))
    if [ "$i" -gt 50 ]; then
        echo "Xvfb не запустился" >&2
        exit 1
    fi
    sleep 0.1
done

if [ "$1" = "vnc-login" ]; then
    shift
    # noVNC управляет Chrome с сессией TikTok: без пароля не открываем, как и панель
    if [ "${#PANEL_PASSWORD}" -lt 12 ]; then
        echo "Вход не запущен: нужен PANEL_PASSWORD не короче 12 символов в .env" >&2
        exit 1
    fi
    # файлом, а не аргументом: аргументы видны в списке процессов
    (umask 077 && printf '%s\n' "$PANEL_PASSWORD" > /tmp/vncpass)
    x11vnc -display :99 -localhost -forever -shared -quiet -passwdfile /tmp/vncpass >/dev/null 2>&1 &
    websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
    echo "Вход в TikTok: http://127.0.0.1:6080/vnc.html (пароль VNC — первые 8 символов PANEL_PASSWORD)"
    exec python -m ttmon login "$@"
fi

exec python -m ttmon "$@"
