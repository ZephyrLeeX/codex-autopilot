"""P2-T5 单实例锁测试(T-SCHED-02 / S-17)。"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from codex_autopilot.scheduling.single_instance import LockBusyError, SingleInstanceLock


class TestSingleInstanceLock:
    def test_acquire_and_release(self, tmp_path: Path):
        lock = SingleInstanceLock(tmp_path / "daemon.lock")
        assert not lock.locked
        lock.acquire()
        assert lock.locked
        lock.release()
        assert not lock.locked

    def test_second_instance_rejected(self, tmp_path: Path):
        """S-17 核心:第二个实例(独立打开的文件描述)被拒。"""

        path = tmp_path / "daemon.lock"
        first = SingleInstanceLock(path)
        first.acquire()
        second = SingleInstanceLock(path)
        with pytest.raises(LockBusyError):
            second.acquire()
        first.release()
        second.acquire()  # 释放后可获取
        second.release()

    def test_context_manager(self, tmp_path: Path):
        path = tmp_path / "daemon.lock"
        with SingleInstanceLock(path), pytest.raises(LockBusyError):
            SingleInstanceLock(path).acquire()
        SingleInstanceLock(path).acquire()  # with 退出后已释放

    def test_holder_pid_written(self, tmp_path: Path):
        path = tmp_path / "daemon.lock"
        with SingleInstanceLock(path):
            content = path.read_text().strip()
            assert content == str(__import__("os").getpid())

    def test_blocking_acquire_with_timeout(self, tmp_path: Path):
        import time

        path = tmp_path / "daemon.lock"
        with SingleInstanceLock(path):
            contender = SingleInstanceLock(path)
            started = time.monotonic()
            with pytest.raises(LockBusyError):
                contender.acquire(blocking=True, timeout=0.3)
            assert time.monotonic() - started >= 0.25

    def test_lock_directory_created(self, tmp_path: Path):
        lock = SingleInstanceLock(tmp_path / "new" / "deep" / "daemon.lock")
        lock.acquire()
        assert lock.path.is_file()
        lock.release()

    def test_cross_process_rejected(self, tmp_path: Path):
        """真实跨进程:子进程尝试取锁必须失败并报告占用人。"""

        lock_path = tmp_path / "daemon.lock"
        with SingleInstanceLock(lock_path):
            script = textwrap.dedent(
                f"""
                import sys
                from pathlib import Path
                from codex_autopilot.scheduling.single_instance import (
                    LockBusyError, SingleInstanceLock,
                )
                try:
                    SingleInstanceLock(Path({str(lock_path)!r})).acquire()
                except LockBusyError as exc:
                    print(f"BUSY pid={{exc.holder_pid}}")
                    sys.exit(21)
                print("ACQUIRED")
                sys.exit(0)
                """
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
                cwd=tmp_path,
            )
            assert result.returncode == 21  # cli-design §6:单实例锁被占
            assert "BUSY" in result.stdout
            assert result.stdout.split("pid=")[1].strip().isdigit()
