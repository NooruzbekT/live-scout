import contextlib
import msvcrt
import time
from collections.abc import Iterator
from pathlib import Path

# Windows снимает блокировку убитого процесса не мгновенно
ACQUIRE_WAIT_SECONDS = 3.0
ACQUIRE_STEP_SECONDS = 0.2


class AlreadyRunning(RuntimeError):
    pass


@contextlib.contextmanager
def exclusive_lock(path: Path, wait_s: float = ACQUIRE_WAIT_SECONDS) -> Iterator[None]:
    """Блокировка ОС на байт файла, а не «файл существует»: снимается сама,
    если процесс убит, поэтому протухших lock-файлов не бывает."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_s
    with path.open("a+b") as fh:
        while True:
            # locking берёт байты с текущей позиции, а режим a+ ставит её в конец файла
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise AlreadyRunning(f"уже запущен другой процесс ({path})") from exc
                time.sleep(ACQUIRE_STEP_SECONDS)
        try:
            yield
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
