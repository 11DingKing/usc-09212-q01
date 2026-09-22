"""时间工具：事件时间与服务接收时间分离。"""
from datetime import datetime, timezone


def now_utc() -> str:
    """服务端接收时间，统一 UTC ISO-8601。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_event_time(value):
    """调用方可携带业务事件时间；缺省由服务端补齐。"""
    if value in (None, ""):
        return now_utc()
    if not isinstance(value, str):
        raise ValueError("event_time 必须是 ISO-8601 字符串")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"非法 event_time: {value}") from exc
    return value
