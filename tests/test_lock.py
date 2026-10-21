import subprocess
import sys
from pathlib import Path

import pytest

from ttmon.lock import AlreadyRunning, exclusive_lock

HOLD_LOCK = """
import sys, time
from pathlib import Path
from ttmon.lock import exclusive_lock
with exclusive_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(60)
"""


def test_second_holder_in_process_rejected(tmp_path: Path) -> None:
    path = tmp_path / "collect.lock"
    with exclusive_lock(path), pytest.raises(AlreadyRunning), exclusive_lock(path, wait_s=0):
        pass
    with exclusive_lock(path):
        pass


def test_lock_released_when_holder_process_killed(tmp_path: Path) -> None:
    path = tmp_path / "collect.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(AlreadyRunning), exclusive_lock(path, wait_s=0):
            pass
    finally:
        holder.kill()
        holder.wait()
        if holder.stdout is not None:
            holder.stdout.close()
    with exclusive_lock(path):
        pass
