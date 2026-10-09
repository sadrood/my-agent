"""模型能力目录：上下文窗口 / 输出上限 / 视觉 / 思考档位的来源。

网关 `/models` 常常只给模型 id、不给窗口与模态，于是压缩阈值只能硬编码兜底、视觉能力
只能靠猜。这里按三级取值：**本地实测覆盖表 > 学习缓存 > models.dev 目录**，全部查不到
则返回 None 由调用方决定兜底，绝不臆造数字。
"""
import json
import os
import re
import threading
import time
import urllib.request

CATALOG_URL = "https://models.dev/api.json"
CATALOG_TTL_SECONDS = 7 * 24 * 3600
FETCH_TIMEOUT_SECONDS = 20

_LOCK = threading.RLock()
_FETCH_ATTEMPTED_AT = 0.0
_REFRESH_THREAD = None

#: 本机实测值，**按端点作用域**（同一模型名换端点窗口/模态可能不同，不能当全局事实）。
#: 只写量过的字段；没量过的不写（宁缺勿造）。
LOCAL_OVERRIDES_BY_HOST = {
    "token.sensenova.cn": {
        "deepseek-flash": {"context": 1_048_576, "output": 65_536,
                           "vision": True, "reasoning": True},
        "deepseek-v4.1-flash": {"context": 1_048_576, "output": 65_536,
                                "vision": True, "reasoning": True},
    },
    "172.16.10.242": {
        "deepseek-flash": {"vision": True},
        "gpt-5.6-terra": {"vision": True},
    },
}

FIELDS = ("context", "output", "vision", "reasoning")


def _host(base_url: str) -> str:
    return str(base_url or "").strip().lower()


def _override_for(model: str, base_url: str) -> dict:
    """取该端点的实测覆盖（host 子串匹配）。"""
    host = _host(base_url)
    for key, table in LOCAL_OVERRIDES_BY_HOST.items():
        if key in host:
            fields = table.get(str(model or "").strip().lower())
            if fields:
                return dict(fields)
    return {}


def _root() -> str:
    from config import PROJECT_ROOT
    return PROJECT_ROOT


def catalog_path() -> str:
    return os.path.join(_root(), "memory", "model_catalog.json")


def learned_path() -> str:
    return os.path.join(_root(), "memory", "model_capabilities.json")


def _norm(text: str) -> str:
    """归一化模型名：小写、去掉厂商前缀与所有分隔符（deepseek-ai/X → x）。"""
    value = str(text or "").strip().lower()
    if "/" in value:
        value = value.split("/", 1)[1]
    return re.sub(r"[\s\-_.]+", "", value)


def _tokens(text: str) -> set:
    """按分隔符切成词元（点号不切：4.5 这类版本号拆开会造成跨版本误匹配）。"""
    return {p for p in re.split(r"[\s\-_/]+", str(text or "").lower()) if p}


# ================================================================
# 目录读写
# ================================================================

def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except OSError:
        pass


def fetch_catalog(url: str = CATALOG_URL, timeout: float = FETCH_TIMEOUT_SECONDS) -> dict:
    """拉 models.dev 目录（独立出来便于测试打桩与失败降级）。

    必须带 User-Agent：默认的 Python-urllib UA 会被上游回 403。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": "my_agent/1.0 (+https://github.com/sadrood/my-agent)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def load_catalog(force: bool = False, fetcher=None) -> dict:
    """取目录：缓存未过期直接用；过期/缺失时**后台刷新**并仍返回现有缓存。

    models.dev 有 5MB 级（首次实测 40s+），同步拉会卡住当前这一轮对话，所以默认异步；
    只有 force=True（显式预热/自检）才同步等待。
    """
    global _FETCH_ATTEMPTED_AT, _REFRESH_THREAD
    with _LOCK:
        cached = _read_json(catalog_path())
        fetched_at = float(cached.get("fetched_at") or 0)
        if cached.get("models") and time.time() - fetched_at < CATALOG_TTL_SECONDS and not force:
            return cached
        if force:
            return _refresh(cached, fetcher) or cached
        # 后台刷新一次即可，别每个查询都起线程
        if time.time() - _FETCH_ATTEMPTED_AT >= 60 and (
                _REFRESH_THREAD is None or not _REFRESH_THREAD.is_alive()):
            _FETCH_ATTEMPTED_AT = time.time()
            _REFRESH_THREAD = threading.Thread(target=_refresh, args=(cached, fetcher),
                                               daemon=True, name="model-catalog-refresh")
            _REFRESH_THREAD.start()
        return cached


def _refresh(cached: dict, fetcher=None) -> dict:
    """真正去拉一次并落盘（失败保留旧缓存，绝不抛给调用方）。"""
    try:
        raw = (fetcher or fetch_catalog)()
        models = _flatten(raw)
    except Exception:                            # noqa: BLE001
        return cached
    if not models:
        return cached
    payload = {"fetched_at": time.time(), "source": CATALOG_URL, "models": models}
    _write_json(catalog_path(), payload)
    return payload


def warm(blocking: bool = True, fetcher=None) -> dict:
    """显式预热目录（启动/自检可用；blocking=False 时后台拉）。"""
    if blocking:
        return load_catalog(force=True, fetcher=fetcher)
    load_catalog(fetcher=fetcher)
    return _read_json(catalog_path())


def _flatten(raw: dict) -> dict:
    """把 models.dev 的 {provider: {models: {...}}} 摊平成 [(provider, id) → 能力]。"""
    out = {}
    for provider, block in (raw or {}).items():
        models = (block or {}).get("models") if isinstance(block, dict) else None
        if not isinstance(models, dict):
            continue
        for mid, spec in models.items():
            if not isinstance(spec, dict):
                continue
            limit = spec.get("limit") or {}
            modalities = ((spec.get("modalities") or {}).get("input")) or []
            out[f"{provider}/{mid}"] = {
                "provider": provider,
                "id": str(spec.get("id") or mid),
                "name": str(spec.get("name") or mid),
                "canonical": str(spec.get("canonical_model_id") or ""),
                "family": str(spec.get("family") or ""),
                "context": int(limit.get("context") or 0) or None,
                "output": int(limit.get("output") or 0) or None,
                "vision": bool("image" in modalities or spec.get("attachment") is True),
                "reasoning": bool(spec.get("reasoning")),
            }
    return out


# ================================================================
# 查询
# ================================================================

def _match(entry: dict, raw_name: str) -> bool:
    """目录条目是否就是这个模型（精确 → 词元全含）。"""
    target = _norm(raw_name)
    if not target:
        return False
    for candidate in (entry.get("id"), entry.get("name"), entry.get("canonical")):
        if candidate and _norm(candidate) == target:
            return True
    wanted = _tokens(raw_name)
    if not wanted:
        return False
    for candidate in (entry.get("id"), entry.get("name"), entry.get("canonical")):
        if candidate and wanted <= _tokens(candidate):
            return True
    return False


def learned_capabilities(base_url: str = "", model: str = "") -> dict:
    """学习缓存：按 (端点, 模型) 记下实测到的能力（视觉是否可用等）。"""
    store = _read_json(learned_path())
    key = f"{str(base_url or '').rstrip('/')}|{str(model or '').lower()}"
    value = store.get(key)
    return dict(value) if isinstance(value, dict) else {}


def note_capability(base_url: str, model: str, **fields) -> None:
    """实测到某能力就记下来（只收 FIELDS 里的键）。"""
    clean = {k: v for k, v in fields.items() if k in FIELDS and v is not None}
    if not model or not clean:
        return
    with _LOCK:
        store = _read_json(learned_path())
        key = f"{str(base_url or '').rstrip('/')}|{str(model).lower()}"
        entry = store.get(key)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update(clean)
        entry["updated_at"] = time.time()
        store[key] = entry
        _write_json(learned_path(), store)


def lookup(model: str, base_url: str = "") -> dict:
    """按模型名查能力：端点实测覆盖 > 学习缓存 > 目录。查不到返回 {}。"""
    name = str(model or "").strip()
    if not name:
        return {}
    out = {}
    override = _override_for(name, base_url)
    if override:
        out.update(override)
        out["source"] = "override"
    learned = learned_capabilities(base_url, name)
    for key in FIELDS:
        if key in learned and key not in out:
            out[key] = learned[key]
            out.setdefault("source", "learned")
    if all(key in out for key in FIELDS):
        return out
    catalog = load_catalog()
    entries = catalog.get("models") or {}
    if isinstance(entries, dict):
        for entry in entries.values():
            if not isinstance(entry, dict) or not _match(entry, name):
                continue
            for key in FIELDS:
                if key not in out and entry.get(key) is not None:
                    out[key] = entry[key]
                    out.setdefault("source", "catalog")
            out.setdefault("matched", f"{entry.get('provider')}/{entry.get('id')}")
            break
    return out


def context_window(model: str, base_url: str = "") -> int:
    value = lookup(model, base_url).get("context")
    try:
        return int(value) if value else 0
    except (TypeError, ValueError):
        return 0


def max_output(model: str, base_url: str = "") -> int:
    value = lookup(model, base_url).get("output")
    try:
        return int(value) if value else 0
    except (TypeError, ValueError):
        return 0


def supports_vision(model: str, base_url: str = ""):
    value = lookup(model, base_url).get("vision")
    return bool(value) if value is not None else None
