"""事件存储：追加写、JSONL 快照恢复、文件 fsync。

所有状态变更都先序列化为不可变事件，追加到事件日志；
服务重启时重放事件即可还原每轮推荐与洽谈状态。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Callable, Iterable


class EventStore:
    def __init__(self, path: str | None = None):
        self._path = path
        self._lock = threading.RLock()
        self._file = None
        self._seq = 0
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._file = open(path, "a+", encoding="utf-8")

    @property
    def lock(self) -> threading.RLock:
        """供需要"读取-判定-追加"原子性的调用方使用的重入锁。"""
        return self._lock

    def append(self, event_type: str, payload: dict) -> dict:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "type": event_type, "payload": payload}
            if self._file is not None:
                self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._file.flush()
                os.fsync(self._file.fileno())
            return event

    def replay(self, handler: Callable[[str, dict], None]) -> int:
        """按序重放全部事件，返回事件数。仅在启动时调用。"""
        if not self._path or not os.path.exists(self._path):
            return 0
        count = 0
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                handler(event["type"], event["payload"])
                self._seq = event["seq"]
                count += 1
        return count

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def all_events(self) -> Iterable[dict]:
        """测试/导出用：读取全部已持久化事件。"""
        if not self._path or not os.path.exists(self._path):
            return []
        with open(self._path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
