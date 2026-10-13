"""QQ 机器人扫码绑定：手机 QQ 扫一扫，直接拿到 AppID/AppSecret。

腾讯轻量绑定接口三步（都不需要预置凭据）：
  create_bind_task(key) → task_id → 绑定页二维码 → poll_bind_result(task_id)
COMPLETED 时回 `bot_appid` + `bot_encrypt_secret`，后者用本地 key 做
AES-256-GCM 解出 secret。密钥只写进 .env（已在 .gitignore），不打印、不进日志。
"""
from __future__ import annotations

import base64
import io
import os
import secrets
import time
from enum import IntEnum
from typing import Callable, Dict, Optional, Tuple

BIND_HOSTS = {"production": "q.qq.com", "test": "test.q.qq.com"}
DEFAULT_SOURCE = "my_agent"
POLL_INTERVAL = 2.0
REQUEST_TIMEOUT = 10.0


class BindStatus(IntEnum):
    """poll_bind_result 的状态：0 未知 / 1 等待 / 2 完成 / 3 过期。"""

    NONE = 0
    PENDING = 1
    COMPLETED = 2
    EXPIRED = 3


def host_of(env: str = "production") -> str:
    """环境名 → 接口域名。"""
    if env not in BIND_HOSTS:
        raise ValueError(f"未知环境：{env}")
    return BIND_HOSTS[env]


def make_bind_key() -> Tuple[str, bytes]:
    """随机 32 字节绑定密钥：base64 那份发给服务端，原始字节留着解 secret。"""
    raw = secrets.token_bytes(32)
    return base64.b64encode(raw).decode(), raw


def _post_json(url: str, payload: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
    """POST JSON 并回 dict；测试里打桩这一处即可覆盖整条绑定链路。"""
    import requests  # 延迟导入：绑定是低频动作，不拖慢桥的启动

    resp = requests.post(url, json=payload, timeout=timeout,
                         headers={"Accept": "application/json"})
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {url}")
    return resp.json()


def create_bind_task(key_b64: str, env: str = "production",
                     timeout: float = REQUEST_TIMEOUT) -> str:
    """建一个待扫的绑定任务，返回 task_id。"""
    url = f"https://{host_of(env)}/lite/create_bind_task"
    data = _post_json(url, {"key": key_b64}, timeout)
    if data.get("retcode") != 0:
        raise RuntimeError(data.get("msg") or "create_bind_task 失败")
    task_id = (data.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError("create_bind_task：服务端没给 task_id")
    return str(task_id)


def build_connect_url(task_id: str, source: str = DEFAULT_SOURCE,
                      env: str = "production") -> str:
    """二维码指向的绑定页；域名固定 production——二维码是手机 QQ 扫的。"""
    from urllib.parse import quote

    return (f"https://{BIND_HOSTS['production']}/qqbot/openclaw/connect.html"
            f"?task_id={quote(str(task_id))}&source={quote(source)}&_wv=2")


def poll_bind_result(task_id: str, env: str = "production",
                     timeout: float = REQUEST_TIMEOUT) -> Dict[str, object]:
    """查任务状态。COMPLETED 时带上 app_id / encrypt_secret / user_openid。"""
    url = f"https://{host_of(env)}/lite/poll_bind_result"
    data = _post_json(url, {"task_id": task_id}, timeout)
    if data.get("retcode") != 0:
        raise RuntimeError(data.get("msg") or "poll_bind_result 失败")
    body = data.get("data") or {}
    return {
        "status": BindStatus(int(body.get("status") or 0)),
        "app_id": str(body.get("bot_appid") or ""),
        "encrypt_secret": body.get("bot_encrypt_secret") or "",
        "user_openid": body.get("user_openid") or "",
    }


def decrypt_secret(encrypt_secret_b64: str, raw_key: bytes) -> str:
    """AES-256-GCM：前 12 字节 IV、后 16 字节 tag、中间是密文。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = base64.b64decode(encrypt_secret_b64)
    iv, tag, body = blob[:12], blob[-16:], blob[12:-16]
    return AESGCM(raw_key).decrypt(iv, body + tag, None).decode("utf-8")


def qr_text(url: str) -> str:
    """二维码的终端字符画（浅色块=码点，终端里直接能扫）。"""
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def qr_png(url: str, path: str) -> str:
    """二维码存成 PNG（手机相册/截图都能扫），返回落盘路径。"""
    import qrcode

    target = os.path.abspath(path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    qrcode.make(url).save(target)
    return target


def read_env_value(env_path: str, key: str) -> str:
    """读 .env 里某键的当前值：注释行/空行跳过，没有就返回空串。"""
    if not os.path.exists(env_path):
        return ""
    with open(env_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip()
    return ""


def save_env(app_id: str, app_secret: str, env_path: str = ".env",
             extra: Optional[Dict[str, str]] = None) -> str:
    """把键写进 .env：已有行（含占位注释行）就地填，没有则追加；别处不动。"""
    want = {"QQBOT_APP_ID": app_id, "QQBOT_APP_SECRET": app_secret}
    if extra:
        want.update({k: v for k, v in extra.items() if v})
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    filled = set()
    for i, line in enumerate(lines):
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip().lstrip("#").strip()
        if key not in want or key in filled:
            continue
        if not line.lstrip().startswith("#") or not line.split("=", 1)[1].strip():
            lines[i] = f"{key}={want[key]}"      # 真值行或空占位行：就地填
            filled.add(key)
    for key, value in want.items():
        if key not in filled:
            lines.append(f"{key}={value}")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return os.path.abspath(env_path)


def bind(source: str = DEFAULT_SOURCE, env: str = "production",
         total_timeout: float = 600.0, interval: float = POLL_INTERVAL,
         on_qr: Optional[Callable[[str], None]] = None,
         on_expired: Optional[Callable[[], None]] = None,
         sleep: Callable[[float], None] = time.sleep) -> Dict[str, str]:
    """等扫码直到成功：二维码过期就换一张新的，整体超过 total_timeout 抛 TimeoutError。"""
    deadline = time.monotonic() + total_timeout
    while True:
        key_b64, raw_key = make_bind_key()
        task_id = create_bind_task(key_b64, env)
        if on_qr:
            on_qr(build_connect_url(task_id, source, env))
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("等待扫码超时")
            try:
                result = poll_bind_result(task_id, env)
            except Exception:
                sleep(interval)
                continue
            if result["status"] == BindStatus.COMPLETED:
                return {
                    "app_id": str(result["app_id"]),
                    "app_secret": decrypt_secret(str(result["encrypt_secret"]), raw_key),
                    "user_openid": str(result["user_openid"]),
                }
            if result["status"] == BindStatus.EXPIRED:
                if on_expired:
                    on_expired()
                break
            sleep(interval)


def main(argv: Optional[list] = None) -> int:
    """`python -m agent.qqbot --bind`：打印二维码，扫完把密钥写进 .env。"""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m agent.qqbot --bind",
                                     description="QQ 机器人扫码绑定")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="二维码里的来源标识")
    parser.add_argument("--env", choices=tuple(BIND_HOSTS), default="production")
    parser.add_argument("--timeout", type=float, default=600.0, help="等扫码的总秒数")
    parser.add_argument("--env-file", default=".env", help="密钥写进哪个文件")
    parser.add_argument("--png", default=os.path.join("output", "qqbot-bind-qr.png"))
    parser.add_argument("--no-write-env", action="store_true", help="只显示二维码，不落盘")
    parser.add_argument("--print-secret", action="store_true",
                        help="配合 --no-write-env：把 AppSecret 打到终端（会进日志，谨慎）")
    args = parser.parse_args(list(argv) if argv is not None else [])

    try:
        import qrcode  # noqa: F401  —— 二维码是本命令唯一的展示手段
    except ImportError:
        print("缺少依赖：pip install qrcode（生成二维码用）")
        return 2

    state = {"url": "", "png": ""}

    def on_qr(url: str) -> None:
        state["url"] = url
        state["png"] = qr_png(url, args.png)
        print(qr_text(url))
        print(f"请用手机 QQ 扫描上面的二维码完成机器人绑定（也可打开 {state['png']}）")

    def on_expired() -> None:
        print("二维码已过期，正在换一张…")

    print("正在向腾讯申请绑定任务…")
    try:
        result = bind(source=args.source, env=args.env, total_timeout=args.timeout,
                      on_qr=on_qr, on_expired=on_expired)
    except Exception as exc:                       # 失败就把原因照实说出来
        print(f"绑定失败：{exc}")
        return 1
    if args.no_write_env:
        # 不落盘就得给出口：否则这份 secret 再也看不到（同一份只回一次）
        print(f"绑定成功：AppID={result['app_id']}")
        if args.print_secret:
            print(f"AppSecret（请立即保存，别贴进聊天/日志）：{result['app_secret']}")
        else:
            print("AppSecret 未落盘也未回显；需要它请加 --print-secret 再跑一次")
    else:
        extra: Dict[str, str] = {}
        openid = str(result.get("user_openid") or "")
        if openid and not read_env_value(args.env_file, "QQBOT_ALLOW_FROM"):
            # 绑定人就是第一个使用者：顺手进白名单，省掉"发消息被拒→抄 openid"这一步。
            extra["QQBOT_ALLOW_FROM"] = openid
        save_env(result["app_id"], result["app_secret"], args.env_file, extra=extra)
        print(f"绑定成功：AppID={result['app_id']}"
              f"，AppSecret 已写入 {args.env_file}（不回显）")
        if extra:
            print(f"白名单已写入 QQBOT_ALLOW_FROM={openid}（绑定人本人）")
    print("下一步：python -m agent.qqbot 启动桥（先 pip install qq-botpy）")
    return 0


if __name__ == "__main__":      # pragma: no cover - 交互入口
    import sys as _sys
    raise SystemExit(main(_sys.argv[1:]))
