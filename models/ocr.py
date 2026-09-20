"""本地 OCR：从截图/图片里取文字，**不经过视觉大模型**。

为什么需要它（用户原话）："agent 还缺少截图识别文字的能力，总是依赖视觉模型，
有的时候视觉模型无响应，就废了。"
—— 视觉模型（多模态 LLM）能理解版面与语义，但它有超时、有配额、有"这个模型不支持
图片输入"的坑；而这些时候**文字本身是能拿到的**：系统里就有 OCR 引擎。

后端优先级（自动探测，谁可用用谁）：
  1. `windows`  —— Windows.Media.Ocr（Win10/11 自带，支持中文，零安装；经 PowerShell
     WinRT 桥调用，实测在本机可用：zh-Hans-CN）
  2. `rapidocr` —— rapidocr-onnxruntime（跨平台，pip 安装后自动启用）
  3. `tesseract`—— pytesseract + tesseract 可执行文件（装了才用）
全都不可用时给出明确的安装提示，而不是静默失败。

输出做了**中文空格归一**：Windows OCR 会把"系统提示"识别成"系 统 提 示"
（每个汉字之间插空格），照原样回给模型会很难读，也影响后续检索/比对。
"""
import base64
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional

from config import OCR_CONFIG, resolve_under_root

#: Windows OCR 的 PowerShell 桥。写成脚本文件（utf-8-sig）而不是 -Command，
#: 避免引号/换行在命令行里被再解析一遍。
#: 结果用 WriteAllText 落 UTF-8 文件再由 Python 读——不经过控制台代码页，
#: 中文不会在管道里变成乱码。
_PS_BRIDGE = r'''
param([string]$Image, [string]$Out, [string]$Lang)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $asTaskGeneric.MakeGenericMethod($type)
    $t.Invoke($null, @($op)).GetAwaiter().GetResult()
}
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Image)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = $null
if ($Lang) {
    foreach ($one in $Lang.Split(',')) {
        $l = $one.Trim()
        if (-not $l) { continue }
        try {
            $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
                (New-Object Windows.Globalization.Language $l))
        } catch { $engine = $null }
        if ($null -ne $engine) { break }
    }
}
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) {
    [System.IO.File]::WriteAllText($Out, 'NO_ENGINE', (New-Object System.Text.UTF8Encoding($false)))
    exit 2
}
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = @()
foreach ($line in $result.Lines) { $lines += $line.Text }
$text = ($lines -join "`n")
[System.IO.File]::WriteAllText($Out, $text, (New-Object System.Text.UTF8Encoding($false)))
'''


class OcrError(RuntimeError):
    """OCR 失败（后端不可用 / 识别异常 / 超时）。"""


#: 中文标点（用于空格归一：汉字与这些符号之间的空格也该去掉）
_CJK = r"\u4e00-\u9fff"
_CJK_PUNCT = "，。：；！？、）》」』】…—“”‘’％．"
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])")
_SPACE_CJK_PUNCT = re.compile(rf"(?<=[{_CJK}])[ \t]+(?=[{re.escape(_CJK_PUNCT)}])")
_SPACE_PUNCT_CJK = re.compile(rf"(?<=[{re.escape(_CJK_PUNCT)}])[ \t]+(?=[{_CJK}])")
#: 数字/字母 与 中文标点 之间也去掉空格（"87 ％" → "87％"）；
#: 半角标点不受影响，所以 "Error: connection refused" 里的空格照旧保留。
_SPACE_ASCII_PUNCT = re.compile(rf"(?<=[0-9A-Za-z%])[ \t]+(?=[{re.escape(_CJK_PUNCT)}])")
_SPACE_PUNCT_ASCII = re.compile(rf"(?<=[{re.escape(_CJK_PUNCT)}])[ \t]+(?=[0-9A-Za-z])")
#: 数字里的小数点：Windows OCR 会读成全角「．」或间隔号「·」并加空格
#: （"98 ． 7％"），对齐成半角小数点更好用，也便于后续比对/检索。
_NUM_DOT = re.compile(r"(?<=\d)[ \t]*[．·。][ \t]*(?=\d)")
_NUM_PERCENT = re.compile(r"(?<=\d)[ \t]*％")


def normalize_ocr_text(raw: str) -> str:
    """归一化 OCR 输出：数字写法 + 中文空格。

    只动 CJK 相邻的空格与"数字里的小数点/百分号"——英文单词之间的空格必须保留
    （"connection refused" 不能被粘成一坨）。

    注意顺序：**先**把「．％」对齐成半角，**再**去空格——否则 "98.7％ ，"
    里的百分号已经变成 `%`，空格规则的前导字符类就匹配不到它了。
    """
    text = raw or ""
    text = _NUM_DOT.sub(".", text)
    text = _NUM_PERCENT.sub("%", text)
    text = _SPACE_BETWEEN_CJK.sub("", text)
    text = _SPACE_CJK_PUNCT.sub("", text)
    text = _SPACE_PUNCT_CJK.sub("", text)
    text = _SPACE_ASCII_PUNCT.sub("", text)
    text = _SPACE_PUNCT_ASCII.sub("", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(lines).strip()


@dataclass
class OcrResult:
    """一次识别的结果。"""
    text: str                      # 归一化后的文本（给人/模型看）
    raw_text: str = ""             # 引擎原样输出（排查用）
    engine: str = ""
    seconds: float = 0.0
    image: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def lines(self) -> int:
        return len([ln for ln in self.text.splitlines() if ln.strip()])

    def summary(self) -> str:
        head = f"OCR 引擎: {self.engine} | {self.lines} 行 / {self.chars} 字 | {self.seconds:.1f}s"
        if self.warnings:
            head += " | " + "；".join(self.warnings)
        return head


class OcrEngine:
    """本地 OCR 引擎（按可用性自动挑后端）。"""

    BACKENDS = ("windows", "rapidocr", "tesseract")

    def __init__(self, backend: str = None, languages: str = None,
                 timeout: float = None, config: dict = None):
        cfg = dict(OCR_CONFIG if config is None else config)
        self.config = cfg
        self.backend = (backend or cfg.get("backend") or "").strip().lower()
        self.languages = languages or cfg.get("languages") or "zh-Hans-CN,en-US"
        self.timeout = float(timeout if timeout is not None else cfg.get("timeout", 60))
        # 放大倍数：实测（20px 图 83%→93%、34px 图 87%→89%）2 倍明显更准，
        # 3 倍对小字不再提升、还会把英文单词切碎（"refu sed"），所以默认 2。
        try:
            self.scale = max(1, int(cfg.get("scale", 2) or 1))
        except (TypeError, ValueError):
            self.scale = 2
        self._ps_script = ""

    # ---------------- 后端探测 ----------------

    @staticmethod
    def _windows_available() -> bool:
        if os.name != "nt":
            return False
        return bool(_which("powershell") or _which("pwsh"))

    @staticmethod
    def _rapidocr_available() -> bool:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return True
        except Exception:                       # noqa: BLE001
            try:
                import rapidocr  # noqa: F401
                return True
            except Exception:                   # noqa: BLE001
                return False

    @staticmethod
    def _tesseract_available() -> bool:
        if not _which("tesseract"):
            return False
        try:
            import pytesseract  # noqa: F401
            return True
        except Exception:                       # noqa: BLE001
            return False

    def available_backends(self) -> List[str]:
        checks = {
            "windows": self._windows_available,
            "rapidocr": self._rapidocr_available,
            "tesseract": self._tesseract_available,
        }
        return [b for b in self.BACKENDS if checks[b]()]

    def resolve_backend(self) -> str:
        """挑一个能用的后端；显式指定但不可用时给出明确错误。"""
        if self.backend and self.backend not in self.BACKENDS:
            raise OcrError(f"未知 OCR 后端 {self.backend!r}（可用: {', '.join(self.BACKENDS)}）")
        if self.backend:
            if self.backend in self.available_backends():
                return self.backend
            raise OcrError(
                f"指定的 OCR 后端 {self.backend!r} 不可用。可用: "
                f"{', '.join(self.available_backends()) or '（无）'}")
        usable = self.available_backends()
        if not usable:
            raise OcrError(
                "没有可用的本地 OCR 后端。任选一种启用：\n"
                "  · Windows：用系统自带 OCR（Win10/11 默认可用，无需安装）\n"
                "  · pip install rapidocr-onnxruntime（跨平台，带中英文模型）\n"
                "  · 安装 tesseract 可执行文件 + pip install pytesseract")
        return usable[0]

    # ---------------- 识别 ----------------

    def recognize(self, image_path: str) -> OcrResult:
        """识别一张图片（本地文件路径）。"""
        path = resolve_under_root(image_path) if image_path else ""
        if not path or not os.path.isfile(path):
            raise OcrError(f"找不到图片: {image_path}")
        backend = self.resolve_backend()
        t0 = time.time()
        warnings: List[str] = []
        prepared, tmp = self._maybe_upscale(path, warnings)
        try:
            if backend == "windows":
                raw = self._run_windows(prepared)
            elif backend == "rapidocr":
                raw = self._run_rapidocr(prepared)
            else:
                raw = self._run_tesseract(prepared)
        finally:
            if tmp:
                try:
                    os.remove(prepared)
                except OSError:
                    pass
        text = normalize_ocr_text(raw)
        if raw and not text:
            warnings.append("识别结果全被归一化清空（请检查图片是否有文字）")
        return OcrResult(text=text, raw_text=raw, engine=backend,
                         seconds=round(time.time() - t0, 2), image=path,
                         warnings=warnings)

    def _maybe_upscale(self, path: str, warnings: List[str]):
        """按配置放大图片再识别（返回 (实际路径, 临时文件路径或空)）。

        实测小字号截图放大 2 倍后准确率明显上升；放大的失败一律忽略——
        识别本身不该因为"想更准一点"而失败。
        """
        if self.scale <= 1:
            return path, ""
        try:
            from PIL import Image
            with Image.open(path) as im:
                big = im.resize((im.width * self.scale, im.height * self.scale),
                                Image.LANCZOS)
                fd, tmp = tempfile.mkstemp(prefix="myagent_ocr_up_", suffix=".png")
                os.close(fd)
                big.save(tmp)
            return tmp, tmp
        except Exception as e:                  # noqa: BLE001
            warnings.append(f"放大 {self.scale}x 失败，按原图识别（{str(e)[:60]}）")
            return path, ""

    def recognize_base64(self, b64: str) -> OcrResult:
        """识别 base64 图片（视觉链路里拿到的就是 base64）。

        落一个临时文件给后端用，识别完立即删除（项目规则：不留临时文件）。
        """
        data = (b64 or "").strip()
        if not data:
            raise OcrError("空图片数据")
        if "," in data[:64] and data.lstrip().startswith("data:"):
            data = data.split(",", 1)[1]          # 容忍 data URI 前缀
        try:
            blob = base64.b64decode(data, validate=False)
        except Exception as e:                    # noqa: BLE001
            raise OcrError(f"base64 解码失败: {str(e)[:120]}") from e
        fd, tmp = tempfile.mkstemp(prefix="myagent_ocr_", suffix=".png")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            result = self.recognize(tmp)
            result.image = "(base64 输入)"
            return result
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # ---------------- 各后端实现 ----------------

    def _run_windows(self, path: str) -> str:
        """Windows.Media.Ocr（PowerShell WinRT 桥）。"""
        if not self._ps_script:
            fd, script = tempfile.mkstemp(prefix="myagent_ocr_", suffix=".ps1")
            with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
                f.write(_PS_BRIDGE)
            self._ps_script = script
        out_fd, out_file = tempfile.mkstemp(prefix="myagent_ocr_", suffix=".txt")
        os.close(out_fd)
        shell = _which("powershell") or _which("pwsh")
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", self._ps_script, "-Image", path, "-Out", out_file,
                 "-Lang", self.languages],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout,
            )
            text = ""
            try:
                with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                pass
            if text.strip() == "NO_ENGINE":
                raise OcrError(
                    "Windows OCR 没有可用识别语言（设置 → 时间和语言 → 语言 → 添加中文/英文的"
                    "「基本输入」可选功能），或改用 OCR_BACKEND=rapidocr")
            if proc.returncode != 0 and not text.strip():
                raise OcrError(f"Windows OCR 失败（exit={proc.returncode}）: "
                               f"{(proc.stderr or '')[:200]}")
            return text
        except subprocess.TimeoutExpired as e:
            raise OcrError(f"Windows OCR 超时（{self.timeout:.0f}s）") from e
        finally:
            try:
                os.remove(out_file)
            except OSError:
                pass

    def _run_rapidocr(self, path: str) -> str:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception:                       # noqa: BLE001
            from rapidocr import RapidOCR       # 新版包名
        engine = _rapid_cache_get()
        if engine is None:
            engine = RapidOCR()
            _rapid_cache_set(engine)
        result, _elapse = engine(path)
        lines: List[str] = []
        for item in (result or []):
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                lines.append(str(item[1]))
        return "\n".join(lines)

    def _run_tesseract(self, path: str) -> str:
        import pytesseract
        from PIL import Image

        langs = ",".join(l.split("-")[0] for l in self.languages.split(",") if l.strip())
        try:
            return pytesseract.image_to_string(Image.open(path), lang=langs or "chi_sim+eng")
        except Exception as e:                  # noqa: BLE001
            raise OcrError(
                f"tesseract 识别失败: {str(e)[:150]}（若提示缺少语言包，"
                f"请安装 chi_sim/eng traineddata）") from e

    def languages_available(self) -> List[str]:
        """Windows OCR 当前可用的识别语言（其它后端返回空）。"""
        shell = _which("powershell") or _which("pwsh")
        if not shell or os.name != "nt":
            return []
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-Command",
                 "[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] > $null; "
                 "[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | "
                 "ForEach-Object { $_.LanguageTag }"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30)
            return [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
        except Exception:                       # noqa: BLE001
            return []


_RAPID_CACHE: dict = {}


def _rapid_cache_get():
    return _RAPID_CACHE.get("engine")


def _rapid_cache_set(engine) -> None:
    _RAPID_CACHE["engine"] = engine


def _which(name: str) -> str:
    from shutil import which
    return which(name) or ""


def ocr_available() -> bool:
    """当前环境是否有任何可用的本地 OCR 后端（用于提示与降级判断）。"""
    try:
        return bool(OcrEngine().available_backends())
    except Exception:                           # noqa: BLE001
        return False


def recognize_image(image_path: str = "", image_base64: str = "") -> OcrResult:
    """便捷入口：路径或 base64 二选一。"""
    engine = OcrEngine()
    if image_base64:
        return engine.recognize_base64(image_base64)
    return engine.recognize(image_path)


def auto_fallback_enabled() -> bool:
    return bool(OCR_CONFIG.get("enabled", True)) and bool(OCR_CONFIG.get("auto_fallback", True))
