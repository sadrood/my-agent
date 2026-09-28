"""LLM 健康遥测：按端点/模型累计限流与延迟，落一份 JSON 供事后读。
上游不公开 rpm/tpm，限流行为只能自己攒：延迟、429 类型、等待与恢复耗时。"""
import io
import json
import os
import time
from datetime import datetime

from config import PROJECT_ROOT

#: 默认落盘位置（memory/ 已被 .gitignore 忽略）
DEFAULT_PATH = os.path.join(PROJECT_ROOT, "memory", "llm_health.json")
MAX_EVENTS = 300            # 事件环形缓冲上限
MAX_RECOVERY_SAMPLES = 50   # 每端点保留的恢复时长样本数


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _limit_kind(message: str) -> str:
    """把上游文案归类：分钟窗口 vs 套餐额度打空（两者恢复尺度差很多）。"""
    msg = str(message or "").lower()
    if "tpm exhausted" in msg or "quota" in msg:
        return "tpm_exhausted"
    return "rpm_tpm_limit"


class LLMHealth:
    """累计每个端点/模型的调用、延迟与限流事实，并可原子落盘。"""

    def __init__(self, path: str = None, max_events: int = MAX_EVENTS):
        self.path = os.path.abspath(path or DEFAULT_PATH)
        self.max_events = int(max_events)
        self.labels: dict = {}
        self.events: list = []
        self._limited_since: dict = {}
        self._waits_while_limited: dict = {}
        self._dirty = False
        self._load()

    # ------------------------------------------------------------ 落盘
    def _load(self) -> None:
        try:
            with io.open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.labels = data.get("labels") or {}
            self.events = (data.get("events") or [])[-self.max_events:]
            # 限流开始时刻要跨进程恢复：否则重启后即使恢复了也算不出"等了多久"
            for label, rec in self.labels.items():
                since = str(((rec.get("limited") or {}).get("since")) or "").strip()
                if not since:
                    continue
                try:
                    self._limited_since[label] = datetime.fromisoformat(since).timestamp()
                except ValueError:
                    pass
                self._waits_while_limited[label] = int(rec.get("waits_while_limited") or 0)

    def flush(self) -> bool:
        """原子写（临时文件 + os.replace），失败不影响主流程。"""
        if not self._dirty:
            return False
        payload = {"updated_at": _now(), "labels": self.labels,
                   "events": self.events[-self.max_events:]}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._dirty = False
            return True
        except OSError:
            return False

    def snapshot(self) -> dict:
        return {"updated_at": _now(), "labels": self.labels,
                "events": self.events[-self.max_events:]}

    # ------------------------------------------------------------ 记录
    def _label(self, event: dict) -> dict:
        key = str(event.get("label") or event.get("model") or "unknown")
        rec = self.labels.setdefault(key, {
            "model": event.get("model") or "",
            "base_url": event.get("base_url") or "",
            "calls": 0, "ok": 0, "errors": 0,
            "latency_seconds": {"count": 0, "min": None, "max": None, "avg": None, "last": None},
            "quota_limited": {"rpm_tpm_limit": 0, "tpm_exhausted": 0, "total": 0},
            "wait_seconds_total": 0.0,
            "waits_while_limited": 0,
            "limited": {"since": "", "longest_seconds": 0.0, "recovered_after_seconds": []},
            "switches_in": 0, "switches_out": 0,
        })
        if event.get("model"):
            rec["model"] = event["model"]
        if event.get("base_url"):
            rec["base_url"] = event["base_url"]
        return rec

    def record(self, event: dict) -> None:
        """事件：call_ok / quota_limited / recovered / switch。"""
        kind = str(event.get("event") or "")
        label = str(event.get("label") or event.get("model") or "unknown")
        rec = self._label(event)
        now = time.time()
        if kind == "call_ok":
            secs = float(event.get("seconds") or 0.0)
            rec["calls"] += 1
            rec["ok"] += 1
            stat = rec["latency_seconds"]
            stat["count"] += 1
            stat["last"] = round(secs, 3)
            stat["min"] = round(secs, 3) if stat["min"] is None else min(stat["min"], round(secs, 3))
            stat["max"] = round(secs, 3) if stat["max"] is None else max(stat["max"], round(secs, 3))
            stat["avg"] = round((stat["avg"] or 0.0) + (secs - (stat["avg"] or 0.0)) / stat["count"], 3)
            if label in self._limited_since:
                lasted = round(now - self._limited_since.pop(label), 1)
                limited = rec["limited"]
                limited["recovered_after_seconds"] = (
                    limited["recovered_after_seconds"] + [lasted])[-MAX_RECOVERY_SAMPLES:]
                limited["longest_seconds"] = max(limited["longest_seconds"], lasted)
                limited["since"] = ""
                rec["waits_while_limited"] = self._waits_while_limited.pop(label, 0)
                self.events.append({"at": _now(), "event": "recovered", "label": label,
                                    "limited_seconds": lasted})
        elif kind == "quota_limited":
            rec["errors"] += 1
            bucket = _limit_kind(event.get("message"))
            rec["quota_limited"][bucket] += 1
            rec["quota_limited"]["total"] += 1
            wait = float(event.get("wait") or 0.0)
            rec["wait_seconds_total"] = round(rec["wait_seconds_total"] + wait, 1)
            if label not in self._limited_since:
                self._limited_since[label] = now
                rec["limited"]["since"] = _now()
            self._waits_while_limited[label] = self._waits_while_limited.get(label, 0) + 1
            self.events.append({"at": _now(), "event": "quota_limited", "label": label,
                                "kind": bucket, "wait": wait})
        elif kind == "switch":
            rec["switches_in"] += 1 if not event.get("primary") else 0
            self.events.append({"at": _now(), "event": "switch", "label": label,
                                "reason": event.get("reason", ""),
                                "primary": bool(event.get("primary"))})
        elif kind == "call_error":
            rec["calls"] += 1
            rec["errors"] += 1
            self.events.append({"at": _now(), "event": "call_error", "label": label,
                                "message": str(event.get("message") or "")[:120]})
        else:
            return
        self._dirty = True
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]
        if kind in ("quota_limited", "recovered", "switch"):
            self.flush()          # 关键时刻立刻落盘，别等进程退出


def render(snapshot: dict) -> str:
    """给 CLI 的一页摘要：每个端点的调用、延迟、限流次数与恢复时长。"""
    labels = snapshot.get("labels") or {}
    if not labels:
        return "还没有限流/调用数据（memory/llm_health.json 为空）。"
    lines = ["端点/模型 | 调用 | 失败 | 延迟(均/最小/最大 s) | 限流(分钟窗口/额度空) | 等待合计 s | 恢复样本"]
    for key, rec in sorted(labels.items()):
        lat = rec.get("latency_seconds") or {}
        q = rec.get("quota_limited") or {}
        limited = rec.get("limited") or {}
        samples = limited.get("recovered_after_seconds") or []
        if samples:
            ordered = sorted(samples)
            mid = ordered[len(ordered) // 2]
            recover = "中位 %.0fs/最长 %.0fs (%d 次)" % (mid, limited.get("longest_seconds", 0), len(samples))
        elif limited.get("since"):
            recover = "限流中（自 %s）" % limited.get("since")
        else:
            recover = "—"
        lines.append("%s | %d | %d | %s/%s/%s | %d/%d | %.0f | %s" % (
            key, rec.get("calls", 0), rec.get("errors", 0),
            lat.get("avg", "—"), lat.get("min", "—"), lat.get("max", "—"),
            q.get("rpm_tpm_limit", 0), q.get("tpm_exhausted", 0),
            rec.get("wait_seconds_total", 0.0), recover))
    events = snapshot.get("events") or []
    if events:
        lines.append("")
        lines.append("最近事件：")
        for e in events[-10:]:
            lines.append("  %s %s %s" % (e.get("at", ""), e.get("event", ""),
                                         {k: v for k, v in e.items() if k not in ("at", "event")}))
    return "\n".join(lines)
