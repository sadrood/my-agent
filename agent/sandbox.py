"""
OS 级沙箱模块（Windows AppContainer，BEST_PRACTICES「下一步优先级」第 3 项）。

隔离原理（Windows 8+ AppContainer）：
1. 为本 Agent 创建一个 AppContainer 配置档案（CreateAppContainerProfile），
   得到容器 SID；进程以该 SID 的低特权令牌启动。
2. 容器内进程默认对用户文件系统 / 注册表 / 网络全部不可访问（default-deny），
   只能写显式授权的目录 —— 这里用 icacls 给工作区授予
   ALL APPLICATION PACKAGES (S-1-15-2-1) 的 modify 权限。
3. 通过 PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES 属性启动 cmd.exe，
   命令及其子进程都运行在容器内。

设计约定：
- **fail-closed**：容器创建或进程启动失败时返回错误，绝不静默回退到无沙箱执行。
- 仅前台命令走沙箱（后台命令、bg 子系统保持原路径）。
- 容器内默认无网络；需要联网的命令请关闭 SANDBOX_EXECUTION 或走审批流。
- 黑名单 / 审批门 / Guardian 在沙箱之前仍然生效（纵深防御，沙箱是最后一层）。

非 Windows 平台 appcontainer_available() 恒为 False，sandbox_enabled() 开启时
终端返回明确错误而不是明文执行。
"""
import os
import shutil
import subprocess
from dataclasses import dataclass

from config import SANDBOX_EXEC_CONFIG

_IS_WINDOWS = (os.name == "nt")

# AppContainer 内进程可写的输出重定向文件名（位于工作区内，容器可写）
_STDOUT_FILE = ".sandbox_stdout.tmp"
_STDERR_FILE = ".sandbox_stderr.tmp"


@dataclass
class SandboxOutcome:
    """沙箱执行结果。error 非空表示沙箱机制本身失败（fail-closed）。"""
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    sandboxed: bool = True


def sandbox_mode() -> str:
    """当前沙箱执行模式：off / appcontainer。"""
    return SANDBOX_EXEC_CONFIG.get("mode", "off")


def sandbox_enabled() -> bool:
    return sandbox_mode() != "off"


def appcontainer_available() -> bool:
    """当前平台是否支持 AppContainer（仅 Windows，且 userenv API 可加载）。"""
    if not _IS_WINDOWS:
        return False
    try:
        import ctypes
        ctypes.WinDLL("userenv.dll")
        return True
    except Exception:
        return False


# ============================================================
# AppContainer ctypes 绑定（懒加载，非 Windows 不触碰）
# ============================================================

PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
SECURITY_MAX_SID_SIZE = 68


def _load_apis():
    """加载 userenv/kernel32 并返回所需函数与结构体（Windows 专用）。"""
    import ctypes
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW),
                    ("lpAttributeList", ctypes.c_void_p)]

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [("AppContainerSid", ctypes.c_void_p),
                    ("Capabilities", ctypes.c_void_p),
                    ("CapabilityCount", wintypes.DWORD)]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p),
                    ("Attributes", wintypes.DWORD)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwSessionId", wintypes.DWORD)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD),
                    ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    userenv = ctypes.WinDLL("userenv.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)

    # 签名来源: userenv.h（MSDN Learn）
    # HRESULT CreateAppContainerProfile(PCWSTR name, PCWSTR display, PCWSTR desc,
    #     PSID_AND_ATTRIBUTES pCapabilities, DWORD dwCapabilityCount,
    #     PSID *ppSidAppContainerSid)
    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    userenv.CreateAppContainerProfile.restype = ctypes.HRESULT
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.HRESULT
    # 网络 capability 用 well-known SID 常量 + ConvertStringSidToSidW 构造。
    # 不用 DeriveCapabilitySidsFromName：MSDN 标注 userenv.dll，实测 Win11/Server
    # 2025 该导出在 kernelbase.dll 且按文档签名调用直接 access violation，不可信。
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.FreeSid.argtypes = [ctypes.c_void_p]
    advapi32.FreeSid.restype = ctypes.c_void_p

    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    return {
        "ctypes": ctypes,
        "wintypes": wintypes,
        "STARTUPINFOEXW": STARTUPINFOEXW,
        "SECURITY_CAPABILITIES": SECURITY_CAPABILITIES,
        "SID_AND_ATTRIBUTES": SID_AND_ATTRIBUTES,
        "PROCESS_INFORMATION": PROCESS_INFORMATION,
        "SECURITY_ATTRIBUTES": SECURITY_ATTRIBUTES,
        "userenv": userenv,
        "kernel32": kernel32,
        "advapi32": advapi32,
    }


def _ensure_container_profile(api, profile_name: str):
    """创建（或复用）AppContainer 配置档案，返回系统分配的容器 SID 指针。

    ctypes 对 restype=HRESULT 的失败结果会自动抛 OSError（winerror 即
    HRESULT 值），这里按 winerror 识别 ERROR_ALREADY_EXISTS：删除档案后
    重建。工作区授权针对 ALL APPLICATION PACKAGES 组（含所有容器 SID），
    重建不影响工作区访问。SID 指针由调用方负责 FreeSid。
    """
    ctypes = api["ctypes"]
    userenv = api["userenv"]

    container_name = profile_name[:64]
    sid_out = ctypes.c_void_p()
    args = (container_name, "my_agent sandbox", "my_agent 命令隔离沙箱")

    def _create():
        userenv.CreateAppContainerProfile(*args, None, 0, ctypes.byref(sid_out))

    try:
        _create()
    except OSError as e:
        if (getattr(e, "winerror", 0) & 0xFFFFFFFF) != 0x800700B7:
            raise   # 非"已存在"错误 → fail-closed 交上层
        try:
            userenv.DeleteAppContainerProfile(container_name)
        except OSError:
            pass
        _create()
    return sid_out


def _grant_workspace_ace(workspace: str) -> None:
    """给工作区授予 ALL APPLICATION PACKAGES 修改权限（容器内进程唯一可写区）。"""
    r = subprocess.run(
        ["icacls", workspace, "/grant", "*S-1-15-2-1:(OI)(CI)(M)"],
        capture_output=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
    )
    if r.returncode != 0:
        # icacls 输出跟随系统 ANSI 代码页（mbcs），字节捕获后解码避免编码炸裂
        msg = (r.stderr or r.stdout or b"").decode("mbcs", errors="replace")
        raise OSError(f"icacls 授权工作区失败: {msg.strip()[:200]}")


def _grant_interpreter_aces(prefix=None, base_prefix=None, which_fn=None) -> list:
    """给解释器安装目录授予 AppContainer 只读执行权限（best-effort）。

    容器内进程默认读不到用户目录下安装的程序——不授权则终端在沙箱内
    只能跑系统自带命令（实战验证发现）。用目录级 (OI)(CI)(RX) 授权靠
    继承覆盖 python313.dll / Lib / DLLs / venv 全部内容；单文件授权
    （icacls 对 .exe）实测会被拒。

    Args:
        prefix: 运行中解释器根目录（venv），缺省 sys.prefix
        base_prefix: 基础解释器安装目录，缺省 sys.base_prefix
        which_fn: 可注入的 shutil.which（测试用）

    Returns:
        授权成功的目录列表（失败/被拒的静默跳过，fail-open）
    """
    import sys as _sys
    prefix = prefix or _sys.prefix
    base_prefix = base_prefix or _sys.base_prefix
    which_fn = which_fn or shutil.which

    candidates = [prefix, base_prefix]
    for name in ("python.exe", "node.exe", "git.exe"):
        found = which_fn(name)
        if found:
            candidates.append(os.path.dirname(os.path.abspath(found)))

    granted = []
    seen = set()
    for path in candidates:
        if not path:
            continue
        key = os.path.normpath(os.path.abspath(path)).lower()
        if key in seen or not os.path.isdir(path):
            continue
        seen.add(key)
        try:
            r = subprocess.run(
                ["icacls", path, "/grant", "*S-1-15-2-1:(OI)(CI)(RX)"],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
            )
            if r.returncode == 0:
                granted.append(path)
        except Exception:
            continue
    return granted


def _grant_extra_dirs(dirs) -> list:
    """给配置清单里的额外目录授予容器只读执行权限（icacls RX，best-effort）。

    用于工作区之外的共享库 / 数据集等只读资源。工作区本身始终可写，
    不应出现在此清单中。返回授权成功的目录。
    """
    granted = []
    seen = set()
    for path in dirs:
        if not path or not os.path.isdir(path):
            continue
        key = os.path.normpath(os.path.abspath(path)).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            r = subprocess.run(
                ["icacls", path, "/grant", "*S-1-15-2-1:(OI)(CI)(RX)"],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
            )
            if r.returncode == 0:
                granted.append(path)
        except Exception:
            continue
    return granted


# 容器网络 capability：MSDN well-known SID 常量（S-1-15-3-x）。
# 不用 DeriveCapabilitySidsFromName——本机实测按文档签名调用直接 access
# violation，ConvertStringSidToSidW 从字符串构造最稳。
_NETWORK_CAPABILITY_SIDS = {
    "internetClient": "S-1-15-3-1",
    "internetClientServer": "S-1-15-3-2",
    "privateNetworkClientServer": "S-1-15-3-5",
}
SE_GROUP_ENABLED = 0x00000004


def _build_capabilities(api):
    """
    按 SANDBOX_EXEC_CONFIG 构建 capability 数组（当前仅网络开关）。

    Returns:
        (SID_AND_ATTRIBUTES 数组或 None, [sid 指针待 LocalFree 列表])
        数组为 None 表示无 capability（默认全禁网络）。
    """
    if not SANDBOX_EXEC_CONFIG.get("allow_network", False):
        return None, []
    ctypes = api["ctypes"]
    advapi32 = api["advapi32"]
    entries = []
    keepalive = []
    for name, sid_string in _NETWORK_CAPABILITY_SIDS.items():
        sid = ctypes.c_void_p()
        try:
            ok = bool(advapi32.ConvertStringSidToSidW(
                sid_string, ctypes.byref(sid)))
        except Exception:
            ok = False
        if not ok or not sid:
            continue
        keepalive.append(sid)
        entries.append(api["SID_AND_ATTRIBUTES"](sid, SE_GROUP_ENABLED))
    if not entries:
        return None, keepalive
    arr = (api["SID_AND_ATTRIBUTES"] * len(entries))(*entries)
    return arr, keepalive


def _free_capabilities(api, keepalive) -> None:
    """释放 ConvertStringSidToSidW 分配的 SID（LocalFree）。"""
    kernel32 = api["kernel32"]
    for h in keepalive:
        if h:
            try:
                kernel32.LocalFree(h)
            except Exception:
                pass


def run_appcontainer(command: str, workspace: str,
                     timeout: float = 120.0,
                     profile_name: str = "my_agent.sandbox") -> SandboxOutcome:
    """
    在 AppContainer 内执行一条 shell 命令（cmd /c）。

    Args:
        command: 待执行命令字符串（cmd 语义，与终端工具一致）
        workspace: 工作区目录（容器内进程唯一可写目录，也是命令 cwd）
        timeout: 硬超时秒数，超时终止进程
        profile_name: AppContainer 档案名（确定性 GUID 由此派生）

    Returns:
        SandboxOutcome。error 非空 = 沙箱机制失败（调用方必须 fail-closed）。
    """
    if not _IS_WINDOWS:
        return SandboxOutcome(error="AppContainer 仅支持 Windows 平台。")
    if not os.path.isdir(workspace):
        return SandboxOutcome(error=f"工作区不存在: {workspace}")

    try:
        api = _load_apis()
    except Exception as e:
        return SandboxOutcome(error=f"加载沙箱 API 失败: {e}")

    ctypes = api["ctypes"]
    wintypes = api["wintypes"]
    userenv = api["userenv"]
    kernel32 = api["kernel32"]

    out_path = os.path.join(workspace, _STDOUT_FILE)
    err_path = os.path.join(workspace, _STDERR_FILE)

    try:
        sid_out = _ensure_container_profile(api, profile_name)
        _grant_workspace_ace(workspace)
        if SANDBOX_EXEC_CONFIG.get("grant_tools", True):
            # 解释器目录 AC RX 授权（best-effort）：让容器内能跑 python/node/git
            _grant_interpreter_aces()
        # 额外只读目录清单（best-effort）：工作区之外的共享库/数据集等
        extra_dirs = SANDBOX_EXEC_CONFIG.get("grant_dirs") or []
        if extra_dirs:
            _grant_extra_dirs(extra_dirs)
    except Exception as e:
        return SandboxOutcome(error=str(e))

    # 输出重定向文件：在授权之后创建 → 继承工作区 ACE，容器内进程可写
    for p in (out_path, err_path):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    try:
        out_handle, out_fd = _open_inherit_handle(api, out_path, write=True)
        err_handle, err_fd = _open_inherit_handle(api, err_path, write=True)
        in_handle, in_fd = _open_inherit_handle(api, os.devnull, write=False)
    except Exception as e:
        return SandboxOutcome(error=f"创建输出重定向文件失败: {e}")

    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    cmdline = ctypes.create_unicode_buffer(f'"{comspec}" /d /s /c "{command}"')

    siex = api["STARTUPINFOEXW"]()
    siex.StartupInfo.cb = ctypes.sizeof(api["STARTUPINFOEXW"])
    siex.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    siex.StartupInfo.hStdInput = in_handle
    siex.StartupInfo.hStdOutput = out_handle
    siex.StartupInfo.hStdError = err_handle

    sec_caps = api["SECURITY_CAPABILITIES"]()
    sec_caps.AppContainerSid = sid_out
    sec_caps.CapabilityCount = 0
    # capability 数组（当前仅网络开关）：SID 由系统派生，keepalive 供 finally 释放
    cap_array, cap_keepalive = _build_capabilities(api)
    if cap_array is not None:
        sec_caps.Capabilities = ctypes.cast(cap_array, ctypes.c_void_p)
        sec_caps.CapabilityCount = len(cap_array)

    pi = api["PROCESS_INFORMATION"]()
    attr_size = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
    attr_buf = ctypes.create_string_buffer(attr_size.value)
    siex.lpAttributeList = ctypes.cast(attr_buf, ctypes.c_void_p)

    ok = bool(kernel32.InitializeProcThreadAttributeList(
        attr_buf, 1, 0, ctypes.byref(attr_size)))
    if not ok:
        _free_capabilities(api, cap_keepalive)
        return SandboxOutcome(error="InitializeProcThreadAttributeList 失败")

    try:
        ok = bool(kernel32.UpdateProcThreadAttribute(
            attr_buf, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(sec_caps), ctypes.sizeof(sec_caps), None, None))
        if not ok:
            return SandboxOutcome(
                error=f"UpdateProcThreadAttribute 失败: WinError={ctypes.get_last_error()}")

        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
        ok = bool(kernel32.CreateProcessW(
            None, cmdline, None, None, True, flags, None, workspace,
            ctypes.byref(siex), ctypes.byref(pi)))
        if not ok:
            return SandboxOutcome(
                error=f"AppContainer 内启动进程失败: WinError={ctypes.get_last_error()}")

        wait_ms = int(max(0.0, timeout) * 1000)
        wait_rc = kernel32.WaitForSingleObject(pi.hProcess, wait_ms)
        if wait_rc == WAIT_TIMEOUT:
            kernel32.TerminateProcess(pi.hProcess, 1)
            kernel32.WaitForSingleObject(pi.hProcess, 5000)
            return SandboxOutcome(
                returncode=1, stdout=_read_file(out_path), stderr=_read_file(err_path),
                error=f"命令执行超时（>{timeout:.0f}s），沙箱内进程已终止。")

        code = wintypes.DWORD(-1)
        kernel32.GetExitCodeProcess(pi.hProcess, code)
        return SandboxOutcome(
            returncode=int(code.value),
            stdout=_read_file(out_path),
            stderr=_read_file(err_path),
        )
    finally:
        try:
            kernel32.DeleteProcThreadAttributeList(attr_buf)
        except Exception:
            pass
        for h in (pi.hProcess, pi.hThread, out_handle, err_handle, in_handle):
            if h:
                kernel32.CloseHandle(h)
        for fd in (out_fd, err_fd, in_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        _free_capabilities(api, cap_keepalive)
        if sid_out:
            api["advapi32"].FreeSid(sid_out)
        for p in (out_path, err_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def _open_inherit_handle(api, path: str, write: bool):
    """
    打开文件作为容器内进程的 std 流，返回 (可继承句柄, fd)。
    句柄必须设置 HANDLE_FLAG_INHERIT，否则子进程拿到无效 std 句柄。
    调用方负责成对关闭（先 CloseHandle 再 close fd）。
    """
    import msvcrt
    ctypes = api["ctypes"]
    kernel32 = api["kernel32"]
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC if write else os.O_RDONLY
    fd = os.open(path, flags, 0o600)
    handle = msvcrt.get_osfhandle(fd)
    HANDLE_FLAG_INHERIT = 0x00000001
    kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
    return handle, fd


def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ============================================================
# 终端工具接入（fail-closed）
# ============================================================

def run_isolated(command: str, workspace: str) -> SandboxOutcome:
    """按配置执行沙箱命令：当前仅 appcontainer 后端，未启用直接报错。"""
    mode = sandbox_mode()
    if mode == "off":
        return SandboxOutcome(error="沙箱执行未启用（SANDBOX_EXECUTION=off）。", sandboxed=False)
    if not appcontainer_available():
        return SandboxOutcome(
            error=f"沙箱模式 {mode} 在当前平台不可用（仅支持 Windows 8+）。")
    return run_appcontainer(
        command, workspace,
        timeout=float(SANDBOX_EXEC_CONFIG.get("timeout", 120)),
        profile_name=str(SANDBOX_EXEC_CONFIG.get("profile", "my_agent.sandbox")),
    )
