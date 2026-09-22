"""仅追加事件存储与重放。

三类日志文件：
- events.jsonl         公开业务事件（需求、供应商、响应、轮次、撮合、洽谈）
- qualifications.jsonl 采购方实名资格（与公开事件分离保存，可单独授权访问）
- audit.jsonl          人工操作留痕（不可覆盖、不可删除）

每条记录结构：
{"seq": n, "type": ..., "event_time": ..., "received_at": ..., "payload": {...}}
"""
import json
import os
import threading


class EventStore:
    def __init__(self, directory):
        self.directory = directory
        if self.directory:
            os.makedirs(self.directory, exist_ok=True)
        # 文件追加串行锁，只保护写文件与序号分配。
        self._file_lock = threading.Lock()
        # 无目录（:memory:）时的内存缓冲，保证重放语义与落盘模式一致。
        self._memory = {name: [] for name in self._log_names()}
        self._next_seq = {}
        for name in self._log_names():
            path = self._path(name)
            seq = 0
            if path:
                # 预创建三类日志，使实名/公开/审计的物理分离在空库时也可见。
                open(path, "a", encoding="utf-8").close()
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            seq = max(seq, json.loads(line)["seq"])
            self._next_seq[name] = seq + 1

    @staticmethod
    def _log_names():
        return ("events", "qualifications", "audit")

    def _path(self, name):
        return None if not self.directory else os.path.join(self.directory, f"{name}.jsonl")

    def append(self, log, event_type, payload, event_time, received_at):
        """加锁追加单条事件，返回事件序号。崩溃时整行要么完整写入要么缺失。"""
        with self._file_lock:
            seq = self._next_seq[log]
            record = {
                "seq": seq,
                "type": event_type,
                "event_time": event_time,
                "received_at": received_at,
                "payload": payload,
            }
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            path = self._path(log)
            if path:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            else:
                self._memory[log].append(record)
            self._next_seq[log] = seq + 1
            return seq

    def replay(self, log):
        """逐行按序读取日志（落盘即按序，重启后逐行重放）。"""
        path = self._path(log)
        if not path:
            yield from self._memory[log]
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
