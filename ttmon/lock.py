import contextlib
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

# Windows снимает блокировку убитого процесса не мгновенно
ACQUIRE_WAIT_SECONDS = 3.0
ACQUIRE_STEP_SECONDS = 0.2

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fh: BinaryIO) -> None:
        # locking берёт байты с текущей позиции, а режим a+ ставит её в конец файла
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: BinaryIO) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fh: BinaryIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: BinaryIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class AlreadyRunning(RuntimeError):
    pass


@contextlib.contextmanager
def exclusive_lock(path: Path, wait_s: float = ACQUIRE_WAIT_SECONDS) -> Iterator[None]:
    """Блокировка ОС (msvcrt на Windows, flock на Linux), а не «файл существует»: снимается
    сама, если процесс убит, поэтому протухших lock-файлов не бывает."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_s
    with path.open("a+b") as fh:
        while True:
            try:
                _try_lock(fh)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise AlreadyRunning(f"уже запущен другой процесс ({path})") from exc
                time.sleep(ACQUIRE_STEP_SECONDS)
        try:
            yield
        finally:
            _unlock(fh)
