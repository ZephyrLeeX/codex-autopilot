"""基于 flock 的单实例锁(persistence.md §2/D-PER-5,场景 S-17,P2-T5)。

- 锁文件:`$state_dir/daemon.lock`;守护进程 `run` 启动即取
  `flock(LOCK_EX|LOCK_NB)`,失败退出码 21;`tick`/`check` 在评估期间短暂持锁;
- 锁的权威是 flock(关联打开文件描述);文件内容(持有者 pid)仅为诊断信息;
- `O_CLOEXEC`:锁不随 `codex queue` 子进程继承——父进程崩溃后子进程存活
  不得延续锁;
- 仅支持本地文件系统,不保证跨 NFS(persistence.md §2 已注明)。
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path
from typing import Self


class LockBusyError(RuntimeError):
    """锁已被其他持有者占用(场景 S-17;CLI 映射退出码 21)。"""

    def __init__(self, path: Path, holder_pid: int | None = None):
        self.path = path
        self.holder_pid = holder_pid
        holder = f" (held by pid {holder_pid})" if holder_pid else ""
        super().__init__(f"another instance holds the lock: {path}{holder}")


def _read_holder_pid(fd: int) -> int | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        content = os.read(fd, 64).decode("ascii", errors="replace").strip()
        return int(content) if content.isdigit() else None
    except OSError:
        return None


class SingleInstanceLock:
    """进程级独占锁;不可重入(同进程二次 acquire 同样失败,与跨进程一致)。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def locked(self) -> bool:
        return self._fd is not None

    def acquire(self, *, blocking: bool = False, timeout: float | None = None) -> None:
        """取锁。非阻塞失败或超时抛 LockBusyError。

        `timeout` 仅在轮询等待时生效(与 blocking=True 搭配);默认不等待。
        """

        if self._fd is not None:
            raise LockBusyError(self.path, os.getpid())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            if blocking:
                deadline = None if timeout is None else time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                        if deadline is not None and time.monotonic() >= deadline:
                            raise LockBusyError(self.path, _read_holder_pid(fd)) from exc
                        time.sleep(0.05)
            else:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN):
                        raise LockBusyError(self.path, _read_holder_pid(fd)) from exc
                    raise
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        # 诊断信息(非权威):记录持有者 pid。
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
