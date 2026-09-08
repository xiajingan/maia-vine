"""Atomic JSON/text state storage with a bounded cross-process lock."""

from __future__ import annotations

import errno
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class StateLockTimeout(TimeoutError):
    pass


class StateStore:
    def __init__(self, root: Path):
        self.root = root

    def path(self, relative: str | Path) -> Path:
        target = (self.root / relative).resolve()
        if target != self.root.resolve() and self.root.resolve() not in target.parents:
            raise ValueError(f"状态路径越界: {relative}")
        return target

    @contextmanager
    def lock(self, relative: str | Path, timeout_seconds: float = 10) -> Iterator[None]:
        lock_path = self.path(str(relative) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        owner = uuid.uuid4().hex
        ttl_seconds = max(timeout_seconds * 6, 60)
        hostname = socket.gethostname()
        while True:
            pending_path = lock_path.with_name(f".{lock_path.name}.{owner}.pending")
            try:
                pending_path.mkdir(mode=0o700)
                (pending_path / "owner.json").write_text(
                    json.dumps(
                        {
                            "owner": owner,
                            "pid": os.getpid(),
                            "hostname": hostname,
                            "acquired_at": time.time(),
                            "ttl_seconds": ttl_seconds,
                        }
                    ),
                    encoding="utf-8",
                )
                os.rename(pending_path, lock_path)
                break
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                (pending_path / "owner.json").unlink(missing_ok=True)
                try:
                    pending_path.rmdir()
                except FileNotFoundError:
                    pass
                if self._recover_abandoned_directory_lock(lock_path, hostname):
                    continue
                if time.monotonic() >= deadline:
                    raise StateLockTimeout(
                        f"状态锁超时: {lock_path}；禁止自动抢占，请确认持有者状态后执行受控恢复"
                    ) from None
                time.sleep(0.05)
        stop_heartbeat = threading.Event()

        def renew() -> None:
            while not stop_heartbeat.wait(ttl_seconds / 3):
                try:
                    current = json.loads((lock_path / "owner.json").read_text(encoding="utf-8"))
                    if current.get("owner") != owner:
                        return
                    os.utime(lock_path)
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    return

        heartbeat = threading.Thread(target=renew, name=f"state-lock-{owner[:8]}", daemon=True)
        heartbeat.start()
        try:
            yield
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1)
            try:
                current = json.loads((lock_path / "owner.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                current = {}
            if current.get("owner") == owner:
                (lock_path / "owner.json").unlink(missing_ok=True)
                try:
                    lock_path.rmdir()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _recover_abandoned_directory_lock(lock_path: Path, hostname: str) -> bool:
        """Recover only an expired lock whose local owner process is provably gone."""
        try:
            metadata = json.loads((lock_path / "owner.json").read_text(encoding="utf-8"))
            ttl_seconds = float(metadata["ttl_seconds"])
            pid = int(metadata["pid"])
            lock_host = str(metadata["hostname"])
            age = time.time() - lock_path.stat().st_mtime
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            return False
        if age <= ttl_seconds or lock_host != hostname:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError):
            return False
        else:
            return False
        quarantine = lock_path.with_name(f"{lock_path.name}.abandoned.{uuid.uuid4().hex}")
        try:
            os.replace(lock_path, quarantine)
        except (FileNotFoundError, OSError):
            return False
        (quarantine / "owner.json").unlink(missing_ok=True)
        try:
            quarantine.rmdir()
        except OSError:
            pass
        return True

    def acquire(self, relative: str | Path, owner: str, ttl_seconds: int, *, now: float | None = None) -> bool:
        """Atomically acquire an owner lock, recovering an expired lock."""
        with self.lock(f"{relative}.lease-mutation"):
            lock_path = self.path(relative)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = time.time() if now is None else now
            payload = json.dumps(
                {"owner": owner, "acquired_at": timestamp, "ttl_seconds": ttl_seconds},
                ensure_ascii=False,
            ).encode()
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(descriptor, payload)
                finally:
                    os.close(descriptor)
                return True
            except FileExistsError:
                try:
                    current = json.loads(lock_path.read_text(encoding="utf-8"))
                    acquired = float(current.get("acquired_at", 0))
                    ttl = int(current.get("ttl_seconds", ttl_seconds))
                except (OSError, ValueError, json.JSONDecodeError):
                    current, acquired, ttl = {}, 0, 0
                if timestamp - acquired < ttl:
                    return current.get("owner") == owner
                lock_path.unlink()
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(descriptor, payload)
                finally:
                    os.close(descriptor)
                return True

    def release(self, relative: str | Path, owner: str, *, force: bool = False) -> bool:
        with self.lock(f"{relative}.lease-mutation"):
            lock_path = self.path(relative)
            try:
                current = json.loads(lock_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return True
            except json.JSONDecodeError:
                current = {}
            if not force and current.get("owner") != owner:
                return False
            lock_path.unlink(missing_ok=True)
            return True

    def read_json(self, relative: str | Path, default: Any = None) -> Any:
        target = self.path(relative)
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except json.JSONDecodeError as exc:
            raise ValueError(f"状态 JSON 损坏: {target}: {exc}") from exc

    def write_json(self, relative: str | Path, value: Any) -> Path:
        return self.write_text(relative, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def write_text(self, relative: str | Path, value: str) -> Path:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(value, encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def update_json(self, relative: str | Path, update, default: Any = None) -> Any:
        with self.lock(relative):
            current = self.read_json(relative, default)
            next_value = update(current)
            self.write_json(relative, next_value)
            return next_value
