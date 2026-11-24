"""AI 离线翻译插件（Hy-MT2 / llama.cpp）—— **STALKER Localization Toolkit 2.x 专用**

为 2.x（C++/Qt）提供「AI 翻译」栏目。插件采用目录形态：
  * **目录插件** `plugins/ai_translate/plugin.py`（宿主按目录名作 owner/id），
    随包数据在 `plugins/ai_translate/assets/`；
  * 界面只用 `api.ui`（Qt 门面）。**宿主负责排版**：控件按创建顺序落进页面
    纵向布局；`ui.row()` 造出来的是 QHBoxLayout 容器，宿主的 `place()` 接不上
    ⇒ 本插件不自己造横向行，需要成组的"标签 + 输入框"由宿主的字段行规则
    自动合并（顺带获得拖文件进输入框的能力）；
  * **不导入 `urllib.request` / `http.client` / `ssl`**：2.x 发行包不随 `_ssl.pyd`，
    这些模块连 `http://127.0.0.1` 都会在 import 阶段失败。网络统一走内嵌的
    WinHTTP（ctypes + 系统 winhttp.dll），见「HTTP（WinHTTP）」一节。

主要功能：
  * 两档本地离线模型：Hy-MT2-7B Q4_K_M（约 4.4 GB）/ Hy-MT2-1.8B Q8_0（约 1.8 GB）
  * 模型与 llama.cpp 运行库均不内置：插件内下载管理，自动探测网络环境、
    并发实测各镜像下载速度、自动选择最优源；支持断点续传 / 取消 / 失败换源
  * 本地拉起 llama-server（OpenAI 兼容 HTTP），子进程与主程序隔离
  * 翻译 X-Ray 文本 XML / cfgxml / ltx / script，支持术语表、译文复用、
    临时断点、冗余 ID 报告

线程纪律（2.x 硬约束）：`api.ui.*` 只许在宿主 UI 线程调用，其它线程调用会
**返回 None + 记 Warn**（不抛异常）。本插件的做法是：工作线程只碰
`api.log`（线程安全）与自己的队列，控件读写一律经 `ui_call()` 投递回 UI 线程；
跨线程需要的控件值预先缓存进 `self.sel` / `self.flags` / `self.cfg`。

第三方来源：X-Ray 解析器（assets/xray_pkg.zip）与
翻译流程参考 wzyddg/stalker_auto-trans-tool_remake（上游未声明
许可证）；详见仓库 THIRD_PARTY_NOTICES.md 与随包
THIRD_PARTY_NOTICES.txt。
"""

import os
import sys
import re
import json
import time
import io
import csv
import codecs
import hashlib
import socket
import ctypes
import queue
import threading
import subprocess
import zipfile
import tempfile
import shutil
import atexit
import urllib.parse

PLUGIN_INFO = {
    "id": "ai_translate",
    "name": "AI 离线翻译（Hy-MT2 / 自选模型）",
    "version": "2.0.27",
    "repo": "https://github.com/qy0214/stalker-localization-toolkit-ai-translate-2x",
    "description": "本地离线模型翻译 X-Ray 文本；解压即用、零环境依赖；"
                   "混元两档内置模型自动测速下载；支持自选任意 GGUF 模型；"
                   "CPU / CUDA 12.4 / CUDA 13.4 / Vulkan 四后端并自动检测显卡；"
                   "内置无限制强制术语库",
}

# 宿主插件 API 版本（2.x `kPyHostApiVersion = 1`）
PLUGIN_API = 1

APP_NAME = "ai_translate"


_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSETS = os.path.join(_HERE, "assets")
_VENDOR_ZIP = os.path.join(_ASSETS, "xray_pkg.zip")
_SHIPPED_BIN_DIR = os.path.join(_ASSETS, "bin")
_SHIPPED_MODELS_DIR = os.path.join(_ASSETS, "models")


def _writable(path):
    """目录是否可写（Program Files 等只读安装位置会失败）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "wb") as f:
            f.write(b"1")
        os.remove(probe)
        return True
    except Exception:
        return False


# ── 运行数据落点 ────────────────────────────────────────────────
# 2.x 的插件目录是**被宿主管理**的（启用/禁用/刷新/更新），把运行数据写在
# 插件目录里有两类真实代价：
#   1) 更新/重装插件时整个目录可能被替换 ⇒ 已下载的 4.4 GB 模型白下；
#   2) 插件目录一旦被写脏，分享包就不再是"干净可分发"的（config.json、
#      __pycache__、models/ 都会混进去）。
# 所以默认落 `%LOCALAPPDATA%`；确实想要自包含（U 盘 / 绿色版）时，在
# `plugin.py` 同目录放一个空的 `portable.txt` 即可切回"数据跟着插件走"。
_PORTABLE_MARK = os.path.join(_HERE, "portable.txt")
_LOCAL_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "STALKER Localization Toolkit", "ai_translate")

if os.path.isfile(_PORTABLE_MARK) and _writable(_ASSETS):
    _DATA_ROOT = _ASSETS
else:
    _DATA_ROOT = _LOCAL_ROOT

_MODELS_DIR = os.path.join(_DATA_ROOT, "models")
_BIN_DIR = os.path.join(_DATA_ROOT, "bin")
_LOGS_DIR = os.path.join(_DATA_ROOT, "logs")
_CONFIG = os.path.join(_DATA_ROOT, "config.json")
_DOWNLOAD_DIR = os.path.join(_DATA_ROOT, "downloads")

# 术语分两层，避免"升级覆盖 plugins/ 把用户自定义术语一起冲掉"：
#   builtin（随包，只读）      ai_translate_assets/glossary_builtin.json
#   用户覆盖层（可变）          <_DATA_ROOT>/glossary_user.json
# 早期两者是同一个文件（assets/glossary_user.json），升级即丢用户词条；
# 这里保留对旧文件名/旧扁平格式的读取兼容，见 _read_user_layer()。
_GLOSS_BUILTIN = os.path.join(_ASSETS, "glossary_builtin.json")
_GLOSS_BUILTIN_LEGACY = os.path.join(_ASSETS, "glossary_user.json")
_GLOSS_USER = os.path.join(_DATA_ROOT, "glossary_user.json")
_GLOSS_LAYER_FORMAT = "ai-translate-glossary-user/1"

for _d in (_DATA_ROOT, _MODELS_DIR, _BIN_DIR, _LOGS_DIR, _DOWNLOAD_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        pass

_RUN_LOG = os.path.join(_LOGS_DIR, "translate_run.log")
_run_log_lock = threading.Lock()

# 日志等级只用文字前缀表达（2.x 的 api.log 没有 tag / 颜色参数）
_TAG_PREFIX = {"err": "［错误］", "warn": "［提示］", "ok": "［完成］"}


def _append_run_log(text):
    """逐行写运行日志文件（宿主没有 detail 日志通道）。

    条目级进度实测可达 470 行/秒；写进界面日志会把主线程压死，所以
    明细/进度只落盘，界面只保留每 50 个文件一条的进度。
    """
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
    try:
        with _run_log_lock:
            try:
                if os.path.getsize(_RUN_LOG) > 8 * 1024 * 1024:
                    os.replace(_RUN_LOG, _RUN_LOG + ".1")
            except OSError:
                pass
            with open(_RUN_LOG, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        pass


def _bin_dirs():
    """查找运行库时同时看随包目录与用户数据目录。"""
    return [_SHIPPED_BIN_DIR, _BIN_DIR]


# ══════════════════════════════════════════════════════════════════
# Windows 进程生命周期：Job Object + 遗留引擎清理
#
# 背景（2026-10-30 实测）：工具集被"任务管理器结束任务"后，llama-server
# 不会收到任何通知（atexit 不执行），于是留在后台继续占着 ~3 GB 显存和
# 数 GB 内存。连续几次强杀后 6 GB 显卡被 4 个残留引擎塞满，驱动把模型
# 溢到内存，下一次翻译连窗口一起卡死 —— 用户看到的就是"又卡死了"。
#
# 这里做两件事：
#   1. 启动时扫一遍 llama-server.exe：只认本插件自己的 exe 路径，且父
#      进程已经不在的（真孤儿），杀掉并回收显存；
#   2. 新拉起的引擎放进 kill-on-close 的 Job Object：宿主进程无论怎么
#      死（含 TerminateProcess），内核都会连带终止它，不再产生新孤儿。
# ══════════════════════════════════════════════════════════════════

_WIN = False          # False=尚未初始化；None=初始化失败；dict=已就绪
_KILL_JOB = None      # kill-on-close Job 句柄，保持到进程结束
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WAIT_TIMEOUT = 0x00000102
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_TH32CS_SNAPPROCESS = 0x00000002


def _win():
    """Windows 进程/作业对象 API 的轻量封装；非 Windows 返回 None。"""
    global _WIN
    if os.name != "nt":
        return None
    if _WIN not in (False, None):
        return _WIN
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                 wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                    wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,
                                                 wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL

        _WIN = {
            "ctypes": ctypes,
            "wintypes": wintypes,
            "k32": k32,
            "EXTENDED_LIMIT": EXTENDED_LIMIT,
            "PROCESSENTRY32W": PROCESSENTRY32W,
            "INVALID_HANDLE": ctypes.c_void_p(-1).value,
        }
    except Exception:
        _WIN = None
    return _WIN


def _norm_path(p):
    try:
        return os.path.normcase(os.path.abspath(str(p)))
    except Exception:
        return ""


def _server_exe_paths(cfg=None):
    """本插件可能使用的 llama-server.exe 全路径集合。

    只认这些**精确路径**，避免误杀用户自己跑的 llama.cpp 或别的工具集。
    布局：<随包或用户数据>/bin/<变体>/llama-server.exe；外加手工指定的
    exe_paths（用户可选任意本地运行库）。
    """
    paths = set()
    for base in _bin_dirs():
        for variant in ("cpu", "cuda", "cuda13", "vulkan"):
            paths.add(_norm_path(os.path.join(base, variant,
                                              "llama-server.exe")))
        paths.add(_norm_path(os.path.join(base, "llama-server.exe")))
    for p in ((cfg or {}).get("exe_paths") or {}).values():
        if p:
            paths.add(_norm_path(p))
    paths.discard("")
    return paths


def _win_process_image(pid):
    """进程完整 exe 路径；取不到返回空串。"""
    w = _win()
    if not w:
        return ""
    k32, ctypes, wintypes = w["k32"], w["ctypes"], w["wintypes"]
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    except Exception:
        pass
    finally:
        k32.CloseHandle(h)
    return ""


def _win_pid_alive(pid):
    """父进程是否还活着（只用于判孤儿；PID 复用会偏保守地算"活着"）。"""
    w = _win()
    if not w:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    k32 = w["k32"]
    h = k32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


def _win_terminate_pid(pid):
    w = _win()
    if not w:
        return False
    k32 = w["k32"]
    h = k32.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
    if not h:
        return False
    try:
        return bool(k32.TerminateProcess(h, 1))
    finally:
        k32.CloseHandle(h)


def _win_llama_servers():
    """[(pid, ppid, exe_path)]，仅 llama-server.exe。"""
    w = _win()
    if not w:
        return []
    k32, ctypes, wintypes = w["k32"], w["ctypes"], w["wintypes"]
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == w["INVALID_HANDLE"]:
        return []
    out = []
    try:
        entry = w["PROCESSENTRY32W"]()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = (entry.szExeFile or "").lower()
            if name == "llama-server.exe":
                pid = int(entry.th32ProcessID)
                out.append((pid, int(entry.th32ParentProcessID),
                            _win_process_image(pid)))
            entry.dwSize = ctypes.sizeof(entry)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _kill_orphan_servers(cfg=None):
    """杀掉**本插件路径下**且父进程已退出的 llama-server，返回 PID 列表。"""
    if os.name != "nt":
        return []
    allowed = _server_exe_paths(cfg)
    killed = []
    for pid, ppid, exe in _win_llama_servers():
        if not exe or _norm_path(exe) not in allowed:
            continue
        if _win_pid_alive(ppid):
            continue                      # 还归某个活着的宿主，别动
        if _win_terminate_pid(pid):
            killed.append(pid)
    return killed


def _create_kill_job():
    """创建 kill-on-close 作业对象；失败返回 None（不影响翻译）。"""
    w = _win()
    if not w:
        return None
    k32, ctypes = w["k32"], w["ctypes"]
    job = k32.CreateJobObjectW(None, None)
    if not job or job == w["INVALID_HANDLE"]:
        return None
    info = w["EXTENDED_LIMIT"]()
    info.BasicLimitInformation.LimitFlags = \
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    return job


def _assign_pid_to_job(job, pid):
    w = _win()
    if not w or not job:
        return False
    k32 = w["k32"]
    h = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                        False, int(pid))
    if not h:
        return False
    try:
        return bool(k32.AssignProcessToJobObject(job, h))
    finally:
        k32.CloseHandle(h)


def _close_handle(h):
    w = _win()
    if w and h:
        w["k32"].CloseHandle(h)


def _kill_job():
    """进程级共享的 Job（保持句柄到退出；宿主一死内核连带收掉引擎）。"""
    global _KILL_JOB
    if os.name != "nt":
        return None
    if _KILL_JOB is None:
        _KILL_JOB = _create_kill_job()
    return _KILL_JOB


def _assign_server_to_kill_job(proc):
    if proc is None:
        return False
    return _assign_pid_to_job(_kill_job(), proc.pid)


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) STALKER-Localization-Toolkit/ai-translate"


# ══════════════════════════════════════════════════════════════════
# HTTP（WinHTTP / ctypes）
#
# 为什么不用 urllib：2.x 发行包剥掉了 `_ssl.pyd` / libssl / libcrypto
# （scripts/package.ps1 的裁剪清单），`import urllib.request` 会连带
# `import http.client` → `import ssl` ⇒ **连 http://127.0.0.1 都加载失败**。
# 这里用系统 winhttp.dll：无需额外 DLL、走系统代理与系统证书、跟随重定向。
#
# ★ 必须声明 restype/argtypes：ctypes 默认把返回值当 32 位 int，64 位下
#   HINTERNET 指针会被截断成无效句柄（上游 _sdk/http_winhttp.py 的实测教训）。
# ══════════════════════════════════════════════════════════════════

class _HttpError(IOError):
    """HTTP 状态码 >= 400（对齐 urllib.error.HTTPError 的用法）。"""

    def __init__(self, code, reason="", url="", body=b""):
        self.code = int(code)
        self.reason = reason or ""
        self.url = url or ""
        self._body = body or b""
        IOError.__init__(self, "HTTP %d %s%s"
                         % (self.code, self.reason,
                            (" — " + self.url) if self.url else ""))

    def read(self):
        return self._body


class _Headers(dict):
    """大小写不敏感的表头读取（调用侧写 `headers.get("Content-Type")`）。"""

    def __init__(self, pairs):
        dict.__init__(self)
        self._lower = {}
        for key, value in pairs:
            dict.__setitem__(self, key, value)
            self._lower[key.lower()] = key

    def get(self, key, default=""):
        real = self._lower.get(str(key).lower())
        return default if real is None else dict.__getitem__(self, real)

    def __contains__(self, key):
        return str(key).lower() in self._lower


class _HttpResponse(object):
    """与 urllib 响应同形：status / headers / read / close / 上下文管理。"""

    def __init__(self, dll, req, session, conn, status, headers):
        self._dll = dll
        self._req = req
        self._session = session
        self._conn = conn
        self._status = status
        self._headers = headers
        self._closed = False

    @property
    def status(self):
        return self._status

    @property
    def headers(self):
        return self._headers

    def read(self, size=-1):
        if self._closed:
            return b""
        chunks = []
        total = 0
        want = int(size) if size is not None and int(size) > 0 else 0
        buf = ctypes.create_string_buffer(256 * 1024)
        got = ctypes.c_ulong(0)
        while True:
            # 单次最多读 buf 大小；want>0 时读够就停（与 urllib.read(n) 一致）
            ask = len(buf)
            if want:
                ask = min(ask, want - total)
                if ask <= 0:
                    break
            if not self._dll.WinHttpReadData(self._req, buf, ask,
                                             ctypes.byref(got)):
                raise OSError("WinHttpReadData 失败：%d"
                              % ctypes.get_last_error())
            if got.value == 0:
                break
            chunks.append(buf.raw[:got.value])
            total += got.value
            if want and total >= want:
                break
        return b"".join(chunks)

    def close(self):
        if self._closed:
            return
        self._closed = True
        for handle in (self._req, self._conn, self._session):
            if handle:
                try:
                    self._dll.WinHttpCloseHandle(handle)
                except Exception:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


_WINHTTP = None

_WINHTTP_QUERY_STATUS_CODE = 19
_WINHTTP_QUERY_RAW_HEADERS_CRLF = 22
_WINHTTP_QUERY_FLAG_NUMBER = 0x20000000
_WINHTTP_ACCESS_TYPE_NO_PROXY = 1
_WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY = 4
_WINHTTP_FLAG_SECURE = 0x00800000
_WINHTTP_OPTION_REDIRECT_POLICY = 110
_WINHTTP_REDIRECT_DISALLOW_HTTPS_TO_HTTP = 1
_ERROR_INSUFFICIENT_BUFFER = 122

# B250+：宿主 http_download 只接管「无断点、体积可预估」的中小产物。
# 模型（1.8/4.4 GB，需进度+断点+长时长）仍走自建流式，见 _host_http_download。
_HOST_DL_MAX = 256 * 1024 * 1024


def _winhttp():
    """惰性加载 winhttp.dll 并声明全部原型（只做一次）。"""
    global _WINHTTP
    if _WINHTTP is not None:
        return _WINHTTP
    from ctypes import wintypes
    dll = ctypes.WinDLL("winhttp", use_last_error=True)
    handle = wintypes.HANDLE
    dword = wintypes.DWORD
    wide = wintypes.LPCWSTR

    dll.WinHttpOpen.restype = handle
    dll.WinHttpOpen.argtypes = [wide, dword, wide, wide, dword]
    dll.WinHttpSetTimeouts.restype = wintypes.BOOL
    dll.WinHttpSetTimeouts.argtypes = [handle, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int]
    dll.WinHttpSetOption.restype = wintypes.BOOL
    dll.WinHttpSetOption.argtypes = [handle, dword, ctypes.c_void_p, dword]
    dll.WinHttpConnect.restype = handle
    dll.WinHttpConnect.argtypes = [handle, wide, wintypes.WORD, dword]
    dll.WinHttpOpenRequest.restype = handle
    dll.WinHttpOpenRequest.argtypes = [handle, wide, wide, wide, wide,
                                       ctypes.POINTER(wide), dword]
    dll.WinHttpSendRequest.restype = wintypes.BOOL
    dll.WinHttpSendRequest.argtypes = [handle, wide, dword, ctypes.c_void_p,
                                       dword, dword, ctypes.c_size_t]
    dll.WinHttpReceiveResponse.restype = wintypes.BOOL
    dll.WinHttpReceiveResponse.argtypes = [handle, ctypes.c_void_p]
    dll.WinHttpQueryHeaders.restype = wintypes.BOOL
    dll.WinHttpQueryHeaders.argtypes = [handle, dword, wide, ctypes.c_void_p,
                                        ctypes.POINTER(dword),
                                        ctypes.POINTER(dword)]
    dll.WinHttpReadData.restype = wintypes.BOOL
    dll.WinHttpReadData.argtypes = [handle, ctypes.c_void_p, dword,
                                    ctypes.POINTER(dword)]
    dll.WinHttpCloseHandle.restype = wintypes.BOOL
    dll.WinHttpCloseHandle.argtypes = [handle]
    _WINHTTP = dll
    return dll


def _query_headers_text(dll, req):
    size = ctypes.c_ulong(0)
    dll.WinHttpQueryHeaders(req, _WINHTTP_QUERY_RAW_HEADERS_CRLF, None, None,
                            ctypes.byref(size), None)
    if size.value <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(size.value // 2 + 2)
    length = ctypes.c_ulong(ctypes.sizeof(buf))
    if not dll.WinHttpQueryHeaders(req, _WINHTTP_QUERY_RAW_HEADERS_CRLF, None,
                                   ctypes.byref(buf), ctypes.byref(length),
                                   None):
        return ""
    return buf.value


def _parse_headers(text):
    pairs = []
    for line in (text or "").split("\r\n")[1:]:
        if not line.strip():
            continue
        key, _sep, value = line.partition(":")
        if key and _sep:
            pairs.append((key.strip(), value.strip()))
    return _Headers(pairs)


def _query_status(dll, req):
    code = ctypes.c_ulong(0)
    size = ctypes.c_ulong(ctypes.sizeof(code))
    level = _WINHTTP_QUERY_STATUS_CODE | _WINHTTP_QUERY_FLAG_NUMBER
    if not dll.WinHttpQueryHeaders(req, level, None, ctypes.byref(code),
                                   ctypes.byref(size), None):
        raise OSError("WinHttpQueryHeaders(状态码) 失败：%d"
                      % ctypes.get_last_error())
    return int(code.value)


def _status_reason(text):
    first = (text or "").split("\r\n", 1)[0]
    parts = first.split(" ", 2)
    return parts[2] if len(parts) == 3 else ""


class _UrlResponse(object):
    """把 urllib 响应包成与 `_HttpResponse` 同形（status/headers/read/close/上下文）。

    B249 起平台随包保留 `_ssl` + libssl（scripts/package.ps1 的 2026-11-14 决策），
    插件可直接用标准库 urllib —— 这是**平台内置能力**，优先走它；老宿主
    （如 BETA.245 fork，剥掉了 _ssl）import 失败时回退自建 ctypes WinHTTP。
    """

    def __init__(self, raw):
        self._raw = raw
        self.status = int(getattr(raw, "status", 0) or 0)
        # email.message.Message → 大小写不敏感 dict，对齐 _Headers.get()
        self.headers = dict((k, v) for k, v in getattr(raw, "headers", {}).items())

    def read(self, size=-1):
        return self._raw.read() if size is None or int(size) <= 0 \
            else self._raw.read(int(size))

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()
        return False


def _urllib_request(method, url, headers=None, body=None, timeout=30.0):
    """平台内置 SSL 可用时的标准库 HTTP 路径（优先）。不可用抛 ImportError。"""
    import urllib.request as _ur
    import urllib.error as _ue
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise IOError("不支持的下载地址：%s" % url)
    headers = dict(headers or {})
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = UA
    payload = body or None
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    req = _ur.Request(str(url), data=payload, headers=headers, method=method)
    local = parts.hostname.lower() in ("127.0.0.1", "localhost", "::1")
    # 本地回环必须绕过代理（对齐 ctypes 路径的 NO_PROXY）；外网走系统代理。
    opener = _ur.build_opener(_ur.ProxyHandler({})) if local \
        else _ur.build_opener()
    try:
        raw = opener.open(req, timeout=max(1, float(timeout or 30)))
    except _ue.HTTPError as e:
        # 与 ctypes 路径同形：>=400 抛 _HttpError，.read() 仍可取错误体
        raise _HttpError(e.code, e.reason, str(url), e.read())
    return _UrlResponse(raw)


def _http_request(method, url, headers=None, body=None, timeout=30.0):
    """同步 HTTP 请求。返回与 urllib 同形的响应；>=400 抛 `_HttpError`。

    优先用平台内置的 urllib + SSL（B249+）；`_ssl` 缺失（老宿主）或 urllib
    本身失败时回退自建 ctypes WinHTTP（零依赖、走系统代理与证书）。
    """
    try:
        return _urllib_request(method, url, headers, body, timeout)
    except ImportError:
        pass  # 老宿主剥掉 _ssl：回退 WinHTTP
    except (_HttpError, IOError, OSError, ValueError):
        raise  # 真正的网络/参数错误不吞，交调用方处理
    dll = _winhttp()
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise IOError("不支持的下载地址：%s" % url)
    secure = parts.scheme == "https"
    host = parts.hostname
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    millis = max(500, int(float(timeout or 30) * 1000))
    local = host.lower() in ("127.0.0.1", "localhost", "::1")
    session = dll.WinHttpOpen(
        "STALKER-Localization-Toolkit/ai-translate",
        _WINHTTP_ACCESS_TYPE_NO_PROXY if local
        else _WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
        None, None, 0)
    if not session:
        raise OSError("WinHttpOpen 失败：%d" % ctypes.get_last_error())

    conn = req = None
    try:
        for option in (1, 2, 3, 4):   # resolve / connect / send / receive
            dll.WinHttpSetTimeouts(session, millis, millis, millis, millis)
        policy = ctypes.c_ulong(_WINHTTP_REDIRECT_DISALLOW_HTTPS_TO_HTTP)
        dll.WinHttpSetOption(session, _WINHTTP_OPTION_REDIRECT_POLICY,
                             ctypes.byref(policy), ctypes.sizeof(policy))

        conn = dll.WinHttpConnect(session, host, port, 0)
        if not conn:
            raise OSError("WinHttpConnect(%s:%d) 失败：%d"
                          % (host, port, ctypes.get_last_error()))
        req = dll.WinHttpOpenRequest(
            conn, method, path, None, None, None,
            _WINHTTP_FLAG_SECURE if secure else 0)
        if not req:
            raise OSError("WinHttpOpenRequest 失败：%d"
                          % ctypes.get_last_error())

        lines = []
        extra = dict(headers or {})
        if not any(k.lower() == "user-agent" for k in extra):
            lines.append("User-Agent: " + UA)
        for key, value in extra.items():
            lines.append("%s: %s" % (key, value))
        header_text = ("\r\n".join(lines) + "\r\n") if lines else None

        payload = body or b""
        if payload:
            buffer = ctypes.create_string_buffer(payload, len(payload))
            # dwTotalLength 必须是整个请求体的字节数；给 0 而 dwOptionalLength>0
            # 会让 WinHTTP 直接返回 ERROR_INVALID_PARAMETER(87)，POST 永远发不出去。
            sent = dll.WinHttpSendRequest(req, header_text,
                                          len(header_text) if header_text else 0,
                                          ctypes.byref(buffer), len(payload),
                                          len(payload), 0)
        else:
            sent = dll.WinHttpSendRequest(req, header_text,
                                          len(header_text) if header_text else 0,
                                          None, 0, 0, 0)
        if not sent:
            raise OSError("WinHttpSendRequest 失败：%d"
                          % ctypes.get_last_error())
        if not dll.WinHttpReceiveResponse(req, None):
            raise OSError("WinHttpReceiveResponse 失败：%d"
                          % ctypes.get_last_error())

        status = _query_status(dll, req)
        raw = _query_headers_text(dll, req)
        response = _HttpResponse(dll, req, session, conn, status,
                                 _parse_headers(raw))
        if status >= 400:
            try:
                text_body = response.read()
            except Exception:
                text_body = b""
            response.close()
            raise _HttpError(status, _status_reason(raw), url, text_body)
        # 成功路径：句柄所有权交给 response（close 时释放）
        req = conn = session = None
        return response
    finally:
        for handle in (req, conn, session):
            if handle:
                try:
                    dll.WinHttpCloseHandle(handle)
                except Exception:
                    pass


class _HostHttpResponse(object):
    """把宿主 `api.host.http_*` 的 dict 结果包成与流响应同形。

    宿主门面返回 `{ok,status,body(str),error,bytes,truncated}`——body 已按文本
    解码（引擎返回的就是 JSON 文本，正好）。只用于**小文本请求**；GB 级下载
    仍走流式管道（宿主门面会把整包读进内存，不适用）。
    """

    def __init__(self, result):
        self.status = int(result.get("status") or 0)
        self.headers = {}       # 小请求只需 status/body，不读 header
        self._body = str(result.get("body") or "").encode("utf-8")

    def read(self, size=-1):
        if size is None or int(size) <= 0:
            return self._body
        return self._body[:int(size)]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


_API = None
_HOST_NOTE_TS = 0.0      # 上次门面回退留痕时间（限频，防日志风暴）

# ★ 2.0.12 多路翻译直连开关。
# 2.0.5 起所有小请求跟随宿主 api.host.http_post（平台作者要求"不要二次
# 实现"），但实测宿主门面在多线程调用时被串行化：llama-server 请求时间线
# 显示峰值并行度恒为 1（11-16 用自建 urllib 时是 2），三路 worker 发出的
# 请求被门面排成一条队，slot 再均衡也没用。翻译是高频并发小文本请求，
# 与门面"零散小请求"的设计场景不同。这里给线程安全的翻译通道开一个
# 直连开关：仅 /v1/ 推理请求走自建 urllib（并发），其余仍走宿主门面。
# 门面本身照旧注册、健康检查/下载/配置仍跟随宿主，不违背整体约定。
_DIRECT_INFER_HTTP = False


def set_direct_infer_http(on):
    global _DIRECT_INFER_HTTP
    _DIRECT_INFER_HTTP = bool(on)


def _host_fallback_note(url, why):
    """宿主门面失败回退自建路径时，往宿主控制台留一条痕（60s 限频）。

    为什么留痕：门面失败原本是静默回退，同一请求会被原样再打一遍，
    排查时看不出"门面失败过"；限频避免引擎健康检查等高频请求刷屏。
    api 未注册（测试/离线场景）时静默跳过。
    """
    global _HOST_NOTE_TS
    if _API is None:
        return
    now = time.time()
    if now - _HOST_NOTE_TS < 60.0:
        return
    _HOST_NOTE_TS = now
    try:
        _API.log("宿主 HTTP 门面失败，已回退自建路径：%s（%s）"
                 % (url, why))
    except Exception:
        pass


def _host_small_http(method, url, headers=None, body=None, timeout=30.0):
    """小文本请求优先走宿主门面；老宿主/不支持时返回 None 让调用方回退。"""
    host = _HOST
    if host is None:
        return None
    fn = getattr(host, "http_get" if method == "GET" else "http_post", None)
    if fn is None:
        return None
    try:
        if method == "GET":
            result = fn(url, int(timeout), 1 << 20)
        else:
            payload = body.decode("utf-8", "ignore") \
                if isinstance(body, bytes) else str(body or "")
            ctype = (headers or {}).get("Content-Type", "application/json")
            result = fn(url, payload, ctype, int(timeout), 1 << 20)
    except Exception as e:
        _host_fallback_note(url, "门面调用异常 %s: %s"
                            % (type(e).__name__, e))
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        # 门面明确失败（HTTP 4xx/5xx 或网络错误）：留痕后回退自建路径。
        # 否则表现为"静默多打一次请求"，排查时看不出门面失败过。
        _host_fallback_note(
            url, "ok=false（HTTP %s %s）"
            % (result.get("status") if isinstance(result, dict) else "?",
               result.get("error", "") if isinstance(result, dict) else ""))
        return None     # 宿主请求失败 → 回退自建路径（老行为）
    return _HostHttpResponse(result)


def _http_get(url, headers=None, timeout=30.0):
    small = _host_small_http("GET", url, headers=headers, timeout=timeout)
    if small is not None:
        return small
    return _http_request("GET", url, headers=headers, timeout=timeout)


def _http_post(url, data, headers=None, timeout=30.0):
    # ★ 2.0.12：并发推理请求直连自建 urllib，绕过会串行化的宿主门面；
    # 非推理请求（下载/配置/健康检查等零散小请求）仍跟随宿主门面。
    if _DIRECT_INFER_HTTP and "/v1/" in str(url):
        return _http_request("POST", url, headers=headers, body=data,
                             timeout=timeout)
    small = _host_small_http("POST", url, headers=headers, body=data,
                             timeout=timeout)
    if small is not None:
        return small
    return _http_request("POST", url, headers=headers, body=data,
                         timeout=timeout)


LANG_FULL = {"chs": "简体中文", "eng": "英语", "rus": "俄语", "ukr": "乌克兰语"}
LANG_CODE = {"chs": "zh", "eng": "en", "rus": "ru", "ukr": "uk"}

# 通用技术精确表：只做整条精确匹配，绝不子串替换。
# 这些短标签模型通常保留原文；不放进核心硬词，避免影响正文。
_TECHNICAL_EXACT = {
    "ч.": "小时", "ч": "小时", "час": "小时", "часа": "小时",
    "сек": "秒", "сек.": "秒", "м/с": "米/秒", "в/мин": "发/分钟",
    "шт": "个", "шт.": "个", "р.": "卢布", "руб.": "卢布",
    "дБм.": "分贝毫瓦", "дБм": "分贝毫瓦", "м.": "米", "м": "米",
    "RU": "俄语", "FX": "特效",
}

# 会话词表：当前 MOD 的人名/物品/地点候选，自动翻译并缓存。
# 它不是随包术语库，不需要为每个 MOD 手工建档；缓存只在本机。
_SESSION_TERM_NOTE = (
    "这是游戏名词表。逐行译成简体中文，保持行数与顺序；"
    "人名音译，绰号/物品/地点按游戏习惯意译；"
    "不要编号、不要解释、不要保留俄文或英文原文。")
_SESSION_QUOTES = "\"'«»“”‘’"
_SESSION_NAME_DIRS = ("character_name", "snames", "fnames", "bio")
_SESSION_ITEM_DIRS = ("item", "weapon", "equipment", "artefact",
                      "artifact", "arsenal")
_SESSION_LOC_DIRS = ("mapspot", "level", "place", "zone")
_SESSION_SKIP_DIRS = ("dialog", "news", "task", "tip", "stash", "secret",
                      "credits", "ui", "gameplay", "spawn", "npc")
_SESSION_LIMITS = {"人物与军衔": 1500, "物品·武器·装备·神器": 500,
                   "地点与区域": 400, "阵营与组织": 150, "变异生物": 100,
                   # ★ 2.0.11 成就名/声望等级/武器属性短标签：模型单条常原样
                   # 回显，纳入会话词表批量定译（批量带"不得保留英文"提示）。
                   "成就·称号·属性标签": 300}

# ★ 2.0.3 高频专名定译：会话词表只扫“名字型”文件（人物/物品/地点目录），
# 正文里反复出现却不在名字型文件里的专名（组织、事件、俚语地名等）仍会
# 跨文件漂译。这里全文扫描高频词定译，并入软提示层收敛译名。
_FREQ_NAME_MIN_TOTAL = 5    # 全文出现 ≥5 次才候选
_FREQ_NAME_MIN_MID = 2      # 其中句中位置（前随空白/标点）≥2 次（滤句首普通词）
_FREQ_NAME_LIMIT = 64       # 按频次取 top-N，控预算
_FREQ_SCAN_FILE_CAP = 4000  # 解析文件数上限，防超大 MOD 拖慢启动
_FREQ_WORD_RE = re.compile(r"[А-ЯЁ][а-яё]{2,}")
_FREQ_MID_RE = re.compile(
    r"(?<=[\s«„\"'(，。！？…：；—])[А-ЯЁ][а-яё]{2,}")

# TM-lite：只在“同一批文本里重复句比例高”时启用**精确同源**复用。
# 不做模糊匹配、不建跨 MOD 持久库、不把上一次任务的译文混进来。
# 实测 NS7 182 张表：全局重复率 7.4%，5 张表 ≥20%（1,482 条）；
# 阈值 20% 正好把“名字/任务/新闻模板”这类高重复表挑出来，普通对话表
# 不会因为零星重复而改变翻译路径。
# ★ 2.0.13：实测 IWP 重复率约 9.3%，旧门槛 20% 导致表内精确复用长期不启用。
# 降到 8% 让重复段直接复用（零生成）；MIN_ENTRIES 保持避免小表误判。
TM_LITE_MIN_RATIO = 0.08
TM_LITE_MIN_ENTRIES = 40


def session_category(rel, eid):
    """会话词表只收“名字型”条目；对话框/新闻/任务/署名不碰。"""
    s = (rel or "").lower().replace("\\", "/")
    k = (eid or "").lower()
    if any(x in s for x in _SESSION_SKIP_DIRS):
        return None
    if any(x in s for x in _SESSION_NAME_DIRS):
        if k.endswith(("_bio", "_descr", "_desc", "_text", "_hint")):
            return None
        if k.endswith("_name") or "snames" in s or "fnames" in s:
            return "人物与军衔"
        return None
    if any(x in s for x in ("faction", "community")):
        return "阵营与组织"
    # ★ 2.0.11 玩家可见短标签：成就名、声望/等级、武器属性。
    # 必须放在 _SESSION_ITEM_DIRS 之前（gunsl_up_binoc 等含 weapon 字样的
    # 属性表优先归到标签类，模型原样回显时由会话词表批量定译）。
    if any(x in s for x in ("achievement", "game_relations", "binoc",
                            "reputation", "rank")):
        return "成就·称号·属性标签"
    if any(x in s for x in _SESSION_ITEM_DIRS):
        if k.endswith(("_descr", "_desc", "_bio", "_text", "_hint")):
            return None
        return "物品·武器·装备·神器"
    if any(x in s for x in ("creature", "mutant", "monster")):
        return "变异生物"
    if any(x in s for x in _SESSION_LOC_DIRS):
        return "地点与区域"
    return None


def session_term_shape(src, cat):
    """名词形状：短、无句末标点；人名允许缩写点。"""
    s = (src or "").strip().strip(_SESSION_QUOTES).strip()
    if not s or len(s) > 60 or "\n" in s or "\r" in s:
        return ""
    words = s.split()
    if not (1 <= len(words) <= 5):
        return ""
    if cat == "人物与军衔":
        if re.search(r"[。！？!?；;:]", s):
            return ""
        if re.match(r"^[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]?\.?\s*[А-ЯЁA-Z]", s):
            return s
        if s[0].isupper():
            return s
        return ""
    if re.search(r"[.!?。！？；;:，,]", s):
        return ""
    if not re.search(r"[А-Яа-яЁёA-Za-z]", s):
        return ""
    if cat == "物品·武器·装备·神器" and looks_like_model_name(s):
        return ""
    return s

TRANSLATION_SYS_PROMPT = (
    "你是《潜行者 S.T.A.L.K.E.R.》后末日游戏的专业译员，"
    "输入为对话、任务或物品文本。遵守以下规则。\n"
    "\n"
    "【硬性规则】\n"
    "1. 逐字保留、数量与顺序不变：占位符（%s、%d、%1$s）、"
    "控制符（$$ACTION$$、$变量）、脚本与条目ID（如 zat_b101_x、"
    "sgtat_gen_text_1_x、a.b.c）、\\n；疑似代码或标记一律照抄勿译；\n"
    "2. 人名、阵营、地名、怪物、武器物品等专名使用游戏约定译名，"
    "前后统一，不另译、不音译成多个版本；\n"
    "3. 忠实完整，不增译、不漏译、不脑补剧情；\n"
    "4. 粗话、脏话、辱骂、威胁、黑色幽默与残酷描写一律按原意直译，"
    "保留同等粗鲁力度与情绪强度：不得弱化、省略、删除或改写成书面语、"
    "委婉语（дерьмо 就是「屎／狗屎」，不是「糟糕」；заткнись 就是「闭嘴」，"
    "不是「请安静」；башку оторву 就是「拧下你的脑袋」，"
    "不是「我不会放过你」）；原文粗俗则译文粗俗，"
    "不得因内容而回避或粉饰；\n"
    "5. 输入是格变、复数、单数片段或半句时，按词库和上下文还原语义后\n"
    "译成中文，不要保留原文，也不要只译一半；\n"
    "6. 保留原文的换行数量与段落数；不要把两段合并成一段，也不要多加空行；\n"
    "\n"
    "【语气与风格】\n"
    "7. 译文必须是地道的中文口语，读起来要像中国玩家自己在说话："
    "用中文的语序、短句和中文习惯的省略与断句；称呼、亲疏、口气与情绪"
    "贴合说话人身份，自然使用「啊、吧、嘛、呢、唉、喂」等语气词；\n"
    "8. 不要翻译腔：不要「我不得不说」「就……而言」「如此」「因此」「并且」"
    "这类欧化说法，不要生硬的被动句，不要把从句堆成长句，"
    "不要连缀多余的「的」；\n"
    "9. 物品名称、按钮、菜单项这类短标签保持简洁直白，不口语化、不加语气词；\n"
    "10. 反问、讽刺、威胁按说话人的意图译，不要逐字翻转否定："
    "«Ну а на хрен тебе не пойти?» 是「你他妈怎么还不滚？」，"
    "不是「你别去了吧」；\n"
    "11. 引号不是保留原文的理由：引号内的阵营、组织、地名、物品、人名"
    "同样要译成中文；只有代码、型号、条目ID、文件路径才保留原文；\n"
    "12. 术语提示只在语义相符时采用：同一拼写在普通语境（例如 крот=鼹鼠）"
    "不要硬套人名译名；同一人物、物品、地名全篇保持同一个译名。\n"
    "\n"
    "口语对照示例（左为生硬译法，右为地道中文；照右边的路子写）：\n"
    "· 「听着，我有件事要告诉你。」→「哎，我有事找你。」\n"
    "· 「我们无法再继续留在这里了。」→「这儿咱待不下去了。」\n"
    "· 「我不知道你该如何做到这一点。」→「我不知道你咋弄，反正东西我得要。」\n"
    "· 「你明白正在发生什么吗？」→「你懂现在发生啥了吗？」\n"
    "· 「如果你因失血过多而死，我概不负责。」→「你要是流血死了，我可不负责。」\n"
    "\n"
    "只输出译文本身，不加任何解释或说明；原文已是目标语言时原样返回。\n"
    "最后再强调两条：占位符、控制符、条目ID 必须逐字保留；"
    "译文里不得残留整词或整段的俄文。"
)

# 英语源文专用提示词。规则与俄语版同构（分区+首尾复述），把术语/粗口/
# 口语锚例换成英语里最常见的误译点：小模型容易把英语委婉化、书面化，
# 或把习语逐词直译。
TRANSLATION_SYS_PROMPT_EN = (
    "你是《潜行者 S.T.A.L.K.E.R.》后末日游戏的专业译员，"
    "输入可能是英语的对话、任务、物品或界面文本。遵守以下规则。\n"
    "\n"
    "【硬性规则】\n"
    "1. 逐字保留、数量与顺序不变：占位符（%s、%d、%1$s）、"
    "控制符（$$ACTION$$、$变量）、脚本与条目ID（如 zat_b101_x、"
    "sgtat_gen_text_1_x、a.b.c）、\\n；疑似代码或标记一律照抄勿译；\n"
    "2. 人名、阵营、地名、怪物、武器物品等专名使用游戏约定译名；"
    "有官方中文名的用官方名，没有的按常见音译，首次出现后保持统一；"
    "缩写、型号、代号（AK-74、PDA、SEVA）保留原文，不要硬造中文；\n"
    "3. 忠实完整，不增译、不漏译、不脑补剧情；英语的冠词、助动词、"
    "重复主语不要逐词搬进中文；\n"
    "4. 粗话、脏话、辱骂、俚语、威胁、黑色幽默与残酷描写一律按原意直译，"
    "保留同等粗鲁力度与情绪强度：不得弱化、省略、删除或改写成书面语、"
    "委婉语（fuck 就是「操／妈的」，shit 就是「屎／狗屎」，不是「糟糕」；"
    "shut up 就是「闭嘴」，不是「请安静」；I'll rip your head off "
    "就是「我拧下你的脑袋」，不是「我不会放过你」）；"
    "原文粗俗则译文粗俗；\n"
    "5. 输入是复数、缩写、单数片段或半句时，按词库和上下文还原语义后"
    "译成中文，不要保留英文原文；\n"
    "6. 保留原文的换行数量与段落数；不要把两段合并成一段，也不要多加空行；\n"
    "\n"
    "【语气与风格】\n"
    "7. 译文必须是地道的中文口语，读起来要像中国玩家自己在说话："
    "用中文的语序、短句和中文习惯的省略与断句；称呼、亲疏、口气与情绪"
    "贴合说话人身份，自然使用「啊、吧、嘛、呢、唉、喂」等语气词；\n"
    "8. 不要翻译腔：不要「我不得不说」「就……而言」「如此」「因此」「并且」"
    "这类欧化说法，不要生硬的被动句，不要把从句堆成长句，"
    "不要连缀多余的「的」；英语习语按中文口语意译"
    "（例如 It's a piece of cake → 小菜一碟），不要逐词硬译；\n"
    "8a. 物品描述、百科正文虽不是对话，同样不许有翻译腔：用自然的中文"
    "说明文，短句直说（「low radiation won't kill you」是「低剂量辐射"
    "死不了人」，不是「你不会因低辐射而死」）；「并」「其」「则」「"
    "免受……的伤害」这类书面连接能免则免；\n"
    "9. 物品名称、按钮、菜单项这类短标签保持简洁直白，不口语化、不加语气词；\n"
    "10. 反问、讽刺、威胁按说话人的意图译，不要逐字翻转否定："
    "「Why don't you just leave?」是「你他妈怎么还不滚？」，"
    "不是「你为什么不离开」；\n"
    "11. 引号不是保留原文的理由：引号内的阵营、组织、地名、物品、人名"
    "同样要译成中文；只有代码、型号、条目ID、文件路径才保留原文；\n"
    "12. 术语提示只在语义相符时采用；同一人物、物品、地名全篇保持同一个译名；"
    "普通语境里的同形词不要硬套专名译名。\n"
    "\n"
    "英语口语对照示例（左为生硬译法，右为地道中文；照右边的路子写）：\n"
    "· 「Listen, I've got something to tell you.」→「哎，我有事找你。」\n"
    "· 「We can't stay here anymore.」→「这儿咱待不下去了。」\n"
    "· 「I don't know how you're going to do it.」→「我不知道你咋弄，反正东西我得要。」\n"
    "· 「Do you understand what's going on?」→「你懂现在发生啥了吗？」\n"
    "· 「Get out of my sight.」→「滚，别让我看见你。」\n"
    "· 「You're a dead man.」→「你死定了。」\n"
    "\n"
    "只输出译文本身，不加任何解释或说明；原文已是中文时原样返回；"
    "不要把英文原文抄进译文，也不要把规则复述出来。\n"
    "最后再强调两条：占位符、控制符、条目ID 必须逐字保留；"
    "译文里不得残留整词或整段的英文或俄文原文。"
)

# 对话场景批量翻译：只用于连续短对话。1.8B 实测能保留 [n] 行号。
# 短标签/UI/物品名：不要套用对话口语规则。
TRANSLATION_SYS_PROMPT_LABEL = (
    "你是《潜行者 S.T.A.L.K.E.R.》的游戏本地化译者。输入是物品、按钮、"
    "菜单或界面短标签。译成简洁、直白、统一的简体中文；不加语气词，"
    "不扩写，不解释。占位符、控制符、代码/条目ID逐字保留。"
    "专名使用游戏约定译名；只输出译文本身。"
)

TRANSLATION_BATCH_SYS_PROMPT = (
    "你是《潜行者 S.T.A.L.K.E.R.》的场景对话译者。输入是多行连续对话，"
    "每行以 [n] 开头。逐行翻译成自然、口语化的简体中文；必须保留每行"
    "开头的 [n] 编号、行数和顺序，不合并、不拆分、不解释、不加角色名。"
    "上下文里前后句只帮助理解，不要翻译没有编号的内容。"
    "术语提示只在语义相符时采用；引号内的阵营、地名、物品、人名也要译成中文。"
    "输入里的 $$R0$$、$$R1$$ 等控制符必须逐字原样保留，不得翻译、"
    "拆分或改动。"
    "只输出带编号的译文行。"
)

# 模型偶尔会把整段系统提示词当译文回显。用固定锚点做硬校验，
# 不依赖提示词本身（换提示词时只需保留这些锚点）。
_PROMPT_ECHO_MARKERS = (
    "你是《潜行者",
    "S.T.A.L.K.E.R.》后末日游戏的专业译员",
    "只输出译文本身",
    "口语对照示例",
    "术语(用给定译名)",
)


def looks_like_prompt_echo(text):
    s = text or ""
    return any(marker in s for marker in _PROMPT_ECHO_MARKERS)


# ★ 2.0.8 场景上下文串入：模型（尤其 1.8B）会把 user prompt 前面那段
# 「前一句/后一句」当成正文一起翻出来，连标签都照抄。这些标签绝不该出现在
# 译文里，用固定锚点硬校验，与提示词无关。
_CONTEXT_ECHO_MARKERS = (
    "前一句：", "后一句：", "上一句：", "下一句：",
    "上下文（仅供理解", "上下文(仅供理解",
)


def looks_like_context_echo(text):
    s = text or ""
    return any(marker in s for marker in _CONTEXT_ECHO_MARKERS)


def looks_like_context_bleed(src, out):
    """译文串入场景上下文：显式标签，或极短源文吐出超长段落。

    后者兜住模型只翻了上下文正文、却没照抄「前一句：」标签的情况：
    例如源文 `Я не готов.`（11 字符）却输出一整段上一条的译文。
    """
    if not out:
        return False
    if looks_like_context_echo(out):
        return True
    # ★ 2.0.12：串段污染——模型把相邻条目的译文一并吐进当前条目，
    # 形成「另一段译文\\n\\n正确译文」。特征是源文无换行、译文却出现双 \\n，
    # 或译文长度远超源文。旧判据只抓极短源文（<24），抓不到长对话串段。
    s = (src or "").strip()
    o = (out or "").strip()
    if s and "\\n" not in s and "\\n" in o:
        return True
    if o.count("\\n") >= 2 and s.count("\\n") == 0:
        return True
    return len(s) < 24 and len(o) > 150


# ★ 2.0.9 数字保真：金额/年份/坐标等大额数字绝不能被模型改写。
# 实测 dm_lin_treasure_33 源文「30000 рублей」被译成「15,000卢布」（报酬腰斩）。
# 只检 >=3 位的数字（<3 位多为编号/序号，且 2006-м→2006年 等屈折会误报）。
_BIG_NUM_RE = re.compile(r"(?<![\d,.])(\d{3,})(?![\d,.])")
_ZH_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _zh_number_values(text):
    """把译文里的中文计数法还原成数值集合，用于核对原文大额数字。

    支持：1.5万 / 3万 / 两万 / 三万 / 十五万 / 100,000 / 100 000。
    中文汉字数字只覆盖万位以内的常见写法，够核对金额即可。
    """
    vals = set()
    if not text:
        return vals
    # 阿拉伯数字 + 万/千（含千分位逗号、空格）
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([万千])", text):
        try:
            base = float(m.group(1).replace(",", ""))
            mult = 10000 if m.group(2) == "万" else 1000
            v = base * mult
            vals.add(int(v) if v == int(v) else v)
        except ValueError:
            pass
    # 纯汉字数字串，如 三万 / 一万五千 / 两万 / 十五万 / 一百二十五
    # 一次性解析整串（含万/千/百/十多级），避免「一万五千」被拆成 1万+5千。
    for m in re.finditer(r"[零〇一二两三四五六七八九十百千万]+", text):
        seg = m.group(0)
        total, section, num = 0, 0, 0
        for i, ch in enumerate(seg):
            if ch in _ZH_DIGITS:
                num = _ZH_DIGITS[ch]
            elif ch in ("十", "百", "千", "万"):
                unit = {"十": 10, "百": 100, "千": 1000, "万": 10000}[ch]
                if ch == "万":
                    # 万之前的部分（section+num）是"几万"
                    total += (section + num or 1) * unit
                    section, num = 0, 0
                else:
                    section += (num or 1) * unit
                    num = 0
            # 末端数字累加到 section/num 由下方统一处理
        value = total + section + num
        if value:
            vals.add(value)
    return vals


def missing_big_numbers(src, out):
    """源文大额数字在译文中找不到（原样/千分位/中文计数）时返回缺失列表。

    找不到 = 模型改写或漏掉了关键数字（金额、年份、坐标、密码），
    必须拦截。返回空列表表示保真通过。
    """
    if not src or not out:
        return []
    s = str(src)
    # ★ 2.0.21（IWP 产物审计）：色码 %c[r,g,b,a] 内的数字是渲染参数，
    # 不参与保真校验——实测 m249 百科的 109/193 为 %c 分量，被当缺失
    # 金额补成「（原文数字 109）（原文数字 193）」注记污染译文。
    s = re.sub(r"%c\[[^\]]*\]", " ", s)
    src_nums = set(_BIG_NUM_RE.findall(s))
    # ★ 2.0.11：型号尾号与年份区间不参与保真校验。
    # 实测武器长段描述含 `FN FNX-45` / `developed in 2007-2009` / `2800`，
    # 旧逻辑把型号 45、年份 2007/2009 也当金额，模型译成中文后数字对不齐，
    # 两次重试仍判缺失 → 整段保留英文（比纯漏译更糟）。
    # 只豁免「字母-数字」的型号尾号和「4位-4位」年份；独立金额/坐标仍严格校验。
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9]*[-‐‑‒–—]\s*(\d{3,})", s):
        src_nums.discard(m.group(1))
    # ★ 2.0.14：年份区间不再全部豁免。旧逻辑把 2007-2009 两端都丢掉，
    # 模型把「developed in 2007-2009」改成「P220 / 220」时闸门完全放行
    # （实测 esc_2_12_nimble_get_order_131）。改为只豁免区间尾年，保留
    # 起始年参与校验——译文若两个年份都换成别的数字就会被拦下。
    for m in re.finditer(r"\b(\d{4})\s*[-‐‑‒–—]\s*(\d{4})\b", s):
        src_nums.discard(m.group(2))
    if not src_nums:
        return []
    # 译文里的阿拉伯数字（去千分位逗号/空格）
    out_nums = set(re.findall(r"\d+", re.sub(r"[,\s]", "", str(out))))
    out_nums |= {str(int(float(x))) for x in out_nums if x.isdigit()}
    out_vals = _zh_number_values(out)
    missing = []
    for n in src_nums:
        # 原样出现（含千分位：源 30000，译 30,000 → 去逗号后 30000）
        if n in out_nums:
            continue
        iv = int(n)
        if iv in out_vals or float(iv) in out_vals or iv in out_nums:
            continue
        missing.append(n)
    # ★ 2.0.24（IWP 全量审计 D2）：年代的中文纪年表述视为保真。
    # 实测 "1960s / 1990s" 译作「20世纪60年代/90年代」被严格数字核对
    # 判缺失，59 条注记里 38 条是这种误报。译文出现「YY年代」（可带
    # 世纪前缀）或「YYYY年代」时，认定对应十年位年份已表达。
    # 通用规则，不依赖具体 MOD/文本。
    if missing:
        keep = []
        for n in missing:
            if (len(n) == 4 and n.endswith("0")
                    and re.search(r"(?:(?:1[5-9]|20)\s*世纪\s*)?"
                                  + n[2:4] + r"\s*年代|" + n + r"年代",
                                  str(out))):
                continue
            keep.append(n)
        missing = keep
    # 确定性输出顺序：集合迭代顺序随进程哈希变化，会让补数字/测试不稳定
    missing.sort(key=lambda x: (len(x), x))
    return missing


def patch_missing_numbers(src, out, skip=()):
    """数字闸门两次重试仍缺失时的兜底：不整段回退原文，而是把缺失的
    关键数字按出现顺序补进译文，保留已经译好的正文。

    实测武器长段描述（FNX-45 段落）因金额 2800 两次重试都没带上，
    旧逻辑整段保留英文——玩家看到的是一整段看不懂的外文，比数字
    缺失严重得多。这里改为：译文已有中文时，把缺失金额以「（2800）」
    形式补到对应位置附近；找不到锚点则追加到末尾。

    ★ 2.0.24：skip 传入缺失型号串——数字若是某个缺失型号的一部分
    （AK-103 里的 103），由型号保真闸门整串补「（AK-103）」即可，
    再补裸「（103）」就是双注记（实测交互缺陷，回归测试抓出）。
    """
    if not src or not out or not any("\u4e00" <= c <= "\u9fff" for c in out):
        return None                      # 无中文的译文不能补，交给调用方回退
    missed = missing_big_numbers(src, out)
    if not missed:
        return out
    if skip:
        _sk = [str(t) for t in skip]
        missed = [n for n in missed
                  if not any(n in t for t in _sk)]
        if not missed:
            return out
    fixed = out
    for n in missed:
        # 优先插到译文里出现的货币/单位词之后（卢布/美元/格里夫纳等）
        # ★ 2.0.24（IWP 全量审计 D2）：注记字样「原文数字」退役——
        # 兜底补的是裸数字，玩家读到的仍是成品文本而非审计注记。
        m = re.search(r"(卢布|美元|元|格雷|格里夫纳|coin| coins)", fixed)
        if m:
            pos = m.end()
            fixed = fixed[:pos] + "（%s）" % n + fixed[pos:]
        else:
            fixed = fixed.rstrip() + "（%s）" % n
    return fixed


_RU_PROFANITY_RE = re.compile(
    r"дерьм|заткн|хрен|чёрт|черт|бля|хуй|пизд|ебат|еб[ао]|говн|суч|"
    r"сдох|жоп|муд[ао]|уёб|уеб|нахуй|иди ты|пошёл ты|пошел ты", re.I)
# ★ 2.0.13：英文源文的粗口（STALKER 大量英语对话/广播结尾"Weird shit"等）
# 旧版只认俄语，英文脏话完全绕过粗口力度闸门，模型常把 shit/fuck 软化成
# "奇怪的东西"。用词边界锚定，避免把 "shot"(发射) 里的 sh 误伤。
_EN_PROFANITY_RE = re.compile(
    r"\b(?:shit|fuck(?:ing|ed|er)?|bitch|bastard|asshole|dick|piss(?:ed|ing)?|"
    r"crap|damn|bullshit|motherfucker|wtf)\b", re.I)
# 中文译文里出现连续两个以上西里尔字母，基本就是模型漏译/原样保留。
_CYR_WORD_RE = re.compile(r"[А-Яа-яЁё]{2,}")
_ZH_PROFANITY_RE = re.compile(
    r"他妈|妈的|操(?:你|他|她|它|死|蛋|逼|屄)|我操|"
    r"靠[！!。.，,]|滚(?:蛋|开|！|!|。|，)|闭嘴|混蛋|畜生|王八|去死|"
    r"该死|见鬼|鸡巴|屌|日你|找死|活腻|屎|狗屎|放屁")
_PROFANITY_RETRY_NOTE = (
    "本句原文含粗口、辱骂或威胁，必须保留同等粗鲁力度：不得弱化、"
    "删除或改成书面语。按原文直译，例如 на хрен→他妈/滚，"
    "дерьмо→屎/狗屎，заткнись→闭嘴。")
_PLACEHOLDER_RETRY_NOTE = (
    "必须逐字保留输入里的 $$R0$$、$$R1$$ 等控制占位符，"
    "数量、顺序和写法都不能改；只翻译占位符以外的正文。")
_RESIDUAL_CYRILLIC_NOTE = (
    "译文里仍夹着俄文（%s）。引号里的阵营、组织、地名、物品、人名"
    "同样要译成中文；只有代码、型号、条目ID、文件路径保留原文，"
    "不要保留俄文单词。")


def has_source_profanity(text):
    """源文含粗口（俄语/英语/中文任一）——决定是否启用粗口力度闸门。"""
    return bool(_RU_PROFANITY_RE.search(text or "")
                or _EN_PROFANITY_RE.search(text or "")
                or _ZH_PROFANITY_RE.search(text or ""))


def has_zh_profanity(text):
    return bool(_ZH_PROFANITY_RE.search(text or ""))


def has_cjk(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

GLOSSARY_HINT_HEADER = "术语(用给定译名)：\n"
# ★ 2.0.3 官方措辞 A/B：Hy-MT2 官方术语模板是「参考下面的翻译：X 翻译成 Y」，
# 贴近训练分布的措辞可能遵循率更高；做成开关由用户对比。
GLOSSARY_HINT_HEADER_OFFICIAL = "参考下面的翻译：\n"
GLOSSARY_HINT_BUDGET = 240
GLOSSARY_HINT_STOPWORDS = frozenset((
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "нет", "есть", "был", "была", "были", "быть", "это", "этот",
    "для", "или", "если", "уже", "ещё", "еще", "тут", "там", "тогда", "чем",
    "кто", "где", "когда", "почти", "всего", "всегда", "очень", "надо",
    "можно", "нужно", "теперь", "потом", "себя", "себе", "свой", "свою",
    "один", "два", "три", "без", "под", "над", "про", "при", "между",
)) | frozenset((
    # 英语源文同样要过滤功能词，否则术语提示预算会被 the/a/of 吃掉。
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "and", "or",
    "is", "are", "was", "were", "be", "been", "being", "with", "by",
    "from", "this", "that", "these", "those", "it", "its", "as", "not",
    "no", "yes", "you", "your", "yours", "he", "she", "they", "we", "i",
    "me", "my", "our", "their", "his", "her", "them", "us", "do", "does",
    "did", "can", "could", "will", "would", "should", "may", "might",
    "must", "have", "has", "had", "so", "but", "if", "then", "than",
    "also", "very", "just", "there", "here", "what", "which", "who",
    "whom", "whose", "when", "where", "why", "how", "all", "any", "some",
))


def glossary_hint_block(pairs, budget=GLOSSARY_HINT_BUDGET, official=False):
    """把命中术语压成尽可能小的提示块，返回 (文本, 计费字符数)。

    计费字符数是**保守上界**（每行按含换行计、另预留与正文之间的空行），
    略大于实际块长；预算判断因此偏安全，不会算少。

    规则：跳过功能词、大小写去重、按预算截断；超预算的长术语直接跳过而不是
    截半（半截术语反而会误导模型）。实测把原来的 15 条上限换成 240 字符预算
    后，每条请求少注入约 25% 字符，且长专名不再被短词挤掉。

    official=True 用官方模板措辞（「X 翻译成 Y」），False 用自研格式（X=Y）。
    """
    seen = set()
    lines = []
    header = (GLOSSARY_HINT_HEADER_OFFICIAL if official
              else GLOSSARY_HINT_HEADER)
    used = len(header) + 2
    fmt = "%s 翻译成 %s" if official else "%s=%s"
    for k, v in pairs:
        k = str(k).strip()
        lk = k.lower()
        if not k or lk in seen or lk in GLOSSARY_HINT_STOPWORDS:
            continue
        v = str(v)
        cost = len(k) + len(v) + (4 if official else 2)
        if used + cost > budget:
            continue
        seen.add(lk)
        lines.append(fmt % (k, v))
        used += cost
    if not lines:
        return "", 0
    return header + "\n".join(lines), used

EXTRA_PROMPT_DEMO = (
    "· 语气语域：译文用冷峻简短的军事简报口吻，不用敬语、不加句末语气词。\n"
    "· 译名风格：怪物与武器名称一律音译，不意译。\n"
    "· 世界观补充：Зона=禁区，аномалия=异常点，мутант=变异体。\n"
    "· 粗口：原文脏话按原意直译并保留同等力度，不要改写成书面语。"
)


_LLAMA_TAG = "b11612"
_GH_BASE = ("https://github.com/ggml-org/llama.cpp/releases/download/"
            + _LLAMA_TAG + "/llama-" + _LLAMA_TAG + "-bin-win-{variant}-x64.zip")
_GH_CUDART = ("https://github.com/ggml-org/llama.cpp/releases/download/"
              + _LLAMA_TAG + "/cudart-llama-bin-win-cuda-12.4-x64.zip")
_GH_CUDART13 = ("https://github.com/ggml-org/llama.cpp/releases/download/"
                + _LLAMA_TAG + "/cudart-llama-bin-win-cuda-13.4-x64.zip")

# 官方 SHA-256（2026-10-23 取自发布方元数据）：
# 运行库 = llama.cpp b11612 GitHub Release asset digest；
# 模型 = HuggingFace LFS oid（sha256），并核对官方字节数。
_LLAMA_SHA256 = {
    "cpu": "b8456ccb1be98365a5a2227efa5c495508a6a3ebce881c6189a1dbeaaa170b69",
    "cuda-12.4": "4c6d8afd23475f6eded74c4a3019a6746aab83183a3158f3fc4de1cb7c4f5f12",
    "vulkan": "e62f0674970a70b007fa3c425d191ceacabbf02406d6c9ac8b6f27cd23abe9e9",
    "cudart-12.4": "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
    # CUDA 13.4（2026-10-29 取自同一 Release 的 asset digest）
    "cuda-13.4": "e1c59ee10ead069dee12bf5a1f5a3ed294b144ea3f9c5006aa89675c5306de38",
    "cudart-13.4": "738f8c251ac22b70c3ae6f83a10cf222725df0395246a2cf58f32bdb85fbe668",
}
_MODEL_HASHES = {
    "Hy-MT2-7B-Q4_K_M.gguf": (
        "9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b",
        4624648896),
    "Hy-MT2-1.8B-Q8_0.gguf": (
        "5c3fe0b1408a5ceb0143184ef247b11b579c525f4b02b060e6c851bb76fef1a4",
        1908528192),
}
_GH_PROXIES = [
    ("GitHub 直连", "{url}"),
    ("ghproxy.net 代理", "https://ghproxy.net/{url}"),
    ("gh.h233 代理", "https://gh.h233.eu.org/{url}"),
    ("ghfast 代理", "https://ghfast.top/{url}"),
    ("geekertao 代理", "https://gh.geekertao.top/{url}"),
    ("dpik 代理", "https://gh.dpik.top/{url}"),
    ("llkk 代理", "https://gh.llkk.cc/{url}"),
    ("moeyy 代理", "https://github.moeyy.xyz/{url}"),
    ("ghgo 代理", "https://ghgo.xyz/{url}"),
    ("gh-proxy 代理", "https://gh-proxy.com/{url}"),
    ("imciel 代理", "https://ghproxy.imciel.com/{url}"),
    ("ddlc 代理", "https://gh.ddlc.top/{url}"),
    ("idayer 代理", "https://gh.idayer.com/{url}"),
    ("nuaa 镜像", "https://download.nuaa.cf/{url}"),
    ("mirror.ghproxy 代理", "https://mirror.ghproxy.com/{url}"),
    ("ghproxy.cc 代理", "https://ghproxy.cc/{url}"),
    ("960316 代理", "https://gh.960316.xyz/{url}"),
    ("99988866 代理", "https://gh.api.99988866.xyz/{url}"),
    # 2026-10-29 实测可用（不同网络下可用性会变；插件逐个测速择优）
    ("gh-proxy.org 代理", "https://gh-proxy.org/{url}"),
    ("gh.nxnow 代理", "https://gh.nxnow.top/{url}"),
    ("catmak 代理", "https://gh.catmak.name/{url}"),
    ("monlor 代理", "https://gh.monlor.com/{url}"),
]
_GH_DOMAIN_MIRRORS = [
    ("kkgithub 域名镜像", "https://kkgithub.com/{path}"),
]


def _gh_sources(url):
    path = url.split("github.com/", 1)[-1]
    sources = [(n, t.format(url=url)) for n, t in _GH_PROXIES]
    sources += [(n, t.format(path=path)) for n, t in _GH_DOMAIN_MIRRORS]
    return sources

_CUDART_MARKS = ("cudart64", "cublas", "nvrtc")
_CUDART_MARK = ".cudart_installed.json"

def _model_sources(repo_ms, repo_hf, filename):
    return [
        ("ModelScope 主站",
         "https://modelscope.cn/models/%s/resolve/master/%s" % (repo_ms, filename)),
        ("ModelScope 主站(www)",
         "https://www.modelscope.cn/models/%s/resolve/master/%s" % (repo_ms, filename)),
        ("ModelScope CDN",
         "https://modelscope.cn/api/v1/models/%s/repo?Revision=master"
         "&FilePath=%s" % (repo_ms, filename)),
        ("ModelScope CDN(www)",
         "https://www.modelscope.cn/api/v1/models/%s/repo?Revision=master"
         "&FilePath=%s" % (repo_ms, filename)),
        ("ModelScope CDN(download)",
         "https://modelscope.cn/api/v1/models/%s/repo?Revision=master"
         "&FilePath=%s&download=true" % (repo_ms, filename)),
        ("hf-mirror（HF 国内镜像）",
         "https://hf-mirror.com/%s/resolve/main/%s" % (repo_hf, filename)),
        ("hf-mirror.net（HF 国内镜像 2）",
         "https://hf-mirror.net/%s/resolve/main/%s" % (repo_hf, filename)),
        ("HuggingFace 官方",
         "https://huggingface.co/%s/resolve/main/%s" % (repo_hf, filename)),
        ("HuggingFace 官方(hf.co)",
         "https://hf.co/%s/resolve/main/%s" % (repo_hf, filename)),
    ]


TIER_PROFILES = {
    "7b": {
        "task": "model_7b",
        "file": "Hy-MT2-7B-Q4_K_M.gguf",
        "repo_ms": "Tencent-Hunyuan/Hy-MT2-7B-GGUF",
        "repo_hf": "tencent/Hy-MT2-7B-GGUF",
        "size_gb": 4.4,
        "ram_gb": 8,
        "ctx": 8192,
        "combo": "精雕 · Hy-MT2 7B Q4_K_M｜最精确·质量首选｜速度中等｜上下文8K｜约8GB内存",
        "download": "① 精雕 Hy-MT2-7B Q4_K_M（4.4GB · 质量最高/最精确 · 速度中等）",
        "pos": ("质量：最高（复杂长难句、专业领域、多语言指令遵循最强）\n"
                "精度：最精确，专名/语气/占位符保持最好\n"
                "速度：中等（CPU 短句约数秒/条）\n"
                "上下文：8192 token 质量窗（约 3800 字符/段，超长自动分句）\n"
                "定位：终校级译文，NS7 这类超长日记/大段任务文本首选"),
    },
    "mt2": {
        "task": "model_mt2",
        "file": "Hy-MT2-1.8B-Q8_0.gguf",
        "repo_ms": "Tencent-Hunyuan/Hy-MT2-1.8B-GGUF",
        "repo_hf": "tencent/Hy-MT2-1.8B-GGUF",
        "size_gb": 1.8,
        "ram_gb": 4,
        "ctx": 16384,
        "combo": "精准 · Hy-MT2 1.8B Q8_0｜高质量·快速｜上下文16K｜约4GB内存",
        "download": "② 精准 Hy-MT2-1.8B Q8_0（1.8GB · 质量高/精确 · 速度快速）",
        "pos": ("质量：高（新一代小模型，官方称整体超越主流商业翻译 API）\n"
                "精度：精确，较上代小模型在真实场景与指令遵循上明显提升\n"
                "速度：快速（1.8B 轻量，CPU 也明显快于 7B）\n"
                "上下文：16384 token 质量窗（约 7600 字符/段，超长自动分句）\n"
                "定位：日常汉化主力，质量/速度最均衡的一档"),
    },
    "custom": {
        "task": None,
        "file": None,
        "size_gb": 0,
        "ram_gb": 0,
        "ctx": 8192,
        "combo": "自选 · 任意 GGUF 离线模型｜本机自选文件·自定上下文",
        "pos": ("用法：点「选择已有模型」指定本机任意 .gguf 文件（拖入亦可），\n"
                "并在右侧选择该模型要使用的上下文长度。\n"
                "要求：指令/对话微调的聊天模型（GGUF 内置 chat template）；\n"
                "非混元模型的翻译风格与术语遵循由模型本身决定。\n"
                "上下文：按所选长度生效，超长文本仍自动分句；请按模型大小与\n"
                "内存选择（经验：7B/Q4 约 8GB，1.8B/Q8 约 4GB，上下文越大越吃内存）"),
    },
}
TIER_ORDER = ("7b", "mt2", "custom")
BUILTIN_TIERS = ("7b", "mt2")
CUSTOM_CTX_CHOICES = (4096, 8192, 16384, 32768)
TIER_SHORT = {
    "7b": "精雕7B｜质量最高最精确｜速度中等｜8K上下文",
    "mt2": "精准1.8B｜高质量精确｜快速｜16K上下文",
    "custom": "自选模型｜本机任意 GGUF｜上下文自定",
}

MODE_LABELS = {
    "auto": "auto 自动识别全部",
    "text": "text 文本XML",
    "cfgxml": "cfgxml 配置XML",
    "ltx": "ltx 配置",
    "scriptL": "scriptL 脚本就地替换",
    "scriptE": "scriptE 脚本提取",
}
MODE_ORDER = ("auto", "text", "cfgxml", "ltx", "scriptL", "scriptE")

# 多路翻译：引擎槽位数 = 这里的路数；显存/内存随路数线性增加（权重只加载一份）。
# ★ 2.0.2：路数选项按检测到的硬件**动态生成**（1..max_n，封顶 PARALLEL_CAP）——
#   GPU 后端按显存预算、CPU 后端按物理内存预算；预算口径与「显存估算」的 90%
#   警戒线同源。并行单位是**文件**（每路独立翻自己的表，互不影响），且需要跨
#   文件一致的东西（会话词表/任务级 TM/复用译文）都在并发前单线程算好，所以
#   路数只改变"同时翻几个文件"，不改变任何一条文本的译文。硬件/模型信息不足
#   时回落固定档 PARALLEL_ORDER（1/2/4），绝不编造。
PARALLEL_ORDER = (1, 2, 4)       # 兜底固定档（信息不足时仍可用）
PARALLEL_CAP = 16                # 动态路数上限：吞吐收益递减 + 槽位调度开销
PARALLEL_GPU_BUDGET = 0.9        # 显存预算比例（与 refresh_vram_estimate ⚠ 同源）
PARALLEL_CPU_BUDGET = 0.7        # 物理内存预算比例（系统/其它进程留量更大）
PARALLEL_LABELS = {
    1: "1 路（最稳，默认）",
    2: "2 路（推荐，实测约 1.7×）",
    4: "4 路（需 ≥8GB 显存）",
}
# 旧的静态标签语义（2.0.2 起仅在回落固定档时使用）。


def _model_task(tier):
    p = TIER_PROFILES[tier]
    digest, size_bytes = _MODEL_HASHES[p["file"]]
    return {
        "label": p["download"],
        "kind": "model", "tier": tier,
        "filename": p["file"],
        "size_hint": p["size_gb"],
        "size_bytes": size_bytes,
        "sha256": digest,
        "sources": _model_sources(p["repo_ms"], p["repo_hf"], p["file"]),
    }


DOWNLOAD_TASKS = {
    TIER_PROFILES[t]["task"]: _model_task(t) for t in BUILTIN_TIERS
}
DOWNLOAD_TASKS.update({
    "bin_cpu": {
        "label": "llama.cpp CPU 运行库（约 19 MB，兼容性最好）",
        "kind": "zip", "variant": "cpu",
        "filename": "llama-server.exe",
        "size_hint": 0.019,
        "sha256": _LLAMA_SHA256["cpu"],
        "sources": _gh_sources(_GH_BASE.format(variant="cpu")),
    },
    "bin_cuda": {
        "label": "llama.cpp CUDA 12 运行库（约 630 MB，含运行时，NVIDIA 最快）",
        "kind": "zip", "variant": "cuda",
        "filename": "llama-server.exe",
        "size_hint": 0.63,
        "sha256": _LLAMA_SHA256["cuda-12.4"],
        "extra_runtime": {
            "url": _GH_CUDART,
            "marks": _CUDART_MARKS,
            "label": "CUDA 运行时（cudart/cublas）",
            "sha256": _LLAMA_SHA256["cudart-12.4"],
        },
        "sources": _gh_sources(_GH_BASE.format(variant="cuda-12.4")),
    },
    "bin_cuda13": {
        "label": "llama.cpp CUDA 13.4 运行库（约 580 MB，需 580+ 驱动 / Turing+）",
        "kind": "zip", "variant": "cuda13",
        "filename": "llama-server.exe",
        "size_hint": 0.58,
        "sha256": _LLAMA_SHA256["cuda-13.4"],
        "extra_runtime": {
            "url": _GH_CUDART13,
            "marks": _CUDART_MARKS,
            "label": "CUDA 13 运行时（cudart/cublas）",
            "sha256": _LLAMA_SHA256["cudart-13.4"],
        },
        "sources": _gh_sources(_GH_BASE.format(variant="cuda-13.4")),
    },
    "bin_vulkan": {
        "label": "llama.cpp Vulkan GPU 运行库（约 31 MB，A/N/I 显卡通用）",
        "kind": "zip", "variant": "vulkan",
        "filename": "llama-server.exe",
        "size_hint": 0.031,
        "sha256": _LLAMA_SHA256["vulkan"],
        "sources": _gh_sources(_GH_BASE.format(variant="vulkan")),
    },
})

VARIANT_LABELS = {
    "cpu": "CPU（兼容性好，不占显存）",
    "cuda": "CUDA 12.4（NVIDIA 显卡，兼容性最好）",
    "cuda13": "CUDA 13.4（需 580+ 驱动 / Turing+，可选）",
    "vulkan": "Vulkan（A/N/I 显卡通用加速）",
}
VARIANT_ORDER = ("cpu", "cuda", "cuda13", "vulkan")
GPU_VARIANTS = ("cuda", "cuda13", "vulkan")

TIER_LABELS = [TIER_SHORT[t] for t in TIER_ORDER]
_LABEL_TO_TIER = {TIER_SHORT[t]: t for t in TIER_ORDER}
_LABEL_TO_TIER.update({TIER_PROFILES[t]["combo"]: t for t in TIER_ORDER})
_MODEL_TASK_ORDER = [TIER_PROFILES[t]["task"] for t in BUILTIN_TIERS]
TASK_KEY_ORDER = _MODEL_TASK_ORDER + ["bin_cpu", "bin_cuda",
                                    "bin_cuda13", "bin_vulkan"]
TIER_BY_TASK = {TIER_PROFILES[t]["task"]: t for t in BUILTIN_TIERS}
TIER_FILE = {t: TIER_PROFILES[t]["file"] for t in BUILTIN_TIERS}
TIER_ALIAS = {"1b8": "mt2", "mt15": "mt2"}


def _fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.2f %s" % (n, unit)
        n /= 1024.0


def _fmt_eta(sec):
    if sec is None or sec < 0 or sec > 3600 * 99:
        return "--:--:--"
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def detect_nvidia_gpu():
    """通过 nvidia-smi 探测 NVIDIA 独显。

    返回 (名称, 显存MB, 驱动版本, compute_cap)；无 N 卡/取不到返回 None。
    纯本地、无网络；nvidia-smi 随驱动安装。早期只取前两项，现在带上
    驱动版本与计算能力，用于判断能否使用 CUDA 13.4 构建。
    """
    def query(fields):
        return subprocess.run(
            ["nvidia-smi", "--query-gpu=" + fields,
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)))

    try:
        r = query("name,memory.total,driver_version,compute_cap")
        legacy = False
        if r.returncode != 0:
            # 老驱动不认识 compute_cap：退回两字段查询，至少别把 N 卡
            # 误判成"没有独显"（那样会白白降到 Vulkan/CPU）。
            r = query("name,memory.total")
            legacy = True
        if r.returncode != 0:
            return None
        lines = (r.stdout or "").strip().splitlines()
        if not lines:
            return None
        parts = [x.strip() for x in lines[0].split(",")]
        name = parts[0] if parts else ""
        mem = "".join(ch for ch in (parts[1] if len(parts) > 1 else "")
                      if ch.isdigit())
        driver = "" if legacy or len(parts) <= 2 else parts[2]
        try:
            cc = 0.0 if legacy or len(parts) <= 3 else float(parts[3])
        except (TypeError, ValueError):
            cc = 0.0
        return (name, int(mem) if mem else 0, driver, cc)
    except Exception:
        return None


def cuda13_supported(driver, compute_cap):
    """CUDA 13.x 构建的门槛：驱动主版本 ≥ 580 且计算能力 ≥ 7.5（Turing+）。

    这是"可尝试"的判据，不是保证：OEM/笔记本驱动、Optimus、UMD 与 Toolkit
    版本差异都可能让实际初始化失败，所以调用方还要用真实启动 + GPU 层验证兜底。
    """
    try:
        major = int(str(driver).strip().split(".")[0])
    except (TypeError, ValueError):
        return False
    try:
        cc = float(compute_cap)
    except (TypeError, ValueError):
        return False
    return major >= 580 and cc >= 7.5


_IGPU_MARKERS = ("microsoft basic", "basic display", "remote", "citrix",
                 "parsec", "vmware", "virtual", "hyper-v", "meta display",
                 "oray", "sunlogin", "todesk", "gameviewer", "usb display")


def _gpu_registry_vram():
    """从注册表取真实显存（字节）：{驱动描述: 字节数}，取不到返回 {}。

    为什么不用 Win32_VideoController.AdapterRAM：它是 uint32，≥4GB 就回绕
    （本机 RTX 3060 6GB 报 4293918720 字节 → 被算成 4095MB 再截成 4096）。
    HardwareInformation.qwMemorySize 是 64 位，能拿到真实值（本机实测 6.0GiB）。
    """
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
          "Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\"
          "{4d36e968-e325-11ce-bfc1-08002be10318}' -ErrorAction SilentlyContinue"
          " | ForEach-Object { $p=Get-ItemProperty $_.PSPath"
          " -ErrorAction SilentlyContinue;"
          " if ($p.DriverDesc -and $p.'HardwareInformation.qwMemorySize')"
          " { \"$($p.DriverDesc)|$($p.'HardwareInformation.qwMemorySize')\" } }")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=12,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        out = r.stdout.decode("utf-8", "ignore")
    except Exception:
        return {}
    table = {}
    for line in out.splitlines():
        name, _, val = line.strip().partition("|")
        val = val.strip()
        if name and val.isdigit() and int(val) > 0:
            table[name] = int(val)
    return table


def _is_integrated_gpu(low_name):
    """判断是不是集显/虚拟卡（入参已小写）。

    保守策略：拿不准就按集显处理 → 最终退回 CPU。相比"猜成独显去开
    Vulkan、结果比 CPU 还慢"，退回 CPU 是安全得多的错法。
    """
    if any(k in low_name for k in _IGPU_MARKERS):
        return True
    if "intel" in low_name and "arc" not in low_name:
        return True
    if "radeon" in low_name or "amd" in low_name:
        if ("rx " in low_name or " rx" in low_name or "pro " in low_name
                or "instinct" in low_name or "vii" in low_name):
            return False
        return True
    return False


def detect_all_gpus():
    """列出全部显卡（CIM 取名 + 注册表取真实显存），返回 [(名称, 显存MB)]；
    显存未知记为 0，不编造近似值。"""
    vram_table = _gpu_registry_vram()
    try:
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              "Get-CimInstance Win32_VideoController | ForEach-Object {"
              "$n=$_.Name; $m=[int64]($_.AdapterRAM); \"$n|$m\"}")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=12,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        out = r.stdout.decode("utf-8", "ignore")
        gpus = []
        for line in out.splitlines():
            name, _, mem = line.strip().partition("|")
            if not name:
                continue
            mb = 0
            real = vram_table.get(name)
            if real:
                mb = int(real // (1024 * 1024))
            elif mem.isdigit():
                mb = int(mem) // 1024 // 1024
                if mb > 4095:
                    mb = 0
            gpus.append((name, mb))
        return gpus
    except Exception:
        return []


def detect_hardware():
    """整机硬件勘察 → 推荐后端与模型档。返回 dict：
    variant / tier / gpu_name / vram / short（一句话）。"""
    nv = detect_nvidia_gpu()
    others = detect_all_gpus()
    discrete_other = None
    for name, vram in others:
        low = name.lower()
        if "nvidia" in low:
            continue
        if _is_integrated_gpu(low):
            continue
        discrete_other = (name, vram)
    if nv:
        name, vram, driver, cc = nv
        variant = "cuda"          # 默认 12.4；13.4 需用户显式选择
        cuda13_ok = cuda13_supported(driver, cc)
        tier = "7b" if vram >= 5120 else "mt2"
        short = "%s %dGB → CUDA 12.4 + %s%s" % (
            name.replace("NVIDIA GeForce ", ""), round(vram/1024, 1),
            {"7b": "精雕7B", "mt2": "精准1.8B"}[tier],
            ("（CUDA 13.4 可用）" if cuda13_ok else ""))
        return {"variant": variant, "tier": tier, "gpu_name": name,
                "vram": vram, "short": short, "driver": driver,
                "compute_cap": cc, "cuda13_ok": cuda13_ok}
    if discrete_other:
        name, vram = discrete_other
        tier = "mt2"
        return {"variant": "vulkan", "tier": tier, "gpu_name": name,
                "vram": vram,
                "short": "%s → Vulkan + 精准1.8B" % name}
    return {"variant": "cpu", "tier": "mt2", "gpu_name": "",
            "vram": 0, "short": "未检测到独显 → CPU + 精准1.8B"}



_ENC_FALLBACK = ("cp1251", "cp1252")
_ENC_FORCED = "cp1251"
_ENC_UTF8 = ("utf-8", "utf-8-sig")
_BOM_UTF8 = b"\xef\xbb\xbf"
_XML_DECL_ENC = re.compile(rb"<\?xml[^>]*?encoding\s*=\s*[\"']([\w.\-]+)", re.I)
_DECL_SCAN_BYTES = 2048


def _is_valid_utf8(raw):
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _declared_encoding(raw):
    """取 XML 声明里写明的编码名；未声明 / 名字不被 Python 认识时返回 None。"""
    m = _XML_DECL_ENC.search(raw[:_DECL_SCAN_BYTES])
    if not m:
        return None
    name = m.group(1).decode("ascii", "ignore").strip().lower()
    try:
        codecs.lookup(name)
    except (LookupError, ValueError):
        return None
    return name


def encoding_chain(raw):
    """给出该字节流的解码候选次序（首个能严格解码成功者胜出）。

    次序规则，按优先级：
      1. UTF-8 BOM          → utf-8-sig（唯一的权威信号）
      2. 字节流是合法 UTF-8 → utf-8
      3. XML 声明写明的编码 → 提到最前（声明与实际不符时严格解码会失败，
         自动落到下一档，不会误判）
      4. 其余按 cp1251 → cp1252（STALKER 俄版为主流），末位固定 _ENC_FORCED

    返回元组；末位是"尽力而为"档，供解析器的 errors="ignore" 兜底使用。

    已知限制：无 BOM、无声明、又非 UTF-8 的文件，若同时是合法 cp1251 与
    合法 cp1252 字节流（极常见），此处按 STALKER 主流取 cp1251；西文 ww
    版这类文件请把「源语言」选为 eng 或在文件头补 XML 声明。
    """
    if raw.startswith(_BOM_UTF8):
        cands = ["utf-8-sig"]
    elif _is_valid_utf8(raw):
        cands = ["utf-8"]
    else:
        cands = []
        decl = _declared_encoding(raw)
        if decl and decl not in _ENC_UTF8:
            cands.append(decl)
        cands.extend(n for n in _ENC_FALLBACK if n not in cands)
    if not cands or cands[-1] != _ENC_FORCED:
        cands.append(_ENC_FORCED)
    return tuple(cands)


def read_encoding_chain(path, max_bytes=64 * 1024 * 1024):
    """读取文件并给出其解码候选次序；读不了时返回兜底次序。
    仅供解析前判定编码用，不改变调用方自己的读取逻辑。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            raw = f.read(max_bytes if size > max_bytes else size)
    except OSError:
        return (_ENC_FORCED,)
    if size > max_bytes:
        while raw and (raw[-1] & 0xC0) == 0x80:
            raw = raw[:-1]
        if raw and raw[-1] >= 0x80:
            raw = raw[:-1]
    return encoding_chain(raw)


def read_text_any(path):
    """按 UTF-8（含 BOM）→ cp1251 顺序读取脚本/XML 原文，读不出时替换。"""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _files_equal(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x = fa.read(1024 * 1024)
                y = fb.read(1024 * 1024)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


_MOJIBAKE_CHARS = "ÐÑÃÂâ€™Ð„ÑŽ"


def looks_like_mojibake(text):
    """粗判"UTF-8 被单字节码表解出来的乱码"，只用于告警、不自动改判。

    正常俄文/中文文本里不会成片出现 Ð/Ñ/Ã/Â；而 UTF-8 的俄文（Д→D0 94）
    或中文被 cp1251 解码后，特征就是成片的拉丁扩充字符。
    """
    if not text:
        return False
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return False
    bad = sum(1 for c in letters if c in _MOJIBAKE_CHARS)
    return bad / len(letters) > 0.3


_VENDOR_IO_LOCK = threading.Lock()


class capture_stdout(object):
    """把 vendor 解析器的 print() 诊断收进插件日志。

    vendor 包（wzyddg fork）用 print 报告"某文件用哪个编码解码"这类诊断，
    而冻结后的 exe 是 GUI 子系统（PE Subsystem=2），stdout 为 None —— 这些
    诊断在用户侧完全不可见，编码猜错时没有任何线索。这里把它收进
    api.log_detail（只落盘），让"到底用了哪个编码"事后可复查。

    stdout 是进程级全局对象，故重定向期间加锁串行；解析只在作业线程发生，
    且按钮互斥保证同时只有一个作业，冲突面很小。拿不到锁则退化为不重定向
    （宁可少一行日志，也不阻塞翻译主流程）。
    """

    def __init__(self):
        self._buf = io.StringIO()
        self._old = None
        self._held = False

    def __enter__(self):
        self._held = _VENDOR_IO_LOCK.acquire(timeout=30)
        if self._held:
            self._old = sys.stdout
            sys.stdout = self._buf
        return self

    def __exit__(self, *exc_info):
        if self._held:
            sys.stdout = self._old
            _VENDOR_IO_LOCK.release()
        return False

    def lines(self):
        return [ln.strip() for ln in self._buf.getvalue().splitlines()
                if ln.strip()]



DEFAULT_CONFIG = {
    "model_paths": {},
    "model_dir": _MODELS_DIR,
    "exe_paths": {},
    "custom_model_path": "",
    "custom_ctx": 8192,
    "parallel": 1,
    "tier": "7b",
    "variant": "cpu",
    "in_dir": "",
    "out_dir": "",
    "reuse_dir": "",
    "glossary": "",
    "force_glossary": True,
    "glossary_force": True,
    "protect_proper_v2": True,
    "src_lang": "auto",
    "mode": "auto",
    "session_glossary": True,
    "tm_lite": True,
    "output_anyway": False,
    "prompt_extra": "",
    # ★ 2.0.3：官方模板措辞 A/B（术语块「X 翻译成 Y」+ 指令对齐官方分布）
    "official_prompts": False,
    # ★ 2.0.3：全文高频专名定译（软提示收敛译名漂移）
    "freq_names": True,
    "artifact_hashes": {},
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(_CONFIG, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if cfg.get("tier") in TIER_ALIAS:
        cfg["tier"] = TIER_ALIAS[cfg["tier"]]
    if cfg.get("tier") not in TIER_PROFILES:
        cfg["tier"] = "7b"
    mp = cfg.get("model_paths")
    if isinstance(mp, dict) and "1b8" in mp and "mt2" not in mp:
        mp["mt2"] = mp.pop("1b8")
    for k in ("in_dir", "out_dir", "reuse_dir", "glossary"):
        v = cfg.get(k)
        if isinstance(v, str) and v and not os.path.exists(v):
            cfg[k] = ""
    md = cfg.get("model_dir")
    if not (isinstance(md, str) and md and os.path.isdir(md)):
        cfg["model_dir"] = _MODELS_DIR
    if isinstance(mp, dict):
        cfg["model_paths"] = {
            k: v for k, v in mp.items()
            if isinstance(v, str) and v and os.path.isfile(v)}
    ep = cfg.get("exe_paths")
    if isinstance(ep, dict):
        cfg["exe_paths"] = {
            k: v for k, v in ep.items()
            if isinstance(v, str) and v and os.path.isfile(v)}
    cp = cfg.get("custom_model_path")
    if cp and (not isinstance(cp, str) or not os.path.isfile(cp)):
        cfg["custom_model_path"] = ""
    try:
        par = int(cfg.get("parallel", 1))
    except (TypeError, ValueError):
        par = 1
    # ★ 2.0.2：路数选项已动态化（1..PARALLEL_CAP），校验放宽为整数区间；
    #   旧配置的 1/2/4 原样兼容，越界/垃圾值回落 1。
    cfg["parallel"] = par if 1 <= par <= PARALLEL_CAP else 1
    return cfg


_JSON_WRITE_LOCK = threading.Lock()


_LAST_CONFIG_ERROR = ""


def save_config(cfg):
    """原子写配置（tmp → os.replace），并发调用串行化。返回是否写成功。

    写失败**不再静默**：调用方据此提示用户（只读目录/磁盘满时，设置只在内存
    生效）。并发修改 dict 可能让 json.dumps 抛 RuntimeError，同样要捕获。
    """
    global _LAST_CONFIG_ERROR
    try:
        payload = json.dumps(cfg, ensure_ascii=False, indent=1)
    except (TypeError, ValueError, RuntimeError) as e:
        _LAST_CONFIG_ERROR = "%s: %s" % (type(e).__name__, e)
        return False
    with _JSON_WRITE_LOCK:
        try:
            tmp = _CONFIG + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, _CONFIG)
            _LAST_CONFIG_ERROR = ""
            return True
        except Exception as e:
            _LAST_CONFIG_ERROR = "%s: %s" % (type(e).__name__, e)
            return False



def load_builtin_glossary():
    """随包术语（只读）。优先 glossary_builtin.json，兼容早期文件名。"""
    for path in (_GLOSS_BUILTIN, _GLOSS_BUILTIN_LEGACY):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return flatten_glossary(json.load(f))
        except Exception:
            continue
    return {}


def _read_user_layer(builtin=None):
    """读用户覆盖层，返回 (set_map, deleted, clear_builtin)。

    兼容 早期的扁平格式：那份文件其实是"整张术语库的快照"，若直接当
    覆盖层会把 builtin 的后续更新全部遮蔽。这里按**与 builtin 的差异**提取，
    只保留用户真正改过/新增的条目。
    """
    try:
        with open(_GLOSS_USER, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}, set(), False
    if isinstance(data, dict) and data.get("_format") == _GLOSS_LAYER_FORMAT:
        raw_set = data.get("set") if isinstance(data.get("set"), dict) else {}
        set_map = {str(k): str(v) for k, v in raw_set.items()
                   if str(k).strip() and not str(k).startswith("_")}
        try:
            deleted = {str(k) for k in (data.get("del") or [])
                       if str(k).strip()}
        except TypeError:
            deleted = set()
        return set_map, deleted, bool(data.get("clear_builtin"))
    flat = flatten_glossary(data) if isinstance(data, dict) else {}
    base = load_builtin_glossary() if builtin is None else builtin
    return {k: v for k, v in flat.items() if base.get(k) != v}, set(), False


def load_glossary_layers():
    """返回 (合并后的术语表, 用户 set 层, 用户删除集, 是否清空 builtin)。"""
    builtin = load_builtin_glossary()
    set_map, deleted, clear = _read_user_layer(builtin)
    merged = {} if clear else dict(builtin)
    for key in deleted:
        merged.pop(key, None)
    merged.update(set_map)
    return merged, set_map, deleted, clear


# 只把真正跨语境稳定的专名当硬词。界面与提示（Здоровье→生命值）、
# 任务、对话/百科类会随语境变化，一律只做软参考或不注入，避免
# “Здоровье не шалит?”被硬塞成“生命值”这类语境错误。
_HARD_TERM_CATEGORIES = (
    # 人物/军衔不再全局硬替换：不同 MOD 的同名人物可能不是同一人。
    # 人物名由“当前 MOD 会话词表 + 人物表精确命中”处理，见 _build_session_glossary。
    "阵营与组织", "地点与区域", "变异生物", "物品·武器·装备·神器")


_TERMBASE_PATH = os.path.join(_ASSETS, "stalker_termbase.json")
_TERMBASE_CACHE = {"key": None, "data": {}}
_DEFAULT_LAYERS = {
    "canon": list(_HARD_TERM_CATEGORIES),
    "names": ["人物与军衔"],
    "technical": ["技术缩写"],
    # ★ 2.0.22：Gunslinger/GAMMA 两个软参考层已整体退役（抽样语义自判
    # 明确错率约 31%），数据留在 corpus/soft_reference_retired.json。
    "reference": ["ANTHOLOGY 参考"],
}
_RISKY_LATIN_RE = re.compile(r"^[a-z][a-z0-9._\-/&()]{1,18}$")


def load_termbase_data():
    """读随包分类术语库（带 mtime 缓存，避免每次命中都重读 350 KB）。"""
    try:
        st = os.stat(_TERMBASE_PATH)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _TERMBASE_CACHE.get("key") == key:
        return _TERMBASE_CACHE.get("data") or {}
    try:
        with open(_TERMBASE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    _TERMBASE_CACHE["key"] = key
    _TERMBASE_CACHE["data"] = data
    return data


def _layer_categories(meta, name):
    layers = meta.get("layers") if isinstance(meta, dict) else None
    cats = layers.get(name) if isinstance(layers, dict) else None
    if isinstance(cats, str):
        cats = [cats]
    if not isinstance(cats, list):
        cats = _DEFAULT_LAYERS.get(name, [])
    return [str(c) for c in cats if str(c).strip()]


def load_term_layers():
    """返回 v2 四层索引：canon/names/technical/reference + 变体/大小写策略。

    早期术语库没有 `_meta.layers` 时按原分类回退，导入别人的分类词库也能用。
    """
    data = load_termbase_data()
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    canon_cats = _layer_categories(meta, "canon")
    name_cats = _layer_categories(meta, "names")
    tech_cats = _layer_categories(meta, "technical")
    ref_cats = _layer_categories(meta, "reference")

    def collect(cats):
        keys, table = set(), {}
        for cat in cats:
            t = data.get(cat)
            if not isinstance(t, dict):
                continue
            for k, v in t.items():
                name = str(k).strip()
                if not name or name.startswith("_"):
                    continue
                keys.add(name)
                table[name] = str(v)
        return keys, table

    canon, _ = collect(canon_cats)
    names, _ = collect(name_cats)
    _, technical = collect(tech_cats)
    _, reference = collect(ref_cats)
    variants = meta.get("variants") if isinstance(meta.get("variants"),
                                                 dict) else {}
    variants = {str(k): str(v) for k, v in variants.items()
                if str(k).strip() and str(v).strip()}
    explicit = meta.get("case_sensitive")
    case_sensitive = ({str(k) for k in explicit if str(k).strip()}
                      if isinstance(explicit, list) else set())
    # 人物名与内容型专名按“首字母大小写一致”匹配：英文正文里的
    # doctor/bar/crystal 这类普通词不会被当成专名。
    case_sensitive |= names
    case_sensitive |= {k for k in technical if re.search(r"[A-Za-z]", k)}
    case_sensitive |= {k for k in canon if _RISKY_LATIN_RE.match(k)}
    quote_required = meta.get("quote_required")
    quote_required = ({str(k) for k in quote_required if str(k).strip()}
                      if isinstance(quote_required, list) else set())
    return {"canon": canon, "names": names, "technical": technical,
            "reference": reference, "variants": variants,
            "case_sensitive": case_sensitive,
            "quote_required": quote_required}


_BUILTIN_FLAT_CACHE = {"key": None, "data": {}}
_TB_LOW_INDEX = {"key": None, "low": {}}   # termbase casefold 归一索引（审计 F 层优化）
_PARTIAL_GLOSS_CACHE = {"key": None, "words": []}   # 硬层专名词映射（保护条目子串替换）


def termbase_lookup(text):
    """精确查随包术语库（canon/names/technical/reference 全层），返回译名或 None。

    ★ 2.0.7：专名保护跳过之前先查这里——库里已有的译名（如 БАРМЕН→酒保、
    ПРИЗРАК→幽灵）直接取用，不再当"不可译专名"原样保留。大小写与首尾
    引号不敏感（SMS 名字表多为全大写，词库多为首字母大写）。
    只做整串精确匹配，不做子串——避免把正文里的普通词误换成专名译名。
    """
    raw = (text or "").strip()
    s = raw.strip('"\u00ab\u00bb\u201c\u201d\'')
    if not raw:
        return None
    # ★ 2.0.22：首尾引号不敏感必须是**双向**的——旧实现只把输入剥引号后查，
    # 于是键本身带引号的条目永远查不到（实测 50 条不可达，含
    # `Учёный «О-Сознания»`→`“共同意识”科学家`、`"Tears of Electra"`→
    # `“电之泪”`、`"Monolith" Chief`→`黑石首领`；Anomaly 两个「共同意识」
    # 人物名因此白白送模型）。原串优先、剥引号兜底，两种形态都试。
    cands = []
    for _form in (raw, s):
        if not _form:
            continue
        for _c in (_form, _form.casefold()):
            if _c not in cands:
                cands.append(_c)
    # 直接读 termbase 原始分类表：load_term_layers 的 canon/names 是 set
    # （只有 technical/reference 带映射），这里需要"原文→译名"全量映射。
    # ★ 全量审计 2026-11-23：3.6 万条后逐分类 casefold 全扫 O(N) 达 3.6ms/次，
    #   终检几千条目累计数秒——改为模块级 casefold 归一索引（O(1)），
    #   首分类优先（canon/人物 在前，与旧遍历顺序一致）。
    data = load_termbase_data()
    global _TB_LOW_INDEX
    try:
        _st = os.stat(_TERMBASE_PATH)
        _tkey = (_st.st_mtime_ns, _st.st_size)
    except OSError:
        _tkey = None
    if _TB_LOW_INDEX.get("key") != _tkey:
        idx = {}
        for _cat, table in data.items():
            if _cat.startswith("_") or not isinstance(table, dict):
                continue
            for k, v in table.items():
                kl = str(k).casefold()
                if kl not in idx:          # 首分类优先
                    idx[kl] = str(v)
        _TB_LOW_INDEX = {"key": _tkey, "low": idx}
    idx = _TB_LOW_INDEX.get("low") or {}
    for _c in cands:
        if _c in idx:
            return idx[_c]
    # 兼容早期扁平词库（带 mtime 缓存，避免逐条重读 320KB）
    global _BUILTIN_FLAT_CACHE
    try:
        _bst = os.stat(_GLOSS_BUILTIN)
        _bkey = (_bst.st_mtime_ns, _bst.st_size)
    except OSError:
        _bkey = None
    if _BUILTIN_FLAT_CACHE.get("key") != _bkey:
        bflat = load_builtin_glossary() or {}
        _BUILTIN_FLAT_CACHE = {"key": _bkey, "data": bflat,
                               "low": {str(k).casefold(): str(v)
                                       for k, v in bflat.items()}}
    bcache = _BUILTIN_FLAT_CACHE.get("data") or {}
    blow = _BUILTIN_FLAT_CACHE.get("low") or {}
    for _c in cands:
        if _c in bcache:
            return str(bcache[_c])
        if _c in blow:
            return blow[_c]
    return None


_PARTIAL_MODEL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z])(?:[A-Z]+[A-Za-z]*[-/]\d[\w\-/]*"
    r"|\d[\w\-/]*[A-Z]+[A-Za-z]*|[A-Z]{2,})(?![a-z])")


def partial_glossary_translation(src, rel=None):
    """专名保护命中但整串无词库键时，对串内硬层专名词做子串替换。

    ★ 2.0.21（IWP 产物审计）：`Vasya "Boar"`（人物名+引号绰号）整串
    不在词库 → 走专名保护原样保留英文，但 "Boar" 在 canon 有权威译名
    "野猪"。约束（保守）：
      * 只替换 canon/names 硬层词（reference/technical 不参与）；
      * 词长 ≥3 且为 Title Case/全大写（专名形态，避免 doctor/case 类
        普通词误换）；
      * 整串 ≤60 字符（人物/物品短名条目），长句正文不适用；
      * 精确词边界替换，一个词都不命中返回 None（调用方继续保护跳过）。
    ★ 2.0.22（每 MOD 随机文件测试 + 人工参考对照）：全库 133 条写出里
    112 条是半中半英破碎文本——硬层收录了与普通英文词同形的条目
    （Vice→台钳、Eye→眼睛、Lock→锁、Engine→引擎、Sergeant→中士），
    子串替换只命中其中一部分就把残句写出去：
      `Executive Vice President` → `Executive 台钳 President`
      `Senior Sergeant "Mangun" Potryko` → `Senior 中士 "曼古恩" Potryko`
      `Database Applications Engineer` → `Database Applications 工程师`
    人工汉化对同样条目是全译（执行副总裁 / 数据库应用工程师）。补两条约束：
      * 制作名单/音乐表整文件不参与（与"连核心术语也不替换"同口径）；
      * 替换后仍残留普通英文词（含小写、非型号/全大写缩写形态）→
        返回 None，回落原样保留，宁可不翻也不写破碎文本。
    """
    s = (src or "").strip()
    if not s or len(s) > 60:
        return None
    if rel is not None and looks_like_hard_protected_file(rel):
        return None
    global _PARTIAL_GLOSS_CACHE
    data = load_termbase_data()
    meta_layers = (data.get("_meta") or {}).get("layers") or {}
    try:
        _st = os.stat(_TERMBASE_PATH)
        _k = (_st.st_mtime_ns, _st.st_size)
    except OSError:
        _k = None
    if _PARTIAL_GLOSS_CACHE.get("key") != _k:
        # ★ 注意：load_term_layers 的 canon/names set 装的是**词条键**，
        # 不是分类名——硬层分类名要从 _meta.layers 取。
        layers = set(meta_layers.get("canon") or []) | \
            set(meta_layers.get("names") or [])
        words = []
        for cat, tbl in data.items():
            if cat.startswith("_") or not isinstance(tbl, dict) \
                    or cat not in layers:
                continue
            for k, v in tbl.items():
                k = str(k)
                if len(k) < 3 or not re.search(r"[A-Za-z]", k):
                    continue
                if not (k[:1].isupper() or k.isupper()):
                    continue
                words.append((k, str(v)))
        _PARTIAL_GLOSS_CACHE = {"key": _k, "words": words}
    out = s
    changed = False
    for k, v in _PARTIAL_GLOSS_CACHE.get("words") or []:
        if k in out:
            out2 = re.sub(rf"(?<!\w){re.escape(k)}(?!\w)", v, out)
            if out2 != out:
                out = out2
                changed = True
    if not changed:
        return None
    # ★ 2.0.22：残留普通英文词 = 破碎产物，回落原样保留。型号/全大写
    # 缩写（PSO-1、AKMS、SIG、OTs-14-4A）是合法残留，先剥掉再判。
    for w in re.findall(r"[A-Za-z]{3,}", _PARTIAL_MODEL_TOKEN_RE.sub(" ", out)):
        if any(c.islower() for c in w):
            return None
    return out


def termbase_exact_translation(text):
    """整条命中术语库时返回可直接落盘的译名（含首尾引号处理），否则 None。

    源文在引号内时去掉词库译名的外层引号，避免 ““自由团”” 双引号嵌套；
    与 _inline_term_value 同口径。只处理整条精确匹配。
    """
    value = termbase_lookup(text)
    if value is None:
        return None
    s = (text or "").strip()
    quotes = "\"'«»“”‘’"
    if (len(s) >= 2 and s[0] in quotes and s[-1] in quotes
            and len(value) >= 2 and value[0] in quotes
            and value[-1] in quotes):
        value = value[1:-1]
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return lead + value + trail


def load_hard_glossary_keys():
    """只把 canon 层（阵营/地点/变异体/物品）当硬词；人物名与对话类只做软参考。

    builtin 术语表是扁平表，混入了大量新闻/对话里的普通词和屈折形
    （例如 Информацию→信息、Искренне→火花）。这些词强制替换会把
    句子改坏；按 v2 的 canon 层筛出硬词，普通词不再注入/强制。
    """
    return set(load_term_layers()["canon"])


def load_technical_exact():
    """数据化技术精确表；随包术语库优先，旧包回退到内置常量。"""
    out = dict(_TECHNICAL_EXACT)
    out.update(load_term_layers()["technical"])
    return out


def save_user_glossary(set_map, deleted=None, clear_builtin=False):
    """只写用户覆盖层；builtin 永远是随包只读文件。"""
    payload = json.dumps({
        "_format": _GLOSS_LAYER_FORMAT,
        "clear_builtin": bool(clear_builtin),
        "set": dict(set_map or {}),
        "del": sorted(deleted or ()),
    }, ensure_ascii=False, indent=1, sort_keys=True)
    with _JSON_WRITE_LOCK:
        tmp = _GLOSS_USER + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, _GLOSS_USER)


def flatten_glossary(data):
    """把术语表归一成 {原文: 译文}。

    兼容三种文件形态并允许**混合**：
    1) 扁平 {"原文": "译文"}；
    2) 分类名词库 {"分类名": {"原文": "译文"}, ...}（可多层嵌套）；
    3) 上面两种混在一张表里（如 {"a": "b", "武器": {...}}）。

    以 "_" 开头的段（如 _meta）整段跳过。

    注意：旧实现只要表里出现**任何**一个 dict 值，就把整张表当分类表解析，
    于是同一张表里的扁平条目被静默丢弃 —— 实测
    {"普通原文":"普通译文","分类A":{"原文1":"译文1"}} 只剩 {"原文1":...}，
    导入后无提示少条目。这里改成逐键判定，两侧都保留。
    """
    out = {}

    def walk(table, depth=0):
        for key, value in table.items():
            name = str(key).strip()
            if not name or name.startswith("_"):
                continue
            if isinstance(value, dict) and depth < 8:
                walk(value, depth + 1)
            elif not isinstance(value, (dict, list, tuple)):
                out[name] = str(value)

    if isinstance(data, dict):
        walk(data)
    return out



_VENDOR_READY = False


def _load_vendor():
    global _VENDOR_READY
    import types
    if _VENDOR_READY:
        return sys.modules["xRayXmlParser"]
    if not os.path.isfile(_VENDOR_ZIP):
        raise FileNotFoundError("缺少解析器包：%s" % _VENDOR_ZIP)

    pkg_name = "xRayXmlParser"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []
    pkg.__package__ = pkg_name
    sys.modules[pkg_name] = pkg

    order = ["entityDefinition", "xRayTextAnalyzer",
             "xmlFileGenerator", "xmlxReader"]
    with zipfile.ZipFile(_VENDOR_ZIP) as z:
        sources = {n: z.read("%s/%s.py" % (pkg_name, n)).decode("utf-8")
                   for n in order}
        init_src = z.read("%s/__init__.py" % pkg_name).decode("utf-8")

    for name in order:
        mod = types.ModuleType("%s.%s" % (pkg_name, name))
        mod.__package__ = pkg_name
        sys.modules["%s.%s" % (pkg_name, name)] = mod
        code = compile(sources[name], "%s!%s.py" % (_VENDOR_ZIP, name), "exec")
        exec(code, mod.__dict__)
        setattr(pkg, name, mod)

    code = compile(init_src, "%s!__init__.py" % _VENDOR_ZIP, "exec")
    exec(code, pkg.__dict__)
    _VENDOR_READY = True
    return pkg



class ProbeResult(object):
    def __init__(self, name, url):
        self.name = name
        self.url = url
        self.ok = False
        self.speed = 0.0
        self.got = 0
        self.secs = 0.0
        self.range_ok = False
        self.note = ""
        self.kind = ""


def _host_kind(url):
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("huggingface.co", "hf.co", "github.com"):
        return "直连"
    return "镜像"


def probe_source(name, url, max_bytes=2 * 1024 * 1024, max_secs=8.0):
    """对单个源做 Range 小样本测速。失败不抛异常，全部塞进 ProbeResult。"""
    res = ProbeResult(name, url)
    res.kind = _host_kind(url)
    t0 = time.time()
    try:
        # 流式下载探测：必须绕过宿主小请求门面（宿主会整包读进内存）
        with _http_request("GET", url,
                           headers={"Range": "bytes=0-%d" % (max_bytes - 1)},
                           timeout=8) as resp:
            res.range_ok = (resp.status == 206)
            ctype = resp.headers.get("Content-Type", "")
            if "html" in ctype.lower():
                res.note = "返回网页而非文件"
                return res
            while res.got < max_bytes and (time.time() - t0) < max_secs:
                chunk = resp.read(65536)
                if not chunk:
                    break
                res.got += len(chunk)
        res.secs = max(time.time() - t0, 0.001)
        res.speed = (res.got / 1048576.0) / res.secs
        res.ok = res.got > 256 * 1024
        if not res.ok:
            res.note = "样本过小（%s）" % _fmt_size(res.got)
    except _HttpError as e:
        res.note = "HTTP %s" % e.code
    except (socket.timeout, TimeoutError, OSError) as e:
        res.note = "连接失败：%s" % e
    except Exception as e:
        res.note = "%s: %s" % (type(e).__name__, e)
    finally:
        if res.secs == 0.0:
            res.secs = max(time.time() - t0, 0.001)
    return res


def run_probe(sources, log=None, max_workers=12):
    """sources: [(name, url)]；并发测速（上限 max_workers，
    源很多时避免一次性打开几十条连接互相抢带宽），按速度降序返回。"""
    if log:
        log("正在并发实测 %d 个下载源（每源取样 2 MB / 8 秒，最多 %d 并发）…"
            % (len(sources), max_workers))
    results = [None] * len(sources)
    sem = threading.Semaphore(max_workers)

    def worker(i, name, url):
        with sem:
            results[i] = probe_source(name, url)

    threads = []
    for i, (name, url) in enumerate(sources):
        t = threading.Thread(target=worker, args=(i, name, url), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    ok = [r for r in results if r and r.ok]
    bad = [r for r in results if r and not r.ok]
    ok.sort(key=lambda r: r.speed, reverse=True)
    if log:
        for r in ok:
            tag = "直连" if r.kind == "直连" else "镜像"
            log("  ✓ [%s] %s：%.2f MB/s（%s）" % (tag, r.name, r.speed,
                "支持断点" if r.range_ok else "不支持 Range"))
        for r in bad:
            log("  ✗ %s：%s" % (r.name, r.note))
    return ok + bad


def detect_environment(results):
    """根据测速结果归纳网络环境。"""
    official_ok = any(r.ok and r.kind == "直连" for r in results)
    mirror_ok = any(r.ok and r.kind == "镜像" for r in results)
    if official_ok and not mirror_ok:
        return "直连网络：官方源可达，已按实测速度选源"
    if official_ok and mirror_ok:
        return "混合网络：官方源与镜像均可达，已自动选择最快源"
    if mirror_ok:
        return "受限网络：官方源不可达，已自动切换国内镜像"
    return "所有下载源均不可达，请检查网络或代理设置"



class DownloadCancelled(Exception):
    pass


class OutputTruncated(Exception):
    """模型输出撞到 max_tokens 被截断。

    单独成类而不是并进 IOError：重试同一个请求没有意义（同样的输入、同样的
    上限，结果只会再截一次），必须由上层把源文切得更细再译，否则半句译文会
    被静默写进汉化包。
    """
    pass


def _host_http_download(url, target, total=None):
    """B250+ 宿主 `api.host.http_download`（失败不留半文件，受联网开关管）。

    BETA.250（commit 80becbc）真机实测：返回
    `{ok, status, bytes, truncated, error, path}`；404/超限/网络失败都**不会
    在目标路径留半文件**；302 重定向自动跟随。两个能力缺口决定了不能全量
    替换自建流式：
      * `timeout_sec` 是**整段下载的时间窗**（慢速流在窗口内没下完即整单
        失败，实测 14.4 秒的涓流在 timeout=5 下约 8 秒报错）；
      * 没有进度回调、不支持 Range 断点。
    所以只用于「无断点续传、体积可预估且 <= _HOST_DL_MAX」的中小产物；
    GB 级模型（要进度 + 断点 + 长时长）仍走自建流式——能力缺口非二次实现。
    老宿主无此门面 → 返回 None 交回自建路径。
    """
    host = _HOST
    fn = getattr(host, "http_download", None) if host is not None else None
    if fn is None:
        return None
    # 0.5 MB/s 保底换算时间窗（实测镜像 0.78 MB/s）；size cap 比预估多留 4MB。
    timeout = 300 if not total else max(300, int(total / 512000.0) + 60)
    max_bytes = (int(total) + (4 << 20)) if total else (8 << 30)
    try:
        return fn(url, target, timeout_sec=int(timeout),
                  max_bytes=int(max_bytes))
    except Exception:
        return None     # 门面抛异常 → 回退自建路径（老行为）


def download_one(sources, target, log, progress=None, cancel_event=None,
                 retries_per_source=3, verify=None, total_hint=None):
    """按 sources 顺序尝试；.part 断点续传；全部源失败才抛异常。

    progress(done, total, speed_mbps, eta_sec, src_name)
    verify(part_path) -> digest：**每个源下载完成后**立即校验，失败即删文件并
    换下一个源（文档一直这么写，早期实际只是删文件让用户手动重试）。
    返回校验得到的 digest；未提供 verify 时返回 True。

    total_hint：产物预估字节数（调用方从 task 的 size_bytes / size_hint 换算）。
    B250+ 且无断点时，中小产物（<= _HOST_DL_MAX）优先走宿主 http_download
    （跟随宿主）；失败或老宿主回退自建流式。
    """
    part = target + ".part"
    total = None
    last_err = None
    last_invalid = None

    for name, url in sources:
        for attempt in range(1, retries_per_source + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled()
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            try:
                # B250+ 宿主下载（每源只试首轮、无断点、中小产物）；失败回退流式
                if (attempt == 1 and have == 0 and total_hint
                        and total_hint <= _HOST_DL_MAX):
                    hr = _host_http_download(url, target, total_hint)
                    if hr is not None:
                        if cancel_event is not None and cancel_event.is_set():
                            try:
                                os.remove(target)
                            except OSError:
                                pass
                            raise DownloadCancelled()
                        if hr.get("ok"):
                            log("  宿主下载完成：%s"
                                % _fmt_size(int(hr.get("bytes") or 0)))
                            if progress:
                                progress(int(hr.get("bytes") or 0),
                                         int(total_hint), 0, 0, name)
                            digest = verify(target) if verify is not None \
                                else True
                            return digest
                        log("  宿主下载未成功（HTTP %s %s），回退自建下载"
                            % (hr.get("status") or 0,
                               hr.get("error") or "…"))
                headers = {}
                if have:
                    headers["Range"] = "bytes=%d-" % have
                # 流式下载：绕过宿主小请求门面（断点续传/进度/大文件需要流）
                resp = _http_request("GET", url, headers=headers, timeout=30)
                with resp:
                    ctype = resp.headers.get("Content-Type", "")
                    if "html" in ctype.lower():
                        raise IOError("%s 返回了网页而非文件" % name)
                    if have and resp.status == 206:
                        cr = resp.headers.get("Content-Range", "")
                        if "/" in cr:
                            total = int(cr.rsplit("/", 1)[1])
                        mode = "ab"
                    else:
                        if have:
                            log("  %s 不支持断点，重新开始下载" % name)
                        total = int(resp.headers.get("Content-Length", 0)) or None
                        have = 0
                        mode = "wb"
                    done = have
                    t0 = time.time()
                    last_ui = 0.0
                    with open(part, mode) as f:
                        while True:
                            if cancel_event is not None and cancel_event.is_set():
                                raise DownloadCancelled()
                            chunk = resp.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            now = time.time()
                            if progress and (now - last_ui) > 0.4:
                                speed = (done - have) / 1048576.0 / max(now - t0, 0.001)
                                eta = ((total - done) / 1048576.0 / speed
                                       if total and speed > 0 else None)
                                progress(done, total, speed, eta, name)
                                last_ui = now
                    if progress:
                        progress(done, total, 0, 0, name)
                final_size = os.path.getsize(part)
                if total and final_size != total:
                    raise IOError("下载不完整：%d / %d 字节" % (final_size, total))
                digest = None
                if verify is not None:
                    digest = verify(part)
                os.replace(part, target)
                return digest if verify is not None else True
            except DownloadCancelled:
                raise
            except InvalidArtifact as e:
                # verify 内部已删除坏文件；本源的断点不再有意义，换下一个源
                last_invalid = "%s（第 %d 次）：校验未通过：%s" % (
                    name, attempt, e)
                log("  下载产物校验未通过（%s），已删除，切换下一个源…" % e)
                break
            except Exception as e:
                last_err = "%s（第 %d 次）：%s" % (name, attempt, e)
                log("  下载中断，%s" % last_err)
                if isinstance(e, _HttpError) and 400 <= e.code < 500:
                    log("  该源返回 HTTP %s，不再重试，直接换源" % e.code)
                    break
                if attempt < retries_per_source:
                    time.sleep(min(2 * attempt, 5))
        log("  切换下一个下载源（已保留断点 %s）…" % _fmt_size(
            os.path.getsize(part) if os.path.isfile(part) else 0))
    if last_invalid:
        raise InvalidArtifact("所有下载源均未通过校验；最后错误：%s"
                              % last_invalid)
    raise IOError("所有下载源均失败；最后错误：%s" % last_err)


def _extract_zip_into(zip_path, dest_dir):
    """解压到临时目录，再逐项移入 dest_dir（同名项先删）。

    为什么不在 dest_dir 上直接 extractall：中断会留下"半套文件"，而按文件名
    前缀做的存在性检查会把它当成已安装（CUDA 运行时就是这么坏掉的）。
    """
    os.makedirs(dest_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ai_translate_zip_", dir=dest_dir)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        for item in sorted(os.listdir(tmp)):
            src = os.path.join(tmp, item)
            dst = os.path.join(dest_dir, item)
            if os.path.exists(dst):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                else:
                    os.remove(dst)
            shutil.move(src, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def extract_zip(zip_path, dest_dir, marker, log):
    """解压运行库 zip，返回 marker 文件（llama-server.exe）的完整路径。"""
    if os.path.isdir(dest_dir):
        for root, _dirs, files in os.walk(dest_dir):
            if marker in files:
                return os.path.join(root, marker)
    _extract_zip_into(zip_path, dest_dir)
    marker_path = None
    for root, _dirs, files in os.walk(dest_dir):
        if marker in files:
            marker_path = os.path.join(root, marker)
            break
    if not marker_path:
        raise IOError("压缩包中找不到 %s" % marker)
    log("  解压完成：%s" % marker_path)
    return marker_path



class InvalidArtifact(Exception):
    """下载产物校验未通过（不是预期文件，或与上次记录的内容不一致）。"""
    pass


_GGUF_TYPES = {0: ("B", 1), 1: ("b", 1), 2: ("H", 2), 3: ("h", 2),
               4: ("I", 4), 5: ("i", 4), 6: ("f", 4), 7: ("?", 1),
               10: ("Q", 8), 11: ("q", 8), 12: ("d", 8)}


def read_gguf_meta(path, want=("tokenizer.chat_template",
                               "tokenizer.ggml.eos_token_id",
                               "general.name", "general.architecture"),
                   max_elements=4000000, head_bytes=96 * 1024 * 1024):
    """读 GGUF 头部 KV 元数据（只读头部，不加载张量），返回 dict。

    为什么要读它：某些 GGUF 的 tokenizer 元数据不完整 —— 实测
    `hy-mt2-1.8b-q8_0.gguf` 既没有 `tokenizer.chat_template`，EOS 又是 3
    这个不在 EOG 集合里的值，llama.cpp 会打
    `special_eos_id is not in special_eog_ids`，于是模型**永远不吐 EOS**：
    一句 "Hello." 也能连生成 1285 个 token（CPU 上 178 秒）直到客户端超时。
    这种模型拿去翻几千条文本，结果就是每条都超时、整批白跑；启动时读一眼
    元数据就能提前告警。

    实现要点（都踩过坑）：
      * KV 必须**从头顺序走**（值是变长的），无法靠偏移量直接跳到某个键；
        而 `tokenizer.chat_template` 通常排在词表数组之后，所以要把头部
        整块读进内存再解析 —— 逐字段读文件在 10 万级词表上会明显变慢；
      * 读到的结果带一个 `"__partial__"` 标记：解析中途失败/缓冲区不够时
        置 True。**调用方必须据此区分"确实没有模板"与"没解析到"** ——
        早先版本预算耗尽后静默返回部分结果，导致有模板的 7B 被误报
        "未内置对话模板"。
    """
    import struct
    out = {}

    def rd_str(f):
        n = struct.unpack("<Q", f.read(8))[0]
        if n > 100 * 1024 * 1024:
            raise ValueError("KV 字符串长度异常")
        return f.read(n).decode("utf-8", "replace")

    budget = [max_elements]

    def rd_val(f, t):
        if t == 8:
            return rd_str(f)
        if t == 9:
            et = struct.unpack("<I", f.read(4))[0]
            n = struct.unpack("<Q", f.read(8))[0]
            if budget[0] - n < 0:
                raise ValueError("KV 数组元素数超出预算")
            budget[0] -= n
            if n > 8:
                for _ in range(n):
                    rd_val(f, et)
                return "<array %d>" % n
            return [rd_val(f, et) for _ in range(n)]
        fmt, size = _GGUF_TYPES[t]
        return struct.unpack("<" + fmt, f.read(size))[0]

    complete = False
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(min(size, head_bytes))
        f = io.BytesIO(head)
        if f.read(4) != b"GGUF":
            return out
        struct.unpack("<I", f.read(4))[0]
        struct.unpack("<Q", f.read(8))[0]
        n_kv = struct.unpack("<Q", f.read(8))[0]
        for _ in range(n_kv):
            key = rd_str(f)
            t = struct.unpack("<I", f.read(4))[0]
            if key in want:
                out[key] = rd_val(f, t)
            else:
                rd_val(f, t)
        complete = True
    except Exception:
        pass
    if not complete:
        out["__partial__"] = True
    return out


def _fmt_mem(mib):
    return ("%.2f GB" % (float(mib) / 1024.0)) if mib >= 1024 \
        else ("%d MB" % int(mib))


def estimate_vram_mib(model, ctx_per_route, parallel=1, gpu_layers=99):
    """按模型结构和路数估算显存（MiB）：权重 + KV + 计算缓冲。

    llama.cpp 的 KV 缓存按 token 计：layers × kv_heads × (key+value) × 2 字节
    （f16）。实测本模型 32 层 / 4 KV 头 / 128+128 ⇒ 64 KiB/token，
    16384 token 正好 1024 MiB，与 server 日志一致。读不到 GGUF 结构时
    退回 64 KiB/token 的经验值，并在结果里标注 approx。
    """
    # 统一用 MiB：权重/KV 都由字节除以 1024²，compute 直接是 MiB。
    out = {"weights": 0, "kv": 0, "compute": 68, "total": 68,
           "approx": True, "ok": False}
    if not model or not os.path.isfile(model):
        return out
    try:
        base = read_gguf_meta(model, want=("general.architecture",))
        arch = str(base.get("general.architecture") or "").strip()
        want = ()
        if arch:
            want = tuple(arch + "." + suffix for suffix in (
                "block_count", "attention.head_count_kv",
                "attention.key_length", "attention.value_length"))
        meta = read_gguf_meta(model, want=want)
        layers = int(meta.get(arch + ".block_count", 0))
        heads = int(meta.get(arch + ".attention.head_count_kv", 0))
        klen = int(meta.get(arch + ".attention.key_length", 0))
        vlen = int(meta.get(arch + ".attention.value_length", 0))
        if layers <= 0 or heads <= 0 or klen <= 0 or vlen <= 0:
            raise ValueError("GGUF 缺少 KV 结构字段")
        per_token = layers * heads * (klen + vlen) * 2
        out["approx"] = False
    except Exception:
        per_token = 64 * 1024
    kv_bytes = int(ctx_per_route) * max(1, int(parallel)) * per_token
    kv = kv_bytes // (1024 * 1024)
    try:
        weights_bytes = os.path.getsize(model) if int(gpu_layers) > 0 else 0
    except OSError:
        weights_bytes = 0
    weights = weights_bytes // (1024 * 1024)
    out.update(weights=weights, kv=kv,
               total=weights + kv + out["compute"],
               ok=True)
    return out


def physical_ram_mb():
    """物理内存 MiB（GlobalMemoryStatusEx，纯 Win API、零依赖）。

    与 detect_nvidia_gpu 同族的本地硬件探测：非 Windows 或取不到一律返回 0，
    调用方据此回落固定档位，**不编造近似值**（与 detect_all_gpus 的口径一致）。
    """
    if sys.platform != "win32":
        return 0

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return 0
        return int(st.ullTotalPhys // (1024 * 1024))
    except Exception:
        return 0


def parallel_max_routes(budget_mib, weights_mib, kv_per_route_mib,
                        compute_mib=68):
    """预算内可开的最多路数（纯函数，动态选项生成与启动钳制共用）。

    口径：llama-server 单进程多槽位，权重只加载一份，每加一路的边际成本
    ≈ 该路的 KV 缓存。取满足 `权重 + 缓冲 + KV×n ≤ 预算` 的最大 n：
    max_n = clamp(floor((预算 − 权重 − 缓冲) / KV每路), 1, PARALLEL_CAP)。
    返回 0 = 没有预算信息（调用方回落固定档）；权重本身就超预算 ⇒ 1
    （单路仍可用，「显存估算」的 ⚠ 会单独提示吃紧）。
    """
    budget = int(budget_mib or 0)
    if budget <= 0:
        return 0
    fixed = int(weights_mib or 0) + int(compute_mib or 0)
    if fixed >= budget:
        return 1
    marginal = max(1, int(kv_per_route_mib or 0))
    return max(1, min(PARALLEL_CAP, (budget - fixed) // marginal))


def parallel_of_label(label):
    """从选项文本还原路数（"3 路（…）" → 3）；不认识返回 0。

    2.0.2 起选项是动态生成的，不能再按固定表前缀匹配 —— 正则取开头数字，
    对任意路数、任意标签顺序都成立（16 路 不会误匹配 1 路）。
    """
    m = re.match(r"^(\d+)\s*路", str(label or "").strip())
    return int(m.group(1)) if m else 0




def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def verify_artifact(path, task, log, known_hashes=None):
    """校验刚下载完的产物，并把它的 sha256 返回（由调用方记进配置）。

    校验顺序与依据：
      * 官方 SHA-256：任务里登记了 sha256 时，必须逐字节一致。运行库哈希来自
        llama.cpp b11612 GitHub Release 的 asset digest，模型哈希来自 HuggingFace
        LFS oid；镜像只负责传输，不能改变校验值。
      * GGUF：magic 必须是 b"GGUF"、版本字段合理、体积与官方字节数一致；
      * 运行库 zip：必须是合法 zip、内部含 llama-server.exe、且那是个 PE(MZ)；
      * 跨次一致性：把 sha256 记进 config；配置里的旧哈希只作提示，官方哈希优先。

    没有登记官方哈希的产物退化为结构校验 + 跨次一致性，并在日志里明确写出
    这一边界。校验失败的文件一律删除并抛 InvalidArtifact。
    """

    def fail(msg):
        try:
            os.remove(path)
        except OSError:
            pass
        raise InvalidArtifact(msg)

    try:
        size = os.path.getsize(path)
    except OSError as e:
        fail("文件读不到：%s" % e)
    if size <= 0:
        fail("文件为空")

    if task.get("kind") == "zip":
        marker = task.get("filename") or ""
        prefix = (task.get("name_prefix") or "").lower()
        problem = None
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = [n.replace("\\", "/") for n in z.namelist()]
                if marker:
                    hits = [n for n in names if n.endswith(marker)]
                else:
                    hits = [n for n in names
                            if os.path.basename(n).lower().startswith(prefix)]
                if not hits:
                    problem = "压缩包内找不到 %s" % (marker or prefix + "*")
                else:
                    with z.open(hits[0]) as fp:
                        head = fp.read(2)
                    if marker and head != b"MZ":
                        problem = "%s 不是可执行文件（PE 头缺失）" % marker
        else:
            problem = "不是有效的 zip 文件"
        if problem:
            fail(problem)
    elif task.get("kind") == "model":
        try:
            with open(path, "rb") as f:
                head = f.read(8)
        except OSError as e:
            fail("文件读不到：%s" % e)
        if head[:4] != b"GGUF":
            fail("不是 GGUF 模型（magic=%r）" % head[:4])
        version = int.from_bytes(head[4:8], "little")
        if version not in (1, 2, 3):
            fail("GGUF 版本号异常：%d" % version)
        official_size = int(task.get("size_bytes") or 0)
        if official_size and size != official_size:
            fail("体积与官方记录不一致（官方 %d B，实际 %d B）"
                 % (official_size, size))
        hint = float(task.get("size_hint") or 0)
        if hint and not (0.5 * hint <= size / float(1 << 30) <= 2.5 * hint):
            log("  注意：%s 体积 %.2f GB 与预期 %.2f GB 差异较大，"
                "请确认下载的是同一版本" % (os.path.basename(path),
                                    size / float(1 << 30), hint), "warn")

    try:
        digest = sha256_file(path)
    except OSError as e:
        fail("计算 sha256 失败：%s" % e)
    key = task.get("filename") or os.path.basename(path)
    prev = (known_hashes or {}).get(key)
    official = (task.get("sha256") or "").strip().lower()
    if official:
        if digest != official:
            fail("官方 SHA-256 不匹配（期望 %s，实际 %s）"
                 % (official, digest))
        if prev and prev != digest:
            log("  配置里的旧哈希与官方不一致，已按官方值更新", "warn")
        log("  官方 SHA-256 校验通过：%s · %s…"
            % (os.path.basename(path), digest[:16]))
    else:
        if prev and prev != digest:
            fail("与上次记录的哈希不一致（上次 %s…，本次 %s…）"
                 % (prev[:12], digest[:12]))
        log("  未登记官方哈希：仅做结构校验与跨次一致性", "warn")
    return digest



class LlamaServer(object):
    def __init__(self, exe, model, variant, log, ctx_size=8192, tier="",
                 gpu_layers=99, parallel=1):
        self.exe = exe
        self.model = model
        self.variant = variant
        self.log = log
        self.ctx_size = int(ctx_size)
        # ★ 2.0.2：路数上限从写死的 4 放宽到 PARALLEL_CAP（动态选项）。
        self.parallel = max(1, min(PARALLEL_CAP, int(parallel)))
        self.tier = tier
        self.gpu_layers = 0 if variant == "cpu" else int(gpu_layers)
        self.proc = None
        self.port = None
        self.base = None
        self._atexit = False
        self.log_file = None
        self._log_fp = None
        self.gpu_verified = False
        self.health_warn = ""
        self.prompt_extra = ""

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def _verify_gpu(self):
        """从运行日志核实 GPU 是否真的在工作（offload 层数 / 后端名）。"""
        if self.variant == "cpu" or not self.log_file:
            return True
        try:
            with open(self.log_file, "rb") as f:
                f.seek(max(0, os.path.getsize(self.log_file) - 1048576))
                tail = f.read().decode("utf-8", "ignore")
        except OSError:
            return False
        low = tail.lower()
        self.gpu_offloaded = None
        on_gpu = total = None
        m = re.search(r"offloaded\s+(\d+)\s*/\s*(\d+)\s+layers?\s+to\s+gpu", low)
        if m:
            on_gpu, total = int(m.group(1)), int(m.group(2))
        else:
            dev_pat = {"cuda": r"assigned to device cuda\d+",
                       "cuda13": r"assigned to device cuda\d+",
                       "vulkan": r"assigned to device vulkan"}.get(self.variant)
            if dev_pat:
                on_gpu = len(re.findall(dev_pat, low))
                total = on_gpu + len(re.findall(
                    r"assigned to device cpu", low))
        dev_ok = False
        if self.variant in ("cuda", "cuda13"):
            dev_ok = ("using device cuda" in low
                      or "assigned to device cuda" in low)
        elif self.variant == "vulkan":
            dev_ok = ("vulkan" in low and "device" in low)
        self.gpu_verified = dev_ok and (on_gpu or 0) > 0
        if self.gpu_verified:
            if total:
                self.gpu_offloaded = (on_gpu, total)
            self.log("GPU 加速已生效（%s：%s 层在显卡上）"
                     % (self.variant,
                        ("%d/%d" % (on_gpu, total)) if total else str(on_gpu)))
        else:
            self.log("警告：选择了 %s 后端，但未确认层已上 GPU；"
                     "若推理仍很慢，通常是缺 CUDA 运行时或驱动问题，"
                     "请改选 CPU/Vulkan 或在下载管理补装运行库"
                     % self.variant, "warn")
        return self.gpu_verified

    def _pick_port(self):
        """取一个本机空闲端口。

        做法是"绑定 0 → 读回端口 → 立刻关闭"，随后交由子进程监听，两步之间
        存在端口被别的进程抢走的窗口（TOCTOU）。这里不做复杂规避，而是让
        start() 在子进程提前退出时换端口重试一次 —— 比信号量/句柄继承方案
        简单且足够。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def start(self, ready_event_log=None, timeout_sec=900):
        """启动引擎并等 /health 就绪。

        失败路径**必须**把已拉起的子进程收掉：否则健康检查超时/提前退出后
        会留下一个占着显存和内存的孤儿 llama-server，"再点一次启动"就变成
        两个进程抢显存。端口被抢占属可恢复失败，换端口重试一次。
        """
        if self.is_running():
            return True
        last_err = None
        for attempt in (1, 2):
            try:
                return self._start_once(timeout_sec, attempt)
            except IOError as e:
                last_err = e
                self._kill_proc()
                if attempt == 1 and "提前退出" in str(e):
                    self.log("端口可能被占用，换端口重试一次…", "warn")
                    continue
                raise
        raise last_err

    def _start_once(self, timeout_sec, attempt):
        self.port = self._pick_port()
        self.base = "http://127.0.0.1:%d" % self.port

        meta = read_gguf_meta(self.model)
        if meta:
            name = meta.get("general.name") or os.path.basename(self.model)
            if meta.get("__partial__"):
                self.log("模型元数据未完整读出（%s），跳过对话模板检查" % name,
                         "warn")
            elif not meta.get("tokenizer.chat_template"):
                self.log("警告：该 GGUF 未内置对话模板（tokenizer.chat_template），"
                         "翻译请求会退化为纯文本续写，可能出现停不下来/串话；"
                         "建议改用官方发布的 GGUF", "warn")
            else:
                self.log("模型 %s：内置对话模板 %d 字符，EOS=%s"
                         % (name, len(meta["tokenizer.chat_template"]),
                            meta.get("tokenizer.ggml.eos_token_id")), "dim")

        # ctx_size 是「每路」上下文；llama-server 会把总上下文平均分给各槽位，
        # 所以多路时总 ctx 乘以路数，才能保证每条文本仍有同样的上下文预算。
        args = [self.exe,
                "--model", self.model,
                "--host", "127.0.0.1",
                "--port", str(self.port),
                "--ctx-size", str(self.ctx_size * self.parallel),
                "--n-gpu-layers", str(self.gpu_layers),
                "--parallel", str(self.parallel),
                "--temp", "0.1"]
        # ★ 2.0.11：禁用「提示词相似度选槽」。所有请求带同一段系统提示词，
        # 相似度 0.88~0.93 全被 LCP 黏到 slot 2（实测 1621/1714 请求），
        # 另两路空转，三路并发名存实亡。0 = 强制轮转进空闲槽。
        # 仅当多路时加，单路无意义。
        if self.parallel > 1:
            args += ["--slot-prompt-similarity", "0"]
        if self.variant in GPU_VARIANTS:
            # 只要 GPU 识别所需的 I 级行（using device / offloaded N/M layers）。
            # 原来的 --verbose 把 verbosity 开到最大：实测一次运行 25 MB / 28 万行，
            # 请求级 debug 还持续写盘，拖慢整机。
            args += ["-lv", "4"]
        log_file = os.path.join(_LOGS_DIR, "server_%d.log" % int(time.time()))
        self.log_file = log_file
        self._log_fp = open(log_file, "wb")
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        backend_name = {"cpu": "CPU", "cuda": "CUDA 12.4/GPU",
                        "cuda13": "CUDA 13.4/GPU",
                        "vulkan": "Vulkan/GPU"}.get(self.variant, self.variant)
        self.log("启动 llama-server（%s，%s 档，%d 层 GPU offload，"
                 "上下文 %d token × %d 路），端口 %d"
                 % (backend_name, self.tier or "-", self.gpu_layers,
                    self.ctx_size, self.parallel, self.port))
        self.log("运行日志：%s" % log_file)
        self.proc = subprocess.Popen(
            args, cwd=os.path.dirname(self.exe), stdout=self._log_fp,
            stderr=subprocess.STDOUT, creationflags=flags)
        # 把引擎放进 kill-on-close 作业：工具集被任务管理器强杀时，
        # 内核会连带终止它，不再留下占着显存的孤儿（见 _win 注释）。
        if os.name == "nt" and not _assign_server_to_kill_job(self.proc):
            self.log("提示：未能把引擎纳入系统作业托管；若工具集被强杀，"
                     "引擎可能残留（下次启动会自动清理）", "dim")
        if not self._atexit:
            atexit.register(self.stop)
            self._atexit = True

        t0 = time.time()
        health_url = self.base + "/health"
        last_err = ""
        while time.time() - t0 < timeout_sec:
            if self.proc.poll() is not None:
                raise IOError("llama-server 提前退出（返回码 %s），详见 %s"
                              % (self.proc.returncode, log_file))
            try:
                with _http_get(health_url, timeout=5) as r:
                    if r.status == 200:
                        self.log("引擎就绪（用时 %.0f 秒）" % (time.time() - t0))
                        # ★ 2.0.12：引擎就绪后，推理请求切到线程安全的直连
                        # 通道，恢复多路真并发（宿主门面会串行化并发推理）。
                        if self.parallel > 1:
                            set_direct_infer_http(True)
                            self.log("多路推理直连已启用（绕过串行门面，"
                                     "%d 路真并发）" % self.parallel, "ok")
                        self._verify_gpu()
                        self.self_check()
                        return True
                    try:
                        data = json.loads(r.read().decode("utf-8", "ignore"))
                        last_err = str(data.get("error", ""))[:120]
                    except Exception:
                        pass
            except Exception as e:
                last_err = str(e)
            time.sleep(1.5)
        raise IOError("等待引擎就绪超时（%s）" % (last_err or "health 无响应"))

    def translate(self, text, src_lang, tgt_lang, glossary_hits=None,
                  timeout_sec=180, max_tokens=None, use_extra=True,
                  extra_sys=None, temperature=None, context=None,
                  style=None):
        """单条翻译请求。被 max_tokens 截断时抛 OutputTruncated（不重试）。

        提示词里的语言用**全名**（"俄语"）而不是语言码（"ru"）：小模型对
        "将下面的ru文本翻译为简体中文"这类混排会偶发把 ru 当成待译内容。

        official_prompts=True（config 开关）时 user 指令与术语块措辞对齐
        Hy-MT2 官方模板分布，供 A/B 对比；system 规则两版共用。
        """
        if not self.is_running():
            raise IOError("引擎未运行")
        official = bool(getattr(self, "official_prompts", False))
        if src_lang in ("auto", "text"):
            src_lang = detect_text_lang(text) or src_lang
        src = LANG_FULL.get(src_lang)
        tgt = LANG_FULL.get(tgt_lang, tgt_lang)
        sys_msg = (TRANSLATION_SYS_PROMPT_EN if src_lang == "eng"
                   else TRANSLATION_SYS_PROMPT)
        extra = (getattr(self, "prompt_extra", "") or "").strip() \
            if use_extra else ""
        if extra:
            sys_msg = sys_msg + "\n补充要求（优先遵守）：\n" + extra
        if extra_sys:
            sys_msg = sys_msg + "\n补充要求（优先遵守）：\n" + extra_sys
        if official:
            user_msg = ("将以下文本翻译为%s，注意只需要输出翻译后的结果，"
                        "不要额外解释：\n%s" % (tgt, text))
        elif src and src_lang not in ("auto", "text"):
            user_msg = "将下面的%s文本翻译为%s：\n%s" % (src, tgt, text)
        else:
            user_msg = "将下面文本翻译为%s：\n%s" % (tgt, text)
        if context:
            user_msg = ("上下文（仅供理解，不要翻译）：" + chr(10)
                        + str(context) + chr(10) + chr(10) + user_msg)
        if glossary_hits:
            block, _used = glossary_hint_block(glossary_hits,
                                               official=official)
            if block:
                user_msg = block + "\n\n" + user_msg
        if max_tokens is None:
            # ★ 2.0.13：按本次输入长度动态估算（短句不虚设、长段不截断），
            # 上限仍受 ctx 约束——对所有 MOD 通用，不依赖单一 MOD 实测值。
            max_tokens = budget_max_tokens(len(text or ""), self.ctx_size)
        payload = json.dumps({
            # MCP 适配：端点可能要求具体模型名；插件内置引擎仍用默认 hy-mt2。
            "model": getattr(self, "model_name", "hy-mt2"),
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": (0.1 if temperature is None
                            else float(temperature)),
            "max_tokens": max_tokens,
            "stream": False,
        }).encode("utf-8")
        last_err = None
        for attempt in range(2):
            try:
                with _http_post(
                        self.base + "/v1/chat/completions", payload,
                        headers={"Content-Type": "application/json"},
                        timeout=timeout_sec) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, e)
                time.sleep(1.0)
                continue
            choices = data.get("choices") or []
            if not choices:
                last_err = "响应缺少 choices"
                time.sleep(1.0)
                continue
            choice = choices[0]
            out = (choice.get("message") or {}).get("content", "").strip()
            if not out:
                last_err = "空输出"
                time.sleep(1.0)
                continue
            if choice.get("finish_reason") == "length":
                raise OutputTruncated(
                    "输出达到 max_tokens(%d) 被截断" % max_tokens)
            return out
        raise IOError("翻译请求失败：%s" % last_err)

    def self_check(self):
        """启动自检：真跑一条极短翻译，确认模型能正常收尾。

        实测背景：tokenizer 元数据不完整的 GGUF 永远不吐 EOS，一句 "Hello."
        能连生成上千 token 直到请求超时；真拿它去翻几千条文本，就是全部
        超时、白跑一整轮。这里用 16 个 token 的上限提前把它试出来，并把
        原因讲清楚。

        **不阻断启动**（只告警）：避免误伤行为特殊但可用的模型；用户看到
        告警后可以自行换模型或改用其他后端。
        """
        tokenizer_warn = (
            "模型无法正常收尾（输出撞满上限仍未结束）：该 GGUF 的 tokenizer "
            "元数据可能不完整（缺 EOS/EOG 或对话模板），直接翻译会让每条"
            "文本都超时")

        def probe(max_tokens, timeout):
            return self.translate("Hello.", "eng", "chs",
                                  timeout_sec=timeout, max_tokens=max_tokens,
                                  use_extra=False)

        def retry_empty():
            # 16 token 的"空输出"不等于模型坏了：推理型 GGUF 会把预算全花在
            # reasoning_content 上，content 为空（MiniCPM5-2B 实测如此，
            # 256 token 时能正常返回"你好。"）。用更大预算复核一次再下结论。
            try:
                out = probe(256, 180)
            except OutputTruncated:
                self.health_warn = tokenizer_warn
            except Exception as e:
                self.health_warn = (
                    "启动自检请求失败：%s: %s（不影响启动；推理型模型常把小预算"
                    "耗在思考上而返回空内容，可忽略此告警）"
                    % (type(e).__name__, e))
            else:
                self.health_warn = ""
                self.log("启动自检通过（16 token 不足，256 token 复核成功）：%r"
                         % (out[:24] or "(空)"))
                return True
            return False

        try:
            out = probe(16, 120)
        except OutputTruncated:
            self.health_warn = tokenizer_warn
        except OSError as e:
            # "空输出"走推理型模型复核；真正的网络/本地服务错误不要再白等
            # 180 秒，也不要套用"推理型模型空输出"的说辞。
            if "空输出" in str(e):
                if retry_empty():
                    return True
            else:
                self.health_warn = "启动自检请求失败：%s（请检查引擎日志与本地服务）" % e
                self.log("启动自检未通过：" + self.health_warn, "warn")
                return False
        except Exception:
            if retry_empty():
                return True
        else:
            self.health_warn = ""
            self.log("启动自检通过：%r" % (out[:24] or "(空)"))
            return True
        self.log("启动自检未通过：" + self.health_warn, "warn")
        return False

    def _kill_proc(self):
        """杀掉子进程（含其子进程树）并关闭日志句柄。可重复调用。"""
        if self.proc is not None and self.proc.poll() is None:
            pid = self.proc.pid
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None
        fp, self._log_fp = self._log_fp, None
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass

    def translate_batch(self, lines, src_lang, tgt_lang, glossary_hits=None,
                        timeout_sec=180, max_tokens=None,
                        temperature=None):
        """连续对话批量翻译：输入若干源文行，输出带 [n] 编号的译文。"""
        if not self.is_running():
            raise IOError("引擎未运行")
        if src_lang in ("auto", "text"):
            src_lang = detect_text_lang(lines[0] if lines else "") or src_lang
        tgt = LANG_FULL.get(tgt_lang, tgt_lang)
        numbered = chr(10).join("[%d] %s" % (i + 1, line)
                                for i, line in enumerate(lines))
        sys_msg = TRANSLATION_BATCH_SYS_PROMPT
        extra = (getattr(self, "prompt_extra", "") or "").strip()
        if extra:
            sys_msg += chr(10) + "补充要求（优先遵守）：" + chr(10) + extra
        user_msg = ("把下面的连续对话逐行翻译为%s：" % tgt
                    + chr(10) + numbered)
        if glossary_hits:
            block, _used = glossary_hint_block(glossary_hits)
            if block:
                user_msg = block + chr(10) + chr(10) + user_msg
        if max_tokens is None:
            # ★ 2.0.13：按批量输入总长度动态估算，长对话不截断。
            max_tokens = budget_max_tokens(
                sum(len(x) for x in lines), self.ctx_size)
        payload = json.dumps({
            # MCP 适配：端点可能要求具体模型名；插件内置引擎仍用默认 hy-mt2。
            "model": getattr(self, "model_name", "hy-mt2"),
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": (0.1 if temperature is None
                            else float(temperature)),
            "max_tokens": max_tokens,
            "stream": False,
        }).encode("utf-8")
        last_err = None
        for attempt in range(2):
            try:
                with _http_post(
                        self.base + "/v1/chat/completions", payload,
                        headers={"Content-Type": "application/json"},
                        timeout=timeout_sec) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, e)
                time.sleep(1.0)
                continue
            choices = data.get("choices") or []
            if not choices:
                last_err = str(data.get("error", ""))[:120]
                time.sleep(1.0)
                continue
            choice = choices[0]
            content = ((choice.get("message") or {}).get("content") or "").strip()
            if choice.get("finish_reason") == "length":
                raise OutputTruncated("批量输出被截断")
            return content
        raise IOError("批量翻译请求失败（%s）" % (last_err or "无响应"))

    def stop(self):
        set_direct_infer_http(False)
        if self.proc is None and self._log_fp is None:
            return
        self._kill_proc()
        self.log("引擎已停止")



def estimate_tokens(text):
    """保守的多语言 token 估算（混元 12 万词表）：
    汉字/假名/谚文约 1 token/字；拉丁/西里尔字母文本约 0.45 token/字符，
    按 0.55 取偏安全上界；标点空白计入长度。"""
    if not text:
        return 0
    cjk = 0
    for c in text:
        o = ord(c)
        if (0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF
                or 0xAC00 <= o <= 0xD7AF):
            cjk += 1
    other = len(text) - cjk
    return int(cjk * 1.0 + other * 0.55) + 1


def safe_chunk_chars(ctx_size):
    """单次请求可送的最大源文字符数（与画像中的"约 N 字符/段"一致）：
    8192 → 约 3768；16384 → 约 7536。"""
    return int(ctx_size * 0.46)


_SENT_SPLIT = re.compile(r"([。！？!?…\n；;])")
_CLAUSE_SPLIT = re.compile(r"([，,：:])")


_PLACEHOLDER_RE = re.compile(r"\$\$[TR]\d+\$\$")
_ANY_PLACEHOLDER_RE = re.compile(r"\$\$[A-Za-z0-9_]+\$\$")

# 运行期占位符：脚本变量/格式符/控制符。**必须先 mask 再送模型**：
# 提示词要求保留并不可靠，实测模型会把 $level/$name 当成普通英文词译成
# $地点/$名称，导致脚本变量替换失效（news_data.script 等直接引用这些条目）。
_RUNTIME_TOKEN_RE = re.compile(
    r"(\$\$[A-Za-z0-9_]+\$\$"          # $$ACTION$$
    r"|\$[A-Za-z_][A-Za-z0-9_]*"        # $level / $name
    r"|%c\[[^\]]*\]"                    # %c[255,1,255,255]
    r"|%[-+ #0-9.*$]*[A-Za-z]"          # %s / %d / %1$s
    r"|\b[rsg][0-9]?_[a-z0-9_]+\b)")    # r2_details_opt / s_light 等技术后缀
# 换行/制表不再当“严格占位符”：模型少写一个 \n 就把整条退回原文，
# 实测 138 条多行描述因此漏译。送翻前换成真实换行，译后统一写回 \n。
# ★ 2.0.2 终检用：$$R0$$ 形态是**送翻时**的掩码，正常输出里不该出现
#   （restore_runtime_tokens 保证每个掩码恰好还原一次，失败整条回退原文）。
#   终检在成品里扫到它 = 有异常路径漏网，点名让人复核（源文本身含 $$R\d$$
#   的概率可忽略；报告口径是"疑似"，不自动改写）。
_MASK_RESIDUE_RE = re.compile(r"\$\$R\d+\$\$")


def budget_max_tokens(src_chars, ctx_size):
    """★ 2.0.13 按本次输入长度估算输出 token 预算（通用，不随 MOD 失效）。

    为什么不用固定 1536：别的 MOD 若有接近 safe_chars 的长段，译文可能需要
    上千 token，固定值触发 finish_reason=length 截断→递归二分重试，反而更慢。
    为什么不用 ctx*0.6：那与本次输入无关，短句也虚设 4915，放大失控生成。

    正解是看本次要翻多少字：中文输出 token 上界约源字符数的 1.2 倍
    （汉字约 1 token/字，加标点/空格/换行余量），另加 256 缓冲；下限 1024
    覆盖实测最大输出（940），上限不超过窗口 0.6（与旧行为一致的硬顶）。
    """
    est = int(src_chars * 1.2) + 256
    return max(1024, min(est, int(ctx_size * 0.6)))


def mask_runtime_tokens(text):
    """把运行期占位符换成 $$R0$$…，返回 (masked, mapping)。"""
    mapping = {}

    def repl(m):
        ph = "$$R%d$$" % len(mapping)
        mapping[ph] = m.group(0)
        return ph

    return _RUNTIME_TOKEN_RE.sub(repl, text), mapping


def restore_runtime_tokens(text, mapping):
    """回填占位符；任何一个不是恰好出现一次就返回 None（调用方保留原文）。"""
    for ph in mapping:
        if text.count(ph) != 1:
            return None
    for ph, tok in mapping.items():
        text = text.replace(ph, tok)
    return text


# ★ 2.0.24（IWP 全量审计 D10）：半音译粘连签名。模型（1.8B）会把专名
# 音译一半、原文留一半，产出「克里门enko队长」「Tkachenko将军」
# 「$clr_2We正看到」「箱子-kit在那儿」这类玩家不可读、词库回填也救不回
# 的残词（回填要求整词边界，词内碎片永远不命中）。判定前剥掉合法 token：
# 占位符/色码/变量（$$R0$$、%c[...]、$var）、数字、字面 \n——否则
# 「$target正在」「AK-103可以」误报。剥后仍出现「拉丁字母串直接贴汉字」
# 即粘连。大小写混合形态（首字母大写+后续小写）才算：纯大写缩写
# （PC机 的 PC）不误伤。
_GLUE_STRIP_RE = re.compile(
    r"\$\$R\d+\$\$|%c\[[^\]]*\]|\$clr_\d+|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|%[-+ #0-9.*$]*[A-Za-z]|\\n")
_GLUE_RE = re.compile(
    r"[\u4e00-\u9fff][A-Za-z][a-z]+|[A-Za-z][a-z]+[\u4e00-\u9fff]")


def find_glue_residue(text):
    """返回译文里的粘连残词样例（最多 3 个）；无粘连返回 []。

    数字保留在探针里：单位记法（45mm弹壳、125g药剂）的字母串前面
    必然是数字，按"字母串紧邻数字"跳过——这是度量单位不是粘连。
    """
    if not text:
        return []
    probe = _GLUE_STRIP_RE.sub(" ", text)
    hits = []
    for mm in _GLUE_RE.finditer(probe):
        pre = probe[:mm.start()]
        if re.search(r"[\d.]\s*$", pre):
            continue
        hits.append(mm.group(0))
    return hits[:3]


# ★ 2.0.13 圆点注入修复：1.8B 会把遮罩后的颜色码 $$R0$$ 当成列表项，
# 还原后在 %c[...] 后留下 "•"，实测 392 条神器/百科描述被污染（源文零圆点）。
# 只在源文**没有**圆点时清理，避免误伤原文真用到的项目符号。
_BULLET = "\u2022"

def clean_injected_bullets(src, out):
    """源文无圆点而译文有 → 判定为模型注入，删除所有圆点并整理空格。

    保留 round-trip 安全性：只删圆点本身与其后紧邻的一个空格，
    不触碰颜色码/占位符/换行，返回 (清洗后文本, 是否改动)。
    """
    if not out or _BULLET not in out:
        return out, False
    if _BULLET in (src or ""):
        return out, False   # 源文本来就有圆点，可能是合法列表，不动
    cleaned = out
    # "• " / "•" 连同其后紧邻空白一起删（颜色码后常见 %c[...]• %c[...]）
    cleaned = re.sub(re.escape(_BULLET) + r"[ \t]?", "", cleaned)
    # 删完后若颜色码之间留下双空格，收敛为一个（源文该处本无空格）
    cleaned = re.sub(r"(%c\[[^\]]*\])([ \t]{2,})", r"\1 ", cleaned)
    return cleaned, cleaned != out

# 冠词粘连：小模型偶尔把英文冠词 An/A/The 贴在中文译文前（"An神器..."）。
# 只在词首紧邻 CJK 时剥离，避免误伤真正的英文混排。
# ★ 2.0.15：冠词不只出现在行首——颜色码后也会紧贴中文译文
# （实测 %c[default]The科学家）。匹配「行首或颜色码之后」的冠词，
# 替换时保留颜色码本身。
_LEADING_ARTICLE_RE = re.compile(
    r"(%c\[[^\]]*\]|^)(?:An|A|The)(?=[\u4e00-\u9fff])", re.I)

def clean_stuck_article(out):
    """剥离紧贴中文译文的英文冠词（行首/颜色码后），返回 (文本, 是否改动)。"""
    if not out:
        return out, False
    cleaned = _LEADING_ARTICLE_RE.sub(lambda m: m.group(1) or "", out,
                                      count=1)
    return cleaned, cleaned != out


# ★ 2.0.13 通用新闻句式替换：STALKER 各版本的动态广播/新闻存在大量
# 固定英文句式（"We were attacked by a <怪物>"、"<怪物> ambushed us" 等）。
# 这类句子结构固定、只有中间名词变化，逐条送模型既慢又容易漏译。这里做成
# **数据驱动的通用模板**：句式通用 + 名词查词库，任何 MOD 只要名词在库里就
# 能零成本命中；库里没有的名词返回 None，照常送模型，不误伤。
# 只处理纯 ASCII 英文短句，不含占位符/控制符。
_NEWS_PATTERNS = (
    # (正则, 模板)  —— 组1是名词（含可选冠词/复数由正则吃掉）
    (re.compile(r"^We (?:were|got) (?:attacked|ambushed) by (?:a |an |some )?(.+?)[.!]?$", re.I),
     "我们被%s袭击了。"),
    (re.compile(r"^(?:A |An |The )?(.+?) ambushed us[.!]?$", re.I),
     "%s伏击了我们。"),
    (re.compile(r"^(?:A |An |The )?(.+?) attacked us[.!]?$", re.I),
     "%s袭击了我们。"),
    (re.compile(r"^We (?:ran into|encountered|met) (?:a |an |some )?(.+?)[.!]?$", re.I),
     "我们遇到了%s。"),
    (re.compile(r"^We got attacked by (?:a |an |some )?(.+?)[.!]?$", re.I),
     "我们被%s袭击了。"),
)

def pattern_translate(src, lookup):
    """通用句式 + 词库名词 → 确定性译文；无法覆盖返回 None。

    lookup: 单调函数 term -> 译名 或 None（由调用方注入词库查询）。
    """
    s = (src or "").strip()
    if not s or not s.isascii():
        return None
    for pat, tpl in _NEWS_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        noun = m.group(1).strip()
        zh = lookup(noun)
        if not zh:
            return None
        return tpl % zh
    return None


def unescape_newlines(text):
    """送翻前把字面 \\n/\\t 变成真实换行，让模型看到段落结构。"""
    return (text or "").replace("\\r\\n", "\n").replace("\\n", "\n") \
        .replace("\\t", "\t")


def escape_newlines(text):
    """译后统一写回字面 \\n/\\t，保持 X-Ray 文本表的存储约定。"""
    s = (text or "").replace("\r\n", "\n")
    return s.replace("\n", "\\n").replace("\t", "\\t")


def restore_linebreaks(text, count):
    """模型改动换行数时的确定性兜底：把译文的换行数对齐到原文。

    ★ 2.0.13：旧版只在译文**一个换行都没有**时才补，实测大量
    “原文 10 行 → 译文 8 行”的部分丢失被当成 kept_first 原样写回，
    游戏里表现为整段错行（用户实测百科/描述条目）。新逻辑同时处理
    换行不足（按句末/从句标点补）和换行过多（把多余换行并回相邻行），
    且只在缺/多的量不大（≤ 原文行数）时动手，避免把模型正常分行打乱。
    """
    if count <= 0 or not text:
        return text
    cur = text.count("\n")
    if cur == count:
        return text
    # 差异过大时不动手：原文 3 行译文 30 行属于模型自由分段，
    # 强行对齐只会破坏本就可读的译文。
    if abs(cur - count) > max(count, 1):
        return text

    if cur < count:
        # 缺行：优先按句末标点切，不够再退到从句标点。
        parts = [p for p in re.split(r"(?<=[。！？!?…])", text) if p]
        if len(parts) < count + 1:
            parts = [p for p in
                     re.split(r"(?<=[。！？!?…；;，,、])", text) if p]
        if len(parts) < 2:
            return text
        need = min(count, len(parts) - 1)
        out = []
        for i, part in enumerate(parts[:-1]):
            out.append(part + ("\n" if i < need else ""))
        out.append(parts[-1])
        fixed = "".join(out)
        return fixed if fixed.count("\n") == count else text

    # 多行：把多余的换行符号并回（删除多余 \\n，保留非空行内容）。
    lines = text.split("\n")
    keep = []
    for ln in lines:
        if len(keep) < count + 1:
            keep.append(ln)
        elif ln.strip():
            # 已够行数又有正文：并入最后一行，不丢字。
            keep[-1] = keep[-1].rstrip() + ln
        # 纯空行直接丢弃
    fixed = "\n".join(keep)
    # 行数还多（原文就是空行多）：从尾部合并空行
    while fixed.count("\n") > count:
        idx = fixed.find("\n")
        # 优先合并连续空行，其次合并任意相邻换行
        m = re.search(r"\n\s*\n", fixed)
        if m:
            fixed = fixed[:m.start()] + "\n" + fixed[m.end():]
        else:
            fixed = fixed.replace("\n", "", 1)
    return fixed if fixed.count("\n") == count else text


# 专名/歌名/型号保护：只按**结构**判断，不写死任何 MOD 文件名。
# 文件名的 credits/ogg/music/songs/enc_weapons 只是通用提示；用户可关闭。
# ID 引用扫描（跨 MOD 通用；只认结构，不认具体文件名）
_XML_REF_TAG_RE = re.compile(
    r"<(?:text|name|bio|title)\b[^>]*>\s*([A-Za-z0-9_][A-Za-z0-9_.\-]{0,100})\s*</",
    re.I)
_XML_REF_ATTR_RE = re.compile(
    r"\b(?:name|hint|bio|caption|descr|title)\s*=\s*[\"']"
    r"([A-Za-z0-9_][A-Za-z0-9_.\-]{0,100})[\"']",
    re.I)
_SCRIPT_TRANSLATE_RE = re.compile(
    r"translate_string\s*\(\s*[\"']([^\"']+)[\"']\s*\)")
_SCRIPT_QUOTED_RE = re.compile(r"[\"']([A-Za-z0-9_][A-Za-z0-9_.\-]{1,100})[\"']")

_PROTECT_FILE_HINTS = ("credits", "ogg", "music", "songs", "enc_weapons",
                       "snames", "fnames", "character_name")
# ★ 2.0.11：冒号/破折号前必须是「短标题」——不含句末标点、长度收紧。
# 旧式 `[^\n:：]{0,80}` 会吞掉 `Just about anyone, buddy. Think about it:`
# 这类对话（前 80 字内有冒号就整条当标题跳过，实测 12+ 条长对话未译）。
# 真标题（Vano: ...、MP5 — Sanitizer）冒号前短且无 .!?，仍受保护。
# ★ 2.0.17：标题规则再收紧——冒号前段含逗号/引号的是叙述句
# （实测 "Men, take note: only a bullet..." 被当标题整段跳过）。
# 标签形态（Name: / Radio: / Quest:）冒号前是 1~3 个词、无逗号。
_TITLE_LIKE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9 .\-'/]{0,40}[:：]\s*[A-Za-z0-9]")
_TITLE_DASH_RE = re.compile(
    r"^[A-Za-z0-9][^\n.!?！？—–]{0,40}[—–]\s*"
    r"[A-Za-z0-9][^\n.!?！？]{0,40}$")
# 型号名（MP5 "Sanitizer"、AK-74 "Рubin"）：引号前只允许最多 5 个
# 「首字母大写的词」。旧式 `[A-Za-z0-9 ./\-]*` 会一路吞掉小写单词，把
# `You have unlocked the "Heavy Pockets" achievement.` 这类整句英文当成
# 型号整条跳过（实测成就解锁提示 100% 未译）。
_MODEL_LIKE_RE = re.compile(
    r'^(?:[A-Z0-9][A-Za-z0-9\-]*[ .\-/]?){1,5}["“][^"”]{1,80}["”]')
_CODE_LIKE_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .\-/_]{1,30}$")
_COPYRIGHT_RE = re.compile(r"^(?:©|\(c\)|copyright\b)", re.I)
# ★ 2.0.17 误伤修正（实测 2.0.15 产物 59 条漏译里 12 条中招）：
# - `FAILED\b` 在 re.I 下命中叙述文里的 "failed"（"a result of failed
#   experiments"）→ 改为大小写敏感的显式 FAILED；
# - `teleport\w*` 命中叙事词 "teleport closer" → 只认调试标识符形态
#   `teleport_…` 或 `teleport:`/`teleport =`。
# 其余 token（onItemDrop/npcObj/reinit/kamps 等驼峰或生僻词）保留 re.I。
_DEBUG_LIKE_RE = re.compile(
    r"(?:onItemDrop|npcObj|\bFAILED\b|sect\s*-|obj\s*:|gvid\s*:|"
    r"^(?:~|(?i:\[[a-z0-9_:.\- ]+\]$))|"
    r"\bteleport_[a-z0-9_]+|\bteleport\s*[:=]|"
    r"\b(?:reinit|kamps|npcItems|isVisual\w*)\b|"
    r"content\s*:)")
_DEBUG_PHRASES = {"dead", "none", "null", "true", "false",
                  "avail end", "start here", "reinit timer"}

# 口径/型号不是“正文”：9x18 PM、5.56x45 M995 AP、12.7x99mm、.44 Magnum
# 这类短技术串必须整串保留，不能被模型当时间/自动步枪翻译。
_TECH_SPEC_RE = re.compile(
    r"^\s*(?:\d+\s*[x×]\s*)?\d+(?:[.,]\d+)?\s*[x×]\s*"
    r"\d+(?:[.,]\d+)?(?:\s*[A-Za-z0-9.+/\-]{0,12})?\s*$", re.I)
_TECH_CALIBER_RE = re.compile(
    r"^\s*\.\d{2,3}\s+[A-Za-z][A-Za-z0-9 .\-/]{0,24}$", re.I)


def looks_like_technical_text(text):
    """口径/弹药/尺寸类整串：只做技术串保护，不当普通正文。

    ★ 2.0.21（全 MOD 重放审计）：含**句末标点**的串是正文（实测 NS7
    'Магазин на 10 патронов калибра 10.3x45мм.'——弹匣描述带口径被整条
    跳过），尾部句号/问号排除。
    """
    s = str(text or "").strip()
    if not s or len(s) > 60 or re.search(r"[。！？!?；;]", s):
        return False
    if re.search(r"[.。！？!?]\s*$", s) and len(s) > 20:
        return False                     # 句末标点 = 句子，非纯规格串
    if _TECH_SPEC_RE.match(s) or _TECH_CALIBER_RE.match(s):
        return True
    return bool(re.search(r"\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?", s))


def looks_like_generated_noise(rel, text):
    """旧的 ___scriptE__ / ___cfgxml__ 生成表：短 ASCII 多是日志/键名。"""
    low = (rel or "").lower().replace("\\", "/")
    if "___scripte__" not in low and "___cfgxml__" not in low:
        return False
    s = str(text or "").strip()
    if not s or len(s) > 80 or re.search(r"[。！？!?；;]", s):
        return False
    if s.isascii():
        return True
    return bool(re.search(
        r"^(?:gulag|ignored|selected|actor\s+(?:start|end)|spawn|id):",
        s, re.I))


def looks_like_model_name(text):
    """纯拉丁武器/物品型号名：会话词表不应把它们当人名/普通物品翻译。"""
    s = str(text or "").strip()
    if not s or not re.search(r"[A-Za-z]", s):
        return False
    if re.search(r"\d", s) or re.search(r"[“”\"'«»/]", s):
        return True
    if re.search(r"[A-Z]{2,}", s):
        return True
    if "-" in s and len(s.split()) >= 2:
        return True
    caps = sum(1 for w in s.split() if w[:1].isupper())
    return s.isascii() and caps >= 2


def _has_translatable_text(text):
    """剔除运行期占位符后仍有 2 个以上连续字母，才值得送翻。"""
    clean = _RUNTIME_TOKEN_RE.sub("", text or "")
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]{2,}", clean))


def detect_text_lang(text):
    """按字母构成判断单条源文是俄语还是英语。

    X-Ray 的 `<rus>` 字段在不少 MOD 里实际放的是英语；只按字段名选源语言
    会把英语提示词成俄语。这里只做粗判：西里尔字母占优→rus，拉丁占优→eng；
    混合专名/缩写不改变主语言；太短（<2 个字母）返回空，交给文件级提示。
    """
    s = text or ""
    cyr = len(re.findall(r"[А-Яа-яЁё]", s))
    lat = len(re.findall(r"[A-Za-z]", s))
    if cyr >= 2 and cyr >= lat:
        return "rus"
    if lat >= 3 and lat > cyr:
        return "eng"
    return ""


def looks_like_code_or_debug(rel, text):
    """结构性噪声：调试日志/路径/URL/代号。**总是**跳过，不占“专名保护”。"""
    s = (text or "").strip()
    if not s or len(s) <= 1:
        return True
    if s.lower() in _DEBUG_PHRASES or _COPYRIGHT_RE.match(s):
        return True
    if _DEBUG_LIKE_RE.search(s):
        # ★ 2.0.11 [Say nothing] / [Issue Command] 是对话选项，不是调试标记。
        # 只在玩家可见标签表豁免，且方括号内必须是可读英文短语（含空格）。
        if not (is_player_facing_label(rel, s)
                and re.fullmatch(r"\[[A-Za-z][A-Za-z ]{2,60}\]", s)):
            return True
    if s.lower().startswith(("http://", "https://", "www.")):
        return True
    # 短代号/弹药规格：只有“全大写且没有任何小写字母”才跳过。
    # 旧规则把“Набор 1 / Патроны 9х18мм ПМ / 30 х 5.45x39мм”都当型号，
    # 实测 459 条正文因此漏译；含小写词的串一律按正文送翻。
    name_file = any(x in (rel or "").lower() for x in (
        "character_name", "snames", "fnames", "bio"))
    zone_file = any(x in (rel or "").lower() for x in (
        "string_table_enc_zone", "string_table_levels", "mapspots"))
    if not (name_file or zone_file):
        if (any(c.isdigit() for c in s)
                and re.fullmatch(
                    r"[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9 .\-/_]{0,40}", s)):
            return True
        if _CODE_LIKE_RE.match(s) and any(c.isdigit() for c in s):
            return True
    return False


def _is_model_code_form(rel, text):
    """code 过滤里"全大写型号/弹药规格"那一支是否为命中原因。

    ★ 2.0.22（每 MOD 随机文件测试）：全库 398 条被 code 拦掉的条目其实
    词库有整串中文译名，其中 121 条与人工汉化逐字一致（AK-74→AK-74突击步枪、
    AEK-971→AEK-971突击步枪、3D PDA→三维PDA、АН-94→AN-94突击步枪）。
    按 2.0.21 同一原则（词库整串命中 = 明确翻译意图）放行这一支；
    调试词/URL/版权行/单字符（dead、True、None、ч）不是型号形态，仍跳过。
    """
    s = (text or "").strip()
    if len(s) <= 1 or s.lower() in _DEBUG_PHRASES or _COPYRIGHT_RE.match(s):
        return False
    if _DEBUG_LIKE_RE.search(s):
        return False
    if s.lower().startswith(("http://", "https://", "www.")):
        return False
    low = (rel or "").lower()
    if any(x in low for x in ("character_name", "snames", "fnames", "bio",
                              "string_table_enc_zone", "string_table_levels",
                              "mapspots")):
        return False
    return bool(any(c.isdigit() for c in s)
                and re.fullmatch(r"[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9 .\-/_]{0,40}", s))


def looks_like_identifier_text(text):
    """纯小写 ASCII 的“段落/任务/节名”不是用户正文（如 resque information）。"""
    s = (text or "").strip()
    if not s:
        return True
    if re.fullmatch(r"[a-z0-9_.\- /]{1,90}", s) and not re.search(
            r"[.!?,;:()\[\]{}]", s):
        return True
    # 纯 ASCII、无句末标点、<=6 个词：多为任务/段落/角色/资源名
    # （Courier document / My map task / Gans Wiberg / Karta Name），
    # 不是用户正文；但「Disable Info」这类多个首字母大写的界面标签
    # 是给玩家看的，不能当标识符跳过。
    if (s.isascii() and len(s.split()) <= 6
            and not re.search(r"[.!?。！？]", s)):
        caps = sum(1 for w in s.split() if w[:1].isupper())
        if caps <= 1:
            return True
    return False


# ★ 2.0.11 玩家可见短标签豁免：成就名/声望等级/武器属性/对话动作这类
# 单个英文词或 Title Case 短语，恰好和「任务/段落名」同形，被
# looks_like_identifier_text 一律跳过（实测 IWP 129 条该译未译：
# Radiotherapy/Enemy/Magnification/[Say nothing]）。这些表是给玩家看的 UI，
# 必须翻译；只在下列目录豁免，新闻/物品/人物名表仍按原规则跳过。
_PLAYER_LABEL_DIRS = ("achievement", "game_relations", "binoc",
                      "dialog", "relation", "reputation", "rank",
                      "quick_menu", "companion",
                      # ★ 2.0.21（全 MOD 重放审计）：以下目录的短串是
                      # 玩家可见正文，实测被 identifier 误跳（WOT 武器
                      # 升级名 'Synthetic barrel thickener'、新闻碎片
                      # 'a merc'、UI 'Dismissable'/'March'）
                      "news", "dynamic_news", "pda", "options",
                      "equipment", "quests", "loadscreen", "land_names",
                      "weapons_upgrades", "upgrades", "barter", "cryray",
                      # ★ 2.0.22：藏宝点/储物点名是 PDA 里玩家可见正文，
                      # 实测 679 条被 identifier 跳过（'Backpack under tree'、
                      # 'Chest on the roof'、'Safe under the bush'）。此前被
                      # 软层直取掩盖，软层退役后缺口暴露。
                      "treasure", "secrets")

# ★ 2.0.12：全大写短标签的豁免目录。比 _PLAYER_LABEL_DIRS 更窄——
# binoc（武器属性表）里的 NVIDIA DLSS 是真型号缩写，必须继续保护，
# 所以不放进来；只放行成就名、军衔声望、同伴菜单、对话选项里的
# 全大写命令/标题（FOLLOW ME、DO NOT LOOT CORPSES、S.C.H.O.L.A.R.）。
_UPPER_LABEL_DIRS = ("achievement", "game_relations", "relation",
                     "reputation", "rank", "quick_menu", "companion",
                     "dialog",
                     # ★ 2.0.21：新闻/任务/加载屏的全大写标题是玩家正文
                     # （'WHO TOOK MY STASH?!!!'、'NIMBLE'S HANDBOOK'）
                     "news", "dynamic_news", "quests", "loadscreen")


def is_player_facing_label(rel, text=""):
    low = (rel or "").lower().replace("\\", "/")
    if not any(x in low for x in _PLAYER_LABEL_DIRS):
        return False
    s = (text or "").strip()
    # 真对话长句本来就走正常翻译路径，这里只豁免短标签；
    # 长度上限与 identifier 判定保持一致，避免把整段对话误判成标签。
    return bool(s) and len(s) <= 90


# ★ 2.0.25（aera 与 CHS 人工包全量对照 P3）：条目 id 后缀是引擎命名惯例
# 里的"玩家可见字段"标记——st_xxx_name / xxx_descr / ui_yyy_text 这类
# id 的内容就是物品名/描述/界面文案（实测 AREA 闪存盘名 "Flash drive
# with alloys data" 等 2206 条被 identifier 过滤整类跳过，人工包全译）。
# 后缀白名单是跨 MOD 通用的引擎约定，不含任何具体 MOD 词。
_PLAYER_ID_SUFFIX_RE = re.compile(
    r"(?:_|^)(?:name|descr|description|text|title|hint|info|tips|caption)"
    r"(?:_?\d+)?$",
    re.I)


def _id_player_visible(eid):
    """条目 id 以玩家可见字段后缀结尾 → 不按 identifier/technical 跳过。"""
    return bool(eid) and bool(_PLAYER_ID_SUFFIX_RE.search(str(eid)))


def _is_prose_sentence(s):
    """整句正文（不是标题/型号名）。

    ★ 2.0.22（每 MOD 随机文件测试 + 人工参考对照）：2.0.21 的注释声称
    _TITLE_LIKE/_TITLE_DASH 已"长句收口"，但代码只对 _MODEL_LIKE 生效，
    标题分支从未收口——全库 23 条正文被当标题跳过，人工汉化全部翻译：
      `Connection error: CD-Key already in use.`（GAMMA：连接错误: CD-Key已被使用.）
      `Stash: blocked entrance to the underground, inside an L-shaped pipe.`
      `06:00 \\nЭх... Уже пора вставать...`（时间戳前缀被当标签）
    真标题（`Vano:`、`MP5 — Sanitizer`）短且无句末标点，不受影响。
    """
    n = len(s)
    if n >= 40 and re.search(r"[.!?。！？]\s*[\"”»']?\s*$", s):
        return True
    return n > 90 and len(re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", s)) >= 8


_TRACK_LIST_RE = re.compile(r"(?:ТРЕК|Трек|TRACK|Track|曲目)\s*[:：]?\s*\d+",
                            re.I)


def looks_like_hard_protected_file(rel):
    """制作名单/音乐列表：连核心术语也不替换；武器短名只保护非词库型号。"""
    low = (rel or "").lower().replace("\\", "/")
    return any(x in low for x in ("credits", "ogg", "music", "songs"))


def looks_like_proper_text(rel, text):
    """专名/歌名/型号：只在用户勾选“保护专名”时跳过。"""
    s = (text or "").strip()
    if not s:
        return False
    low = (rel or "").lower().replace("\\", "/")
    # 文件级强保护：制作名单、音乐/歌曲列表整张表都不翻；人物/武器/名字表
    # 只保护短名，长描述仍交给模型（例如武器说明）。
    hint = next((h for h in _PROTECT_FILE_HINTS if h in low), "")
    if looks_like_hard_protected_file(rel):
        # ★ 2.0.22（全 MOD 全文件测试）："ogg/music" 命中的其实是物品表——
        # `string_table_ogg_player.xml` 是 MP3 播放器与音乐闪存盘的名字/描述，
        # 玩家在物品栏就能看到，人工汉化对长描述全部翻译。只放行这类长正文；
        # 制作名单与人物名字表仍整表保护。
        # 但**含曲目列表的条目不放行**：ogg_flash_* 的描述后面跟着
        # `ТРЕК 01: Алиса — Веретено` 这类真实歌名/歌手（全库 54 条），
        # 整串送翻会把歌名译错，人工也只译了前半句描述——宁可整条保留。
        if (hint in ("ogg", "music", "songs") and "credits" not in low
                and _is_prose_sentence(s) and not _TRACK_LIST_RE.search(s)):
            return False
        return True
    if hint == "enc_weapons" and len(s) <= 40 and len(s.split()) <= 6:
        return True
    # ★ 2.0.21（全 MOD 重放审计）：_MODEL_LIKE 只对短串生效——长正文
    # （The "underwear" of an exoskeleton...）引号紧跟 Title 词也会命中
    # 型号形态，实测整段物品描述被跳过。>90 字符或含句末标点不判型号。
    # ★ _TITLE_LIKE/_TITLE_DASH 同口径收口：长句不是标题（'The situation
    # is this: ...' 实测整段漏译）；玩家可见目录（quests/news/dialog 等）
    # 的冒号标题是任务名/新闻标题，送翻不保护。
    _label_dir = any(x in low for x in _PLAYER_LABEL_DIRS)
    if (not _label_dir and not _is_prose_sentence(s)
            and (_TITLE_LIKE_RE.match(s) or _TITLE_DASH_RE.match(s)
                 or (_MODEL_LIKE_RE.match(s) and len(s) <= 90
                     and not re.search(r"[.!?。！？]\s*[\"”»]?\s*$", s)))):
        return True
    if len(s) <= 30 and s.isupper() and any(c.isalpha() for c in s):
        # ★ 2.0.7：只保护拉丁字母的型号/缩写（NVIDIA DLSS、AK-74）。
        # 西里尔全大写短名（БАРМЕН、ПРИЗРАК、ЗВЕРОБОЙ…）在人物表/SMS
        # 名字表里是**普通身份称呼**，旧规则把它们整批原样保留、连模型
        # 都不调用。这里放行为"不自动保护"：调用方先用 termbase_lookup
        # 取库里既有译名，没有的送模型翻译。
        if re.search(r"[A-Za-z]", s):
            # ★ 2.0.12：玩家可见的全大写命令/菜单项（FOLLOW ME、WAIT HERE、
            # DO NOT LOOT CORPSES、S.C.H.O.L.A.R. 成就名）是**正文**，不是
            # 型号缩写。目录白名单命中时放行送翻；NVIDIA DLSS、AK-74 等
            # 真型号不在这些目录，仍受保护。
            low2 = low.lower().replace("\\", "/")
            if any(x in low2 for x in _UPPER_LABEL_DIRS) and len(s) <= 30:
                return False
            return True
        return False
    return False


def looks_like_protected_text(rel, text):
    """兼容旧接口：结构性噪声或专名。"""
    return (looks_like_code_or_debug(rel, text)
            or looks_like_proper_text(rel, text))


# ----------------------------------------------------------------------
# 脚本字面文本上下文过滤
#
# 上游 getScriptPotentialTexts 会把脚本里**所有**带字母的引号串都抽出来。
# 实测：63 个脚本、1890 处赋值、46 处 string_find、11 处 string_gsub
# 被换成裸 ID，逻辑串变成 nil。这里按“这一行在干什么”做保守白名单：
#   * log/print/debug/error/assert/trace 一律不动；
#   * string_find/string_gsub/比较运算一律不动（逻辑串）；
#   * 只改显示类调用和明确的文本变量赋值。
# 宁可漏几条界面文本，也不能把脚本逻辑改坏。
# ----------------------------------------------------------------------
_SCRIPT_LOG_CALL_RE = re.compile(
    r"\b(?:log1?|log2|log3|log_message|printf|print|debug|error|assert|"
    r"trace|console|exec)\s*\(", re.I)
_SCRIPT_LOGIC_CALL_RE = re.compile(
    r"\b(?:string_find|string_gsub|string_match|string_gmatch|"
    r"string\.find|string\.gsub|string\.match|string\.gmatch)\s*\(",
    re.I)
_SCRIPT_UI_CALL_RE = re.compile(
    r"\b(?:SetText|SetName|SetTextTip|InitStatic|InitButton|AddText|AddLine|"
    r"send_tip|hud_msg|add_spot_on_map|give_talk_message|map_add_user_spot|"
    r"show_message|show_tip|talk_message|send_message|add_news|add_tip|"
    r"message_box|set_level_changer_invitation)\s*\(", re.I)
_SCRIPT_ASSIGN_RE = re.compile(
    r"^\s*(?:local\s+)?(?:self\.|this\.|obj\.)?([A-Za-z_]\w*)\s*=\s*[^=]")
_SCRIPT_TEXT_VAR_RE = re.compile(
    r"(?:^|_)(?:text|txt|msg|message|hint|caption|title|descr|description|"
    r"name|news|label|tip)(?:_|$)", re.I)


def strip_script_comment(line):
    """去掉 -- 行注释，保留引号内的 --（如 "a--b"）。"""
    out = []
    quote = None
    escape = False
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
        else:
            if ch in "\"'":
                quote = ch
                out.append(ch)
            elif ch == "-" and i + 1 < len(line) and line[i + 1] == "-":
                break
            else:
                out.append(ch)
        i += 1
    return "".join(out)


_SCRIPT_EXTRA_DISPLAY_RES = (
    re.compile(
        r"(?:set_level_changer_invitation|set_description)\s*\(\s*"
        r"([\"'])(.*?)\1", re.S),
    re.compile(
        r"AddItem\s*\(\s*game\.translate_string\s*\(\s*([\"'])(.*?)\1",
        re.S),
)
_SCRIPT_TRANSLATE_ARG_RE = re.compile(
    r"game\.translate_string\s*\(\s*([\"'])(.*?)\1", re.S)


def extra_script_display_literals(whole):
    """补上游漏掉的显示调用字面串（只收西里尔文本，避免碰 ID/代码）。"""
    out = set()
    for pattern in _SCRIPT_EXTRA_DISPLAY_RES:
        for m in pattern.finditer(whole or ""):
            inner = m.group(2)
            if re.search(r"[А-Яа-яЁё]", inner):
                out.add(m.group(1) + inner + m.group(1))
    return out


def script_translate_literal_args(whole):
    """已包在 game.translate_string(...) 里的字面串：替换时不能再套一层。"""
    return {m.group(1) + m.group(2) + m.group(1)
            for m in _SCRIPT_TRANSLATE_ARG_RE.finditer(whole or "")}


def script_literal_allowed(line, literal):
    """这一行的这个字面串是否允许改写/送翻。"""
    code = strip_script_comment(line)
    if not code.strip():
        return False
    if _SCRIPT_LOG_CALL_RE.search(code):
        return False
    if _SCRIPT_LOGIC_CALL_RE.search(code):
        return False
    # 比较/连接判断里的字符串通常是标签、档位、状态名，不能换。
    if re.search(r"==|~=|<=|>=", code):
        return False
    if _SCRIPT_UI_CALL_RE.search(code):
        return True
    m = _SCRIPT_ASSIGN_RE.match(code)
    if m and _SCRIPT_TEXT_VAR_RE.search(m.group(1)):
        return True
    return False


def script_literal_allowed_in_file(whole, cand):
    """候选串在脚本里的**每一次**出现都必须是安全上下文，才允许改写。

    任何一次出现在 log/string_find/比较里就整条跳过；全局替换无法区分
    同一字面串的不同出现位置，保守跳过比改坏逻辑好。
    """
    if len(cand) < 2 or cand[0] not in "\"'" or cand[-1] != cand[0]:
        return False
    inner = cand[1:-1]
    if not inner.strip():
        return False
    for needle in (cand, inner):
        found = False
        start = 0
        while True:
            idx = whole.find(needle, start)
            if idx < 0:
                break
            found = True
            line_start = whole.rfind("\n", 0, idx) + 1
            line_end = whole.find("\n", idx)
            if line_end < 0:
                line_end = len(whole)
            if not script_literal_allowed(whole[line_start:line_end], inner):
                return False
            start = idx + len(needle)
        if found:
            return True
    return False


def split_protected(text, max_chars, pattern):
    """按 split_long_text 切分，但**绝不切断 pattern 的匹配**。

    强制术语模式下原文被换成 $$T1$$ 这类占位符；按字符硬切若切在占位符中间，
    模型会看到半截标记，回填必然失败并退回旧的切片做法（质量下降且难排查）。
    做法：先把每个匹配替换成一个私有区单字符，切分后再还原 —— 匹配永远完整。
    """
    spans = list(pattern.finditer(text))
    if not spans or len(spans) > 5000:
        return split_long_text(text, max_chars)
    if any(0xE000 <= ord(c) <= 0xF8FF for c in text):
        # 源文本已含私有区字符时不能用 PUA 做哨兵（会被误还原）；
        # 退回普通切分：占位符可能被切断，但 _translate_force 会校验并回退，
        # 不会产出坏译文。
        return split_long_text(text, max_chars)
    mapping = {}

    def repl(m):
        ch = chr(0xE000 + len(mapping))
        mapping[ch] = m.group(0)
        return ch

    protected = pattern.sub(repl, text)
    parts = split_long_text(protected, max_chars)
    if not mapping:
        return parts
    out = []
    for part in parts:
        for ch, orig in mapping.items():
            if ch in part:
                part = part.replace(ch, orig)
        out.append(part)
    return out


def split_long_text(text, max_chars):
    """把超长文本切成 ≤ max_chars 的段：先按句末标点（。！？!?…换行；分号），
    再按从句标点（逗号等），最后硬切。分隔符随前文保留，绝不丢字、绝不截断。"""
    if len(text) <= max_chars:
        return [text]

    def split_keep(pattern, s):
        """按捕获组切分，分隔符并回前一片段。"""
        parts = pattern.split(s)
        units, buf = [], ""
        for i, piece in enumerate(parts):
            buf += piece
            if i % 2 == 1:
                if buf:
                    units.append(buf)
                buf = ""
        if buf:
            units.append(buf)
        return units

    def hard_wrap(s, limit):
        return [s[i:i + limit] for i in range(0, len(s), limit)]

    units = split_keep(_SENT_SPLIT, text)
    fine = []
    for u in units:
        if len(u) <= max_chars:
            fine.append(u)
            continue
        for clause in split_keep(_CLAUSE_SPLIT, u):
            fine.extend(hard_wrap(clause, max_chars))

    chunks, cur = [], ""
    for u in fine:
        if len(cur) + len(u) <= max_chars:
            cur += u
        else:
            if cur:
                chunks.append(cur)
            cur = u
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]


_TAG_TEXT = re.compile(
    r"<(?:text|rus|eng|ukr|ger|fra)\b[^>]*>(.*?)</(?:text|rus|eng|ukr|ger|fra)>",
    re.I | re.S)

_SCAN_KIND_MAP = {"text": "text", "cfg": "cfgxml", "ltx": "ltx",
                  "script": "scriptL"}


def _iter_candidate_strings(txt, kind):
    """按文件类型粗提"可能被翻译的字符串"，只用于规模估计与分档推荐。"""
    if kind in ("ltx", "cfgxml", "scriptL", "scriptE"):
        for m in re.finditer(r'"([^"\n]{2,})"', txt):
            s = m.group(1).strip()
            if s:
                yield s
    else:
        for m in _TAG_TEXT.finditer(txt):
            s = re.sub(r"\s+", " ", m.group(1)).strip()
            if s:
                yield s


def classify_xray_file(full, rel):
    """判定 X-Ray 文件类型：'text' / 'cfg' / 'ltx' / 'script' / None。

    勘察（扫描选档）与执行（真正翻哪些文件）**必须共用这一处判定**，
    否则"扫描说有 3000 条"和"实际翻了哪些文件"不是同一批 —— 旧实现两条
    口径各写一套（勘察只看路径里有没有 text 目录，执行还会嗅探 <string>
    头），统计与行为会分歧。
    """
    low = full.lower()
    parts = [x.lower() for x in rel.replace("\\", "/").split("/")]
    if low.endswith(".ltx"):
        return "ltx"
    if low.endswith(".script"):
        return "script"
    if low.endswith(".xml"):
        if "text" in parts[:-1]:
            return "text"
        try:
            with open(full, "rb") as f:
                head = f.read(65536).lower()
        except OSError:
            return "cfg"
        if b"<string" in head and b"id=" in head and (
                b"<text" in head or b"<rus" in head
                or b"<eng" in head or b"<ukr" in head):
            return "text"
        return "cfg"
    return None


def analyze_input_dir(in_dir, mode="text", log=None, cancel=None,
                      max_files=200000):
    """勘察待翻译目录：文件数 / 体积 / 最大文件 / 最长单条文本，
    并按内置档位给出超长条数与推荐档位。只读，不修改任何文件。

    要逐文件读内容才能统计"最长可译文本"，所以超大目录会明显耗时：
      * 每 500 个文件写一条进度，用户知道它在动；
      * 超过 max_files 停止统计并在结果里标 truncated（不再默默卡住）；
      * cancel 为 threading.Event 时支持中途取消。
    长度统计只保留"直方图 + 最长项"，不再把所有 (长度, 文件名) 元组堆在
    内存里（数万条文本就是几十万个元组）。
    """
    suffixes = {
        "text": (".xml",), "cfgxml": (".xml",),
        "ltx": (".ltx",), "scriptL": (".script",), "scriptE": (".script",),
        "auto": (".xml", ".ltx", ".script"),
    }
    want = suffixes.get(mode, (".xml",))
    n_files = 0
    total_bytes = 0
    max_file = (0, "")
    hist = {}
    n_strings = 0
    n_files_with_text = 0
    max_str = 0
    max_str_file = ""
    truncated = False
    cancelled = False
    for root0, dirs, files in os.walk(in_dir):
        dirs[:] = [d for d in dirs
                   if d not in ("translated_hy_mt2", "__temp__")
                   and not d.endswith(".git")]
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        parent_name = os.path.basename(root0).lower()
        if mode == "cfgxml" and parent_name == "text":
            continue
        for fn in files:
            if not fn.lower().endswith(want):
                continue
            full = os.path.join(root0, fn)
            rel = os.path.relpath(full, in_dir)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > 20 * 1024 * 1024:
                continue
            n_files += 1
            total_bytes += size
            if size > max_file[0]:
                max_file = (size, full)
            if n_files > max_files:
                truncated = True
                break
            try:
                with open(full, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            txt = None
            for enc in encoding_chain(raw):
                try:
                    txt = raw.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if txt is None:
                continue
            kind = mode
            if mode == "auto":
                kind = _SCAN_KIND_MAP.get(
                    classify_xray_file(full, rel), "cfgxml")
            file_strings = 0
            for s in _iter_candidate_strings(txt, kind):
                length = len(s)
                hist[length] = hist.get(length, 0) + 1
                n_strings += 1
                file_strings += 1
                if length > max_str:
                    max_str, max_str_file = length, full
            if file_strings:
                n_files_with_text += 1
            if log and n_files % 500 == 0:
                log("  勘察中…已读 %d 个文件" % n_files)
        if truncated:
            if log:
                log("  文件数超过 %d，勘察提前结束（统计口径见结果说明）"
                    % max_files)
            break
    per_tier = {}
    for t in BUILTIN_TIERS:
        cap = safe_chunk_chars(TIER_PROFILES[t]["ctx"])
        per_tier[t] = sum(c for L, c in hist.items() if L > cap)
    if max_str > safe_chunk_chars(TIER_PROFILES["mt2"]["ctx"]):
        recommend = "7b"
    else:
        recommend = "mt2"
    return {
        "files": n_files,
        "bytes": total_bytes,
        "max_file": max_file,
        "max_str": max_str,
        "max_str_file": max_str_file,
        "n_strings": n_strings,
        "files_with_text": n_files_with_text,
        "overlong": per_tier,
        "recommend": recommend,
        "truncated": truncated,
        "cancelled": cancelled,
    }


def decide_ctx(tier, info=None, cap=None):
    """按实测文本长度自动设定服务上下文：
    够短就用 4096 省内存/提速；按需要升档，但不超过该档上限。
    cap 可显式传入（自选档用用户设置）；否则取画像中的档位窗。

    自选档（custom）例外：上下文是用户自己选的，这里**不做收缩** ——
    用户选 8192 却被自动降到 4096，等于工具替用户改了他明确指定过的参数。
    """
    if cap is None:
        cap = TIER_PROFILES[tier]["ctx"]
    if tier == "custom":
        return cap
    if not info or not info.get("max_str"):
        return cap
    need_tokens = int(estimate_tokens("x" * info["max_str"]) * 2.2) + 512
    for ctx in (4096, 8192, 16384, 32768):
        if ctx >= cap:
            return cap
        if need_tokens <= ctx:
            return min(ctx, cap)
    return cap



def _is_word_char(ch):
    """术语两侧的"词字符"判定：西里尔/拉丁按词边界锚定，CJK 视为边界。

    为什么要区分（原实现只用正则 \\w，两者都没分开）：
      * 西里尔/拉丁文字有词的概念，术语两侧必须锚定词边界，否则
        "No" 会误伤 "Now"、"Кот" 会误伤 "Который"；
      * 中文没有词间空格，而 Python 的 \\w 把汉字也算词字符 —— 于是术语表里
        任何中文条目在中文文本里都会因为"左右都是词字符"而永不命中。
        把 CJK 当边界即可命中（如术语「神器」在「一个神器装备」中）。
    """
    if not (ch.isalnum() or ch == "_"):
        return False
    o = ord(ch)
    if (0x3400 <= o <= 0x9FFF
            or 0x3040 <= o <= 0x30FF
            or 0xAC00 <= o <= 0xD7AF):
        return False
    return True


class GlossaryIndex(object):
    """术语匹配索引：首字符分桶 + 同位置长词优先 + 非重叠单向扫描。

    为什么不用 "|".join(...) 的巨型交替正则：实测 8208 条术语拼出的
    204 KB 正则单条 150 字符文本要 43.70 ms，而同样的数据用朴素子串
    扫描只要 1.22 ms（慢 36 倍）；两万条量级的任务上就是几十分钟纯 CPU
    空转，GPU 全程在等。分桶后每个字符只与"以该字符开头"的词条比较，
    比较次数降两个数量级，匹配语义与原正则逐字符一致（有等价性属性测试）。

    v2 增补：
    * `variants`：拼写变体映射到规范词条，避免 ё/е 两个译名打架；
    * `case_sensitive`：人物名/技术缩写/小写单单词拉丁硬词要求首字母
      大小写一致（大写专名仍命中，句子里的普通名词不再误伤）；
    * 查找时统一做 ё→е 折叠，俄文里漏写两点的名字也能命中规范词条。
    """

    def __init__(self, mapping, case_sensitive=None, variants=None):
        raw = {}
        for k, v in (mapping or {}).items():
            name = str(k)
            if name.strip():
                raw[name] = str(v)
        self._case_sensitive = {str(k) for k in (case_sensitive or ())}
        var = {str(k): str(v) for k, v in (variants or {}).items()
               if str(k).strip() and str(v).strip()}

        # 变体只做别名，不单独成为规范条目：否则硬词过滤会拿到变体键，
        # 把“当前 MOD 专名”误判成软词。
        self._map = {}
        # 规范词条自身的译文优先；变体只在规范词条缺席时补位。
        for key, value in raw.items():
            if key not in var or var[key] == key:
                self._map[key] = value
        for key, value in raw.items():
            canon = var.get(key, key)
            if canon in raw:
                self._map.setdefault(canon, value)
            else:
                self._map.setdefault(key, value)
        self._alias = {}
        for key in raw:
            canon = var.get(key, key)
            if canon not in self._map:
                canon = key
            self._alias.setdefault(self._fold(key), set()).add(canon)

        self._by2 = {}
        self._by1 = {}
        for key in self._map:
            kl = self._fold(key)
            if len(key) == 1:
                t = self._by1.setdefault(kl, self._new_table())
            else:
                t = self._by2.setdefault(kl[:2], self._new_table())
            L = len(key)
            t["e"].setdefault(L, set()).add(key)
            for alias in self._alias.get(kl, (key,)):
                if len(alias) != L:
                    continue
                t["c"].setdefault(L, {}).setdefault(
                    self._fold(alias), []).append(key)
        for t in list(self._by1.values()) + list(self._by2.values()):
            t["lens"] = sorted(t["e"].keys(), reverse=True)

    @staticmethod
    def _new_table():
        return {"e": {}, "c": {}, "lens": []}

    @staticmethod
    def _fold(s):
        return s.casefold().replace("ё", "е")

    def _case_ok(self, seg, key):
        """大小写敏感词条：全大写缩写必须逐字一致，其余只要求首字母一致。

        首字母一致而不是全等，是为了让 `Гришачев`/`Гришачёв`、
        `Санёк`/`Санек` 这类拼写变体仍能命中；`crystal` 与 `Crystal`
        的首字母不同，正好被挡在正文普通名词之外。
        """
        if key not in self._case_sensitive:
            return True
        if key.isupper():
            return seg == key
        if key[:1].islower():
            # 小写单单词 canon（bar/crystal/star）只认大写专名：
            # 正文里的普通名词保持原样，交给模型按语境处理。
            return bool(seg) and seg[:1].isupper()
        return bool(seg) and bool(key) and seg[0] == key[0]

    def __len__(self):
        return len(self._map)

    def _probe(self, table, text, i, low):
        """按"位置升序 + 长词优先 + 同长度精确优先"取命中，返回 key 或 None。"""
        if not table:
            return None
        n = len(text)
        for L in table["lens"]:
            end = i + L
            if end > n:
                continue
            seg = text[i:end]
            key = None
            pool = table["e"].get(L)
            if pool and seg in pool:
                key = seg
            else:
                segl = low[i:end] if low is not None else self._fold(seg)
                cand = table["c"].get(L, {}).get(segl)
                if cand:
                    cand = list(dict.fromkeys(cand))
                    exact = [k for k in cand if k[:1] == seg[:1]]
                    if exact:
                        cand = exact
                    key = max(cand, key=lambda k: sum(
                        1 for a, b in zip(seg, k) if a == b))
            if key is None:
                continue
            if not self._case_ok(seg, key):
                continue
            if i and _is_word_char(text[i - 1]):
                continue
            if end < n and _is_word_char(text[end]):
                continue
            return key
        return None

    def find(self, text):
        """返回 [(start, end, key)]：按位置升序、同位置最长优先、互不重叠。"""
        out = []
        if not text or not self._map:
            return out
        n = len(text)
        low = self._fold(text)
        if len(low) != n:
            # 极少数字符 casefold 后长度变化（如 ß→ss）：退回逐段折叠，
            # 保证位置与原文一一对应。
            low = None
        i = 0
        while i < n:
            p2 = low[i:i + 2] if low is not None else self._fold(
                text[i:i + 2])
            key = self._probe(self._by2.get(p2), text, i, low)
            if key is None and self._by1:
                key = self._probe(self._by1.get(p2[:1]), text, i, low)
            if key is None:
                i += 1
                continue
            end = i + len(key)
            out.append((i, end, key))
            i = end
        return out

    def hits(self, text):
        """命中的 (原文, 译文)，长词优先 —— 与旧 _glossary_hits 同序。"""
        pairs = [(key, self._map[key]) for _, _, key in self.find(text)]
        pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
        return pairs



_DUP_FUNCTION_RE = re.compile(r"(有有|的的|了了|是是|在在)")


def clean_translation_output(text):
    """清掉模型常见的功能词重复（有有/的的/了了…），不动正常叠词。"""
    if not text:
        return text
    return _DUP_FUNCTION_RE.sub(lambda m: m.group(1)[0], text)

_STYLE_REPLACEMENTS = (
    ("我不得不说", "说实话"),
    ("就目前而言", "现在"),
    ("就目前来看", "现在看"),
    ("并且", "而且"),
    ("此外", "还有"),
    ("然而", "可"),
    ("因此", "所以"),
    ("进行了一次", "做了一次"),
    ("作出了", "做了"),
    ("给予了", "给了"),
)


def style_cleanup_cn(text):
    """对话专用：保守去掉翻译腔词和高频空格，不改术语、数字、代码。"""
    if not text:
        return text
    out = text
    for old, new in _STYLE_REPLACEMENTS:
        out = out.replace(old, new)
    # 中文之间的半角空格；中文与中文标点之间的空格。
    out = re.sub(r"(?<=[一-鿿])[ 	]+(?=[一-鿿])", "", out)
    out = re.sub(r"[ 	]+(?=[，。！？；：、])", "", out)
    out = re.sub(r"(?<=[，。！？；：、])[ 	]+", "", out)
    # 口语里常见的三个点统一成中文省略号（代码/占位符已在上层排除）。
    out = out.replace("...", "……")
    return out


# =====================================================================
# ★ 2.0.14 确定性后修复（零模型请求，通用型，不针对单一 MOD）
# =====================================================================

# D. 标点规范化：只在中文译文里动手，不动代码/占位符/英文上下文。
# 半角句读紧跟中文 → 全角；叠用标点收敛；「...」→「……」。
_PUNCT_HALF_TO_FULL = {",": "，", ";": "；", "!": "！", "?": "？", ":": "："}
# 连续感叹/问号：中文书面最多两个，超过收敛为两个。
_REPEATED_BANG_RE = re.compile(r"([！!?？])\1{2,}")


def normalize_cn_punctuation(text):
    """中文语境标点规范化（确定性）：半角句读转全角、叠用标点收敛。

    只在含中文的文本里做；用正则的上下文锚定避免误伤英文缩写、
    小数点和代码里的标点。不改术语/数字/占位符。
    """
    if not text or not has_cjk(text):
        return text, False
    out = text
    # 中文字符后紧跟的半角句读 → 全角（排除小数点：前面是数字时跳过）
    out = re.sub(r"(?<=[\u4e00-\u9fff]),", "，", out)
    out = re.sub(r"(?<=[\u4e00-\u9fff]);", "；", out)
    out = re.sub(r"(?<=[\u4e00-\u9fff])!", "！", out)
    out = re.sub(r"(?<=[\u4e00-\u9fff])\?", "？", out)
    # 省略号：连续 3 个及以上半角点 → 中文省略号（前面是数字的不动）
    out = re.sub(r"(?<![\d])\.{3,}(?![\d])", "……", out)
    # 半角点跟在中文后（句末）→ 句号
    out = re.sub(r"(?<=[\u4e00-\u9fff])\.(?=$|[^\dA-Za-z])", "。", out)
    # 叠用收敛：！！！→！！、？？？→？？
    prev = out
    out = _REPEATED_BANG_RE.sub(lambda m: m.group(1) * 2, out)
    # 已有全角省略号旁边多余的半角点（……. → ……）
    out = re.sub(r"…\.(?![.\d])", "…", out)
    # ★ 2.0.24（IWP 全量审计 F5 定量）：汉字之间的空格收敛。
    # 长文分段/模型风格会产出「强烈的 禁区 环境中」这类词间空格——
    # 中文没有词间空格，两侧都是汉字即可安全删除；占位符/色码/数字
    # 两侧必有一侧非汉字，不受影响（「12.3 米」「$start $mid」保留）。
    out = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", out)
    return out, out != prev or out != text


# C. 字面 \n 保真：源文里的字面转义 \n 个数，必须与译文一致。
# 注意：normalized/unescape 阶段字面 \n 已转成真实换行，这里比较的是
# escape_newlines 之后的结果（final），与源 text 的字面 \n 计数对齐。
def count_literal_newlines(text):
    """统计文本里字面 ``\\n`` 转义序列的个数（不是真实换行符）。"""
    return (text or "").count("\\n")


def reconcile_literal_newlines(src, out):
    """译文字面 \\n 数与源文不符时的确定性兜底：按源文数量补齐/收敛。

    模型常把字面 \n 吞成空格（$clr_1A\\n$clr_2B→空格），或凭空加 \n\\n。
    只做数量对齐：缺则按句末/颜色码边界补回，多则收敛到源数量。
    差异过大（源 0 → 译 >2）交给 context_bleed 闸门处理，这里不动。
    """
    if not src or not out:
        return out, False
    want = count_literal_newlines(src)
    have = count_literal_newlines(out)
    if want == have:
        return out, False
    # 源无字面换行却冒出多个：多为串段，交给上层闸门，不在数量层硬删。
    # ★ 2.0.25（aera 全量审计）：实测 11 条「源 0 → 译 2」漏过串段闸门
    # 原样落盘（dm_anomalies_12 等）——模型平白加的断行在游戏里把整句
    # 拆碎。少量（≤2 个）时确定性收敛为空格；更多仍交串段闸门点名。
    if want == 0:
        if 0 < have <= 2:
            out2 = out.replace("\\n", " ")
            while "  " in out2:
                out2 = out2.replace("  ", " ")
            return out2, True
        return out, False
    # 源有换行、译文全丢或少了：在颜色码切换/句末处补回缺失数量。
    if have < want:
        need = want - have
        # 优先在 $clr_N 颜色码切换处补（动态新闻常是颜色分段）
        def _add_color(m):
            nonlocal need
            if need > 0:
                need -= 1
                return "\\n" + m.group(0)
            return m.group(0)
        out2 = re.sub(r"\$clr_\d+", _add_color, out)
        if need > 0:
            # 其次在句末标点后补
            def _add_sentence(m):
                nonlocal need
                if need > 0:
                    need -= 1
                    return m.group(0) + "\\n"
                return m.group(0)
            out2 = re.sub(r"(?<=[。！？!?…])", _add_sentence, out2)
        if need > 0:
            out2 = "\\n" * need + out2
        return out2, out2 != out
    # have > want：收敛多余的换行（删最靠后的空行/连续换行）。
    # 注意：re.sub 的替换串是模板，"\\n" 会被解释成真实换行——
    # 必须用 lambda 返回字面 \n，否则收敛反而制造真换行。
    out2 = out
    while count_literal_newlines(out2) > want:
        out2 = re.sub(r"\\n\\n", lambda _m: "\\n", out2, count=1)
        if "\\n\\n" not in out2:
            break
    return out2, out2 != out


# B. 大数字/型号保真增强。
# 源里的「字母-数字型号串」（FNX-45 / MP-153 / P220 等）和年份区间
# 若被模型整体替换，属于型号篡改，比数字缺失更误导玩家。
_MODEL_TOKEN_RE = re.compile(r"[A-Za-z]{2,}-?\d{2,4}[A-Za-z]?")
_YEAR_RANGE_RE = re.compile(r"\b(\d{4})\s*[-‐‑‒–—]\s*(\d{4})\b")


def missing_model_tokens(src, out):
    """返回源文里「明确型号串」在译文中消失的列表（型号被换掉）。

    只取带引号或紧邻武器/物品语义的型号，避免把普通编号当型号。
    译文里以「译名 + 原型号」形式出现时算保留。
    """
    if not src or not out:
        return []
    missing = []
    seen = set()
    # ★ 2.0.21：色码 %c[r,g,b,a] 内数字不是型号，先剥离再扫（同
    # missing_big_numbers 的色码豁免口径）。
    src = re.sub(r"%c\[[^\]]*\]", " ", str(src))
    for m in _MODEL_TOKEN_RE.finditer(src):
        tok = m.group(0).strip()
        if tok in seen or len(tok) < 4:
            continue
        seen.add(tok)
        # 年份区间不算型号
        if re.fullmatch(r"\d+", tok):
            continue
        # ★ 2.0.24（IWP 全量审计 D2）：英文年代短语不是型号——实测
        # "mid-1990s" 被当型号，译文「1990年代中期」判缺失补出注记。
        if re.fullmatch(r"(?:early|mid|late)[-\s]?\d{4}s?", tok, re.I):
            continue
        # 型号需紧邻引号或武器语境词，减少误报。
        # ★ 2.0.18：语境窗口从 ±24 放宽到 ±48 + 全源兜底——实测武器描述
        # （"derivative of the AK-100 series from the AK-74M, chambered
        # for the M43 cartridge"）型号与语境词相距很远，窄窗口漏掉
        # AK-100→AK-74M 的型号替换。
        s, e = m.span()
        near = src[max(0, s - 48):min(len(src), e + 48)]
        if not (any(q in near for q in "\"'«“”")
                or re.search(r"rifle|pistol|gun|shotgun|carbine|weapon|ammo|"
                             r"cartridge|sniper|caliber|series|barrel|"
                             r"караб|пистол| оружи|патрон",
                             near if near else src, re.I)):
            continue
        if tok not in out:
            missing.append(tok)
    return missing


class TranslationJob(object):
    # 场景上下文/风格提示必须是线程局部的：多路并发时各 worker 不能互相覆盖。
    @property
    def _ctx_hint(self):
        return getattr(self._tls, "ctx", "")

    @_ctx_hint.setter
    def _ctx_hint(self, value):
        self._tls.ctx = value or ""

    @property
    def _style_hint(self):
        return getattr(self._tls, "style", "dialogue")

    @_style_hint.setter
    def _style_hint(self, value):
        self._tls.style = value or "dialogue"

    def __init__(self, parser, server, in_dir, mode, src_lang, tgt_lang,
                 reuse_dir=None, glossary=None, output_anyway=False,
                 force_glossary=True, ctx_size=8192, out_dir=None,
                 log=None, detail=None, progress=None, cancel_event=None,
                 reference_mode=False, copy_reference=True,
                 only_referenced=False, protect_proper=True,
                 preview_reference=False, hard_glossary_keys=None,
                 translate_literals=False, auto_session_terms=False,
                 session_cache_dir=None, tm_lite=True, parallel=1,
                 pending_files=0, freq_names=True):
        self.p = parser
        self.srv = server
        self.in_dir = in_dir
        self.mode = mode
        self.src_lang = src_lang
        self.tgt = tgt_lang
        self.reuse_dir = reuse_dir
        self.glossary = dict(glossary or {})
        term_layers = load_term_layers()
        self.gloss_names = set(term_layers["names"])
        # 百科/任务/界面/新闻只做软提示：精确命中才注入，不强制替换。
        self.gloss_soft = set(term_layers.get("reference") or ())
        self.gloss_variants = dict(term_layers["variants"])
        self.gloss_case_sensitive = set(term_layers["case_sensitive"])
        self.gloss_quote_required = set(
            term_layers.get("quote_required") or ())
        # 技术精确表只做整条命中；不进入硬词/提示层。
        for _k, _v in load_technical_exact().items():
            self.glossary.setdefault(_k, _v)
        self.output_anyway = output_anyway
        self.force_glossary = force_glossary
        self._tls = threading.local()
        self.dialogue_temp = 0.1
        self.batch_stats = {"batches": 0, "lines": 0, "failed": 0}
        self.ctx_size = int(ctx_size)
        # ★ 2.0.2：路数上限从写死的 4 放宽到 PARALLEL_CAP（与 LlamaServer 同步）。
        self.parallel = max(1, min(PARALLEL_CAP, int(parallel)))
        # 多路并发时所有 worker 共享计数/缓存/账本，写操作统一加锁。
        self._stat_lock = threading.Lock()
        self._ledger_lock = threading.RLock()
        self.safe_chars = safe_chunk_chars(self.ctx_size)
        self.long_chunks = 0
        # ID 引用层模式：文本正文在 configs/text 的条目里，XML/脚本只放 ID。
        # 这里只更新条目正文、原样复制引用层，不改写任何 <text>ID</text>。
        self.reference_mode = bool(reference_mode)
        self.copy_reference = bool(copy_reference)
        self.only_referenced = bool(only_referenced)
        self.protect_proper = bool(protect_proper)
        self.preview_reference = bool(preview_reference)
        self._table_entries = {}     # 绝对路径 -> {id: entity}
        self._id_files = {}          # id -> [绝对路径, ...]
        # ★ 2.0.2 终检：本次**成功改写过**的文本表（rel 集合）。真正的改写
        #   收口只有 _write_table_changes / _write_table_as_utf8 两个出口，
        #   在那里登记，终检只验这几张（原样复制的不用验 XML 结构）。
        self._audit_tables = set()
        self._ref_counts = {}        # id -> 引用次数
        self._ref_files = set()      # 含可解析引用的 XML/脚本
        self._unresolved = {}        # id -> 次数（索引里找不到）
        self._localization = {"language": "", "files": []}
        self._lang_hint = ""
        self.log = log or (lambda m: None)
        self.detail = detail or (lambda m: None)
        self.progress = progress or (lambda d: None)
        self.cancel = cancel_event
        # 自动识别出的「整个 MOD 待翻译文件总数」，用于界面计数显示
        self.pending_files = int(pending_files or 0)
        self.req_count = 0
        self.trans_chars = 0
        # ★ 2.0.3 规则违规遥测：kind -> {hits, retried_ok, fixed,
        #   kept_first, kept_source}。收尾输出「提示词体检」报告，
        #   为提示词迭代提供数据（哪条规则被违反最多）。
        self._audit_stats = {}
        self.redundant = []
        self.failed_strings = 0
        self.empty_files = 0
        self._past = set()
        self._temp_clock = 0
        self.out_dir = out_dir or os.path.join(in_dir, "translated_hy_mt2")
        self._excluded_paths = set()
        self.gloss = self._new_gloss_index()
        # None = 兼容旧 API/测试：所有术语都可用；set = 只在这些硬词上
        # 强制/提示（UI 传入 termbase 的 canon 层 + 用户自定义层）。
        self.gloss_hard = (set(hard_glossary_keys)
                           if hard_glossary_keys is not None else None)
        self.translate_literals = bool(translate_literals)
        self.auto_session_terms = bool(auto_session_terms)
        self.auto_freq_names = bool(freq_names)
        self.freq_names = {}
        self.freq_stats = {"candidates": 0, "cached": 0,
                           "translated": 0, "failed": 0}
        self.session_cache_dir = session_cache_dir or os.path.join(
            _DATA_ROOT, "session_terms")
        self.session_keys = set()
        # 会话词条只做提示，不参与“整条精确替换”；否则模型生成的坏译名
        # 会绕过 protect_proper 直接写进术语表。
        self.session_only_keys = set()
        self.session_stats = {}
        self.session_requests = 0
        # TM-lite：高重复批次内精确复用；缓存只在本次任务内存里。
        self.tm_lite = bool(tm_lite)
        self.tm_stats = {"scope": "off", "total": 0, "unique": 0,
                         "ratio": 0.0, "hits": 0, "saved_chars": 0}
        self._tm_cache = {}
        self._tm_global_enabled = False
        self._tm_local_enabled = False
        # 完成账本：记录"每个输入文件的指纹 + 输出大小"，取代"输出文件存在就算
        # 译完"。旧做法在两种真实场景下会静默漏译：①游戏更新给 XML 加了新条目，
        # ②ltx/cfg 上次中断留下半截输出 —— 两者都会被"存在即跳过"永久保留。
        self._ledger_path = None
        self._ledger = {}
        self._ledger_present = False
        self._ledger_saved = 0.0

    def _log_progress(self, idx, total, rel):
        """每 50 个文件记一条 GUI 进度；逐文件明细不再进界面日志。

        实测日志：6122 个文件时逐文件 log 产生近 5000 行、局部 3000 行/秒，
        而界面每条日志整块重绘 400 行 —— 主线程被占满，窗口像卡死。
        明细保留在运行日志里（detail），界面只留可读的进度。
        """
        if idx == 1 or idx == total or idx % 50 == 0:
            self.log("进度 %d/%d（%.0f%%）：%s"
                     % (idx, total, 100.0 * idx / max(total, 1), rel))

    def _cancelled(self):
        return self.cancel is not None and self.cancel.is_set()

    def _parse(self, fn, *args):
        """调用 vendor 解析器，并把它的 print 诊断转进详细日志。

        解析器会逐文件打印"用了哪个编码解码"—— 那正是编码猜错时唯一的
        线索，而 GUI 子系统下 stdout 为 None，不接过来就等于没有。

        同时记录单文件解析耗时：vendor 的 cfgxml 正则曾在一类 <texture>
        文件上灾难性回溯，一卡就是几十秒且持有 GIL（整个窗口无响应）。
        修好后仍保留这条"慢文件"证据，下次换别的语料再遇到就能直接定位。
        """
        t0 = time.time()
        with capture_stdout() as cap:
            result = fn(*args)
        elapsed = time.time() - t0
        for line in cap.lines():
            self.detail("  " + line)
        if elapsed >= 2.0:
            name = os.path.basename(str(args[0])) if args else "?"
            self.detail("  解析耗时 %.1f 秒：%s" % (elapsed, name))
            if elapsed >= 10.0:
                self.log("解析耗时异常（%.0f 秒）：%s；如再次出现请反馈该文件"
                         % (elapsed, name), "warn")
        return result

    def _warn_mojibake(self, rel, samples):
        """单字节解码结果疑似乱码时告警（不自动改判，交人工决定）。"""
        for text in samples:
            if looks_like_mojibake(text):
                self.log("⚠ %s 疑为编码误判（乱码特征）：请用「编码转换」页"
                         "统一转成 UTF-8 后再翻译，否则译文不可用" % rel, "warn")
                return

    def _read_existing(self, path):
        """递归读取目录内全部文本 XML 的已有译文（复用/断点）。"""
        reuse = {}
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if not fn.lower().endswith(".xml"):
                    continue
                full = os.path.join(root, fn)
                try:
                    ents = self.p.parse_xray_text_xml(
                        full, read_encoding_chain(full))
                except Exception as e:
                    self.detail("  复用文件跳过：%s（%s: %s）"
                                % (fn, type(e).__name__, e))
                    continue
                for ent in ents:
                    rec = self.p.getRecommendLangText(ent, self.tgt)[1]
                    if rec:
                        reuse[ent.id] = rec
        return reuse

    def _looks_like_target(self, text):
        """目标语言为中文时，汉字占**正文**比例高即认为已经是中文。

        不能把 `$level`、`%s`、`$$T1$$` 里的拉丁字母算进分母：实测
        `$level $kill_seen_z，如同一个潜行者...` 的汉字占比会被压到 0.3
        以下，已汉化条目被再次送翻，模型把变量名也译成 `$地点`。
        先剔除占位符、XML 标签，再按剩下的字母计算。
        """
        if self.tgt != "chs":
            return False
        clean = _RUNTIME_TOKEN_RE.sub("", text or "")
        clean = re.sub(r"<[^>]+>", "", clean)
        letters = [c for c in clean if c.isalpha()]
        if not letters:
            return False
        han = sum(1 for c in letters if "\u4e00" <= c <= "\u9fff")
        # 0.1 而不是 0.3：`测试Taurus Raging Bull左轮手枪` 这类“中文 + 品牌”
        # 已经是译文，0.3 会把品牌名那点拉丁字母算成“不是中文”，又翻一遍。
        return han / len(letters) > 0.1

    def _new_gloss_index(self):
        if not self.glossary:
            return None
        return GlossaryIndex(
            self.glossary, case_sensitive=self.gloss_case_sensitive,
            variants=self.gloss_variants)

    def _span_context_ok(self, text, start, end, key):
        """阵营常见词（долг/свобода/монолит）只认引号内的专名用法。

        `долг` 本义是债务/责任，`свобода` 是自由，`монолит` 是整块石料；
        只有 `"Долга"` 这种引号内的组织名才强制替换，避免把普通名词
        硬改成“纪律团/自由团/黑石”。
        """
        if key not in self.gloss_quote_required:
            return True
        return self._inside_quotes(text, start, end)

    @staticmethod
    def _inside_quotes(text, start, end):
        """词条是否位于任意引号对内，而不是只贴着引号。

        原文常见 `"Броня Долга"`、`"база Свободы"` 这种引号包住的多词
        名称：`Долга/Свободы` 不紧贴引号，但仍在引号语境里，应算专名。
        """
        for op, cl in (("«", "»"), ("“", "”"), ("‘", "’")):
            if (text.rfind(op, 0, start) > text.rfind(cl, 0, start)
                    and text.find(cl, end) != -1):
                return True
        for q in ('"', "'"):
            if text.count(q, 0, start) % 2 == 1 and text.find(q, end) != -1:
                return True
        return False

    def _glossary_hits(self, text):
        """命中的硬术语 + 人物软提示，用于提示块；普通词不注入。

        canon 层与本次 MOD 会话词在前，人物层只作末尾软提示（不强制替换）。
        人物层按首字母大小写匹配，正文里的 доктор/bar 不会误命中专名。
        """
        if self.gloss is None:
            return []
        spans = self.gloss.find(text)
        pairs = [(key, self.gloss._map[key]) for start, end, key in spans
                 if self._span_context_ok(text, start, end, key)]
        if self.gloss_hard is None:
            return pairs
        hard = [(k, v) for k, v in pairs
                if k in self.gloss_hard or k in self.session_keys]
        used = {k for k, _v in hard}
        names = [(k, v) for k, v in pairs
                 if k in self.gloss_names and k not in used]
        used |= {k for k, _v in names}
        # 软层只提示不强制；长句/过长的百科条目会挤占 240 字符预算，
        # 直接跳过，让硬词和短专名优先。
        soft = [(k, v) for k, v in pairs
                if k in self.gloss_soft and k not in used
                and len(k) <= 32]
        return hard + names + soft

    def _glossary_spans(self, text):
        """命中的硬术语 (start, end, key)，用于强制/切片。"""
        if self.gloss is None:
            return []
        spans = self.gloss.find(text)
        spans = [h for h in spans
                 if self._span_context_ok(text, h[0], h[1], h[2])]
        if self.gloss_hard is None:
            return spans
        return [h for h in spans if h[2] in self.gloss_hard]

    # ── TM-lite：高重复批次内的精确复用 ────────────────────────────
    @staticmethod
    def _tm_norm(text):
        """归一化只做空白/换行整理，保留大小写与标点——精确复用不猜语义。"""
        s = unescape_newlines(str(text or "")).strip()
        return re.sub(r"[ \t]+", " ", s)

    def _tm_key(self, text, lang):
        src = self._tm_norm(text)
        sig = (tuple(sorted(_RUNTIME_TOKEN_RE.findall(src))),
               src.count("\n"))
        return (str(lang), len(src), sig, src)

    def _tm_measure(self, values):
        seen = set()
        total = 0
        for value in values:
            src = self._tm_norm(value)
            if len(src) < 4:
                continue
            total += 1
            seen.add(src)
        unique = len(seen)
        return {"total": total, "unique": unique,
                "ratio": (1.0 - float(unique) / total) if total else 0.0}

    def _tm_should_enable(self, stats):
        total = int(stats.get("total") or 0)
        unique = int(stats.get("unique") or 0)
        ratio = stats.get("ratio")
        if ratio is None:
            ratio = (1.0 - float(unique) / total) if total else 0.0
        return (self.tm_lite and total >= TM_LITE_MIN_ENTRIES
                and ratio >= TM_LITE_MIN_RATIO)

    def _tm_enable_global(self, values):
        """任务级门控：重复率高才跨表复用；否则完全走原路径。"""
        if not self.tm_lite:
            return False
        stats = self._tm_measure(values)
        self.tm_stats.update(stats)
        if self._tm_should_enable(stats):
            self._tm_global_enabled = True
            self.tm_stats["scope"] = "global"
            self.log("TM-lite：本批重复率 %.1f%%（%d/%d），启用跨表精确复用"
                     % (stats["ratio"] * 100, stats["unique"],
                        stats["total"]), "dim")
            return True
        self.log("TM-lite：未启用（重复率 %.1f%%，门槛 %.0f%%；"
                 "不改变翻译路径）"
                 % (stats["ratio"] * 100, TM_LITE_MIN_RATIO * 100), "dim")
        return False

    def _tm_enable_table(self, values, rel):
        """表级门控：任务级没启用时，只在高重复表内复用。"""
        self._tm_local_enabled = False
        if not self.tm_lite or self._tm_global_enabled:
            return
        stats = self._tm_measure(values)
        if not self._tm_should_enable(stats):
            return
        self._tm_local_enabled = True
        self._tm_cache = {}
        self.tm_stats.update(stats)
        self.tm_stats["scope"] = "table"
        self.log("TM-lite：%s 重复率 %.1f%%（%d/%d），表内精确复用"
                 % (rel, stats["ratio"] * 100, stats["unique"],
                    stats["total"]), "dim")

    def _tm_lookup(self, text, lang):
        if not (self._tm_global_enabled or self._tm_local_enabled):
            return None
        return self._tm_cache.get(self._tm_key(text, lang))

    def _tm_store(self, text, lang, out):
        if not (self._tm_global_enabled or self._tm_local_enabled):
            return
        src = self._tm_norm(text)
        if len(src) < 4 or not out or out == text:
            return
        if self.tgt == "chs" and not has_cjk(out):
            return
        self._tm_cache.setdefault(self._tm_key(text, lang), out)

    def _translate_chunk(self, content, lang, hints, depth=0, note=""):
        """翻译单个句段：超出该档安全长度时按句切分逐段翻译并拼回，
        保证不超长、不截断（长文质量保护）。

        depth 用于处理"模型输出被 max_tokens 截断"：收到 OutputTruncated
        就把该段一分为二重译，最多递归 3 层（宁可多切几刀，也不能把半句
        译文写进汉化包）。"""
        if len(content) <= self.safe_chars:
            try:
                return self.srv.translate(
                    content, lang, self.tgt, glossary_hits=hints,
                    extra_sys=note or None,
                    context=getattr(self, "_ctx_hint", "") or None,
                    style=getattr(self, "_style_hint", "dialogue"),
                    temperature=self._temp_for_style())
            except OutputTruncated:
                if depth >= 3 or len(content) < 8:
                    self.log("  输出被截断且已无法再切分（%d 字符），"
                             "建议改用更大上下文档位" % len(content), "warn")
                    raise
                self.log("  输出被截断，按句再切一次重译（%d 字符）"
                         % len(content), "warn")
                return "".join(
                    self._translate_chunk(seg, lang, hints, depth + 1, note)
                    for seg in split_long_text(
                        content, max(4, len(content) // 2)))
        parts = split_protected(content, self.safe_chars, _ANY_PLACEHOLDER_RE)
        if len(parts) > 1:
            with self._stat_lock:
                self.long_chunks += max(0, len(parts) - 1)
            self.log("  长文本 %d 字符 → 自动切为 %d 段翻译（%s 上下文窗）"
                     % (len(content), len(parts), self.ctx_size))
        return "".join(
            self._translate_chunk(seg, lang, hints, depth + 1, note)
            for seg in parts)

    def _translate_segments(self, text, lang, note=""):
        """整条翻译；占位符在 `_translate_one` 里已 mask，不能在这里按
        换行/`$$ACTION$$` 再切碎。

        旧实现调用 `cutTextWithSeparator`：它把 `\\n` 当分隔符，多行描述被
        拆成一段段单独翻译，实测 8 条藏匿点描述发了 56 次请求，行与行之间
        失去上下文。这里只保留“整条精确命中术语”的快捷路径，其余交给
        `_translate_chunk`（超长时仍按句切分，保留占位符）。
        """
        if not text.strip():
            return text
        stripped = text.strip()
        if (not self.force_glossary and stripped in self.glossary
                and stripped not in self.session_only_keys):
            with self._stat_lock:
                self.req_count += 1
            return text.replace(stripped, self.glossary[stripped])
        with self._stat_lock:
            self.req_count += 1
            self.trans_chars += len(text)
        hints = None if self.force_glossary else self._glossary_hits(text)
        return self._translate_chunk(text, lang, hints, note=note)

    def _temp_for_style(self):
        if self._style_hint == "label":
            return 0.0
        if self._style_hint == "formal":
            return 0.1
        return self.dialogue_temp

    def _rule_hit(self, kind, outcome):
        """规则违规遥测：kind=规则名，outcome=retried_ok/fixed/
        kept_first/kept_source。线程安全（复用 _stat_lock）。"""
        with self._stat_lock:
            s = self._audit_stats.setdefault(kind, {})
            s["hits"] = s.get("hits", 0) + 1
            s[outcome] = s.get(outcome, 0) + 1

    def _audit_report(self):
        """任务收尾的「提示词体检」：按触发次数排序点名最高频违规规则。
        零违规只落明细日志（GUI 日志风暴防线，T29），有违规才上界面。"""
        if not self._audit_stats:
            self.detail("提示词体检：全部规则零违规，内置提示词与规则约束有效")
            return
        ranked = sorted(self._audit_stats.items(),
                        key=lambda kv: -kv[1].get("hits", 0))
        labels = (("retried_ok", "重试成功"), ("fixed", "自动修复"),
                  ("kept_first", "保留首译"), ("kept_source", "保留原文"))
        parts = []
        for kind, s in ranked:
            bits = ["%d 次" % s.get("hits", 0)]
            bits += ["%s %d" % (lab, s[oc]) for oc, lab in labels
                     if s.get(oc)]
            parts.append("%s（%s）" % (kind, " / ".join(bits)))
        self.log("提示词体检：" + "；".join(parts))
        self.log("最需要关注的规则：%s（触发最多，优先迭代）" % ranked[0][0],
                 "warn")

    def _translate_one(self, text, lang, context="", style="dialogue"):
        """翻译一个文本串；运行期占位符先 mask，译后校验并回填。

        校验失败（模型改了/丢了 `$name`、`%s`、`\n` 等）时**保留原文**，
        绝不把坏译文写进文本表。这是硬约束，不依赖提示词。
        """
        # ★ 2.0.8 源头减险：极短条目（<12 字符，如 "Я не готов."）不注入
        # 场景上下文。这类条目几乎没有正文压住 prompt 前面的「前一句：…」，
        # 1.8B 小模型最容易把上下文整段当正文翻出（实测 bar_arena_man_job_172
        # 被上一条的长译文覆盖）。阈值只取最危险的极短串，正常短对话仍保留
        # 上下文帮助消歧；其余污染由下面的 bleed 检测+去上下文重试兜住。
        self._ctx_hint = "" if len((text or "").strip()) < 12 \
            else (context or "")
        self._style_hint = style or "dialogue"
        if lang in ("auto", "text"):
            lang = detect_text_lang(text) or self._lang_hint or "rus"
        elif lang in ("rus", "eng"):
            det = detect_text_lang(text)
            if det and det != lang:
                lang = det
        if lang == self.tgt:
            return text
        hit = self._tm_lookup(text, lang)
        if hit is not None:
            with self._stat_lock:
                self.tm_stats["hits"] += 1
                self.tm_stats["saved_chars"] += len(text)
            self.detail("  TM-lite 精确复用：%s" % str(text)[:60])
            return hit
        normalized = unescape_newlines(text)
        masked, mapping = mask_runtime_tokens(normalized)
        if not mapping:
            out = self._translate_plain(normalized, lang)
        else:
            out = self._translate_plain(masked, lang)
            restored = restore_runtime_tokens(out, mapping)
            if restored is None:
                # 颜色控制符（%c[...]）在带术语提示的请求里偶发被模型
                # 吞掉；先按“逐字保留控制符”重试一次，仍失败才保留原文。
                try:
                    retry_raw = self._translate_plain(
                        masked, lang, note=_PLACEHOLDER_RETRY_NOTE)
                    retry = restore_runtime_tokens(retry_raw, mapping)
                except Exception:
                    retry = None
                if retry is None:
                    # ★ 2.0.17 第三级兜底：切短块逐块译（占位符用
                    # split_protected 保护不被切断），与完整性闸门同一
                    # 模式。实测动态新闻/物品描述里含 $clr_N、%c[...]、
                    # %s 的条目模型最容易破坏占位符，旧版在这里整条
                    # 保留英文——切片能救回大多数。切片也失败才保留原文。
                    sliced = None
                    try:
                        raw = self._force_sliced(masked, lang,
                                                 note=_PLACEHOLDER_RETRY_NOTE)
                        sliced = restore_runtime_tokens(raw, mapping) \
                            if raw is not None else None
                    except Exception:
                        sliced = None
                    if sliced is not None:
                        self.detail("  运行占位符被破坏，切片翻译成功：%s"
                                    % str(text)[:50])
                        self._rule_hit("占位符保留", "sliced_ok")
                        out = sliced
                    else:
                        self.detail("  运行占位符未原样保留，保留原文：%s"
                                    % str(text)[:60])
                        self._rule_hit("占位符保留", "kept_source")
                        return text
                else:
                    self.detail("  控制符未保留，已按逐字保留要求重试：%s"
                                % str(text)[:50])
                    self._rule_hit("占位符保留", "retried_ok")
                    out = retry
            else:
                out = restored
        if looks_like_prompt_echo(out):
            self.detail("  模型回显提示词，保留原文：%s" % str(text)[:60])
            self._rule_hit("提示词回显", "kept_source")
            return text
        # ★ 2.0.8 上下文串入闸门：模型把「前一句/后一句」上下文当正文翻出，
        # 或极短源文吐出超长段落（上一条译文）。这些标签/段落绝不属于本条
        # 译文。去掉上下文重译一次（清 _ctx_hint，否则重试仍带同一上下文）；
        # 仍异常则保留原文，绝不把相邻条目的译文写进本条。
        if looks_like_context_bleed(normalized, out):
            self.detail("  上下文串入译文，去掉上下文重译：%s"
                        % str(text)[:60])
            saved_ctx = self._ctx_hint
            self._ctx_hint = ""
            try:
                if mapping:
                    retry_raw = self._translate_plain(masked, lang)
                    retry_ctx = restore_runtime_tokens(retry_raw, mapping)
                else:
                    retry_ctx = self._translate_plain(normalized, lang)
            except Exception:
                retry_ctx = None
            if retry_ctx and not looks_like_prompt_echo(retry_ctx) \
                    and not looks_like_context_bleed(normalized, retry_ctx):
                self.detail("  去上下文重译成功：%s" % str(text)[:50])
                self._rule_hit("上下文串入", "retried_ok")
                out = retry_ctx
            else:
                # ★ 2.0.25（aera 全量审计 D12 签名②）：去上下文重译仍串入
                # 时不再直接保留原文——先试切片强译（实测 camelbak 描述
                # 341 字符因此整条漏译）。切片结果同样过串入检测。
                sliced_ctx = None
                try:
                    raw = self._force_sliced(masked, lang)
                    sliced_ctx = restore_runtime_tokens(raw, mapping) \
                        if (raw is not None and mapping) else raw
                except Exception:
                    sliced_ctx = None
                if (sliced_ctx and not looks_like_prompt_echo(sliced_ctx)
                        and has_cjk(sliced_ctx)
                        and not looks_like_context_bleed(normalized,
                                                         sliced_ctx)):
                    self.detail("  去上下文重译仍串入，切片强译成功：%s"
                                % str(text)[:50])
                    self._rule_hit("上下文串入", "sliced_ok")
                    out = sliced_ctx
                else:
                    self.detail("  去上下文重译仍异常，保留原文：%s"
                                % str(text)[:60])
                    self._rule_hit("上下文串入", "kept_source")
                    return text
        # 残留源词确定性替换：模型把专名原样保留时，直接按词库换掉；
        # 不调用模型，也不改动句子其它部分。替换后再看是否还残留俄文。
        out = self._repair_residual_source_terms(normalized, out)
        out = self._repair_residual_latin_names(normalized, out)
        out = self._repair_known_semantic_terms(normalized, out, lang)
        if self._style_hint == "dialogue" and has_cjk(out):
            out = style_cleanup_cn(out)
        # 多行保真：模型偶尔把整段合并成一行；带明确数量再试一次。
        source_nl = normalized.count("\n")
        if source_nl and out.count("\n") != source_nl:
            note = ("原文有 %d 个换行，必须输出 %d 个换行，逐行对应；"
                    "不得合并段落，也不得多加空行。" % (source_nl, source_nl))
            try:
                if mapping:
                    retry_raw = self._translate_plain(
                        masked, lang, note=note)
                    retry = restore_runtime_tokens(retry_raw, mapping)
                else:
                    retry = self._translate_plain(
                        normalized, lang, note=note)
            except Exception:
                retry = None
            if retry and not looks_like_prompt_echo(retry) \
                    and retry.count("\n") == source_nl:
                self.detail("  换行数不符，已按原文段数重试：%s"
                            % str(text)[:50])
                self._rule_hit("换行保真", "retried_ok")
                out = retry
            else:
                fixed = restore_linebreaks(out, source_nl)
                if fixed != out:
                    self.detail("  换行数不符（%d→%d），按句末标点补回"
                                % (source_nl, out.count("\n")))
                    self._rule_hit("换行保真", "fixed")
                    out = fixed
                else:
                    self.detail("  换行数与原文不一致（%d→%d），保留首次译文"
                                % (source_nl, out.count("\n")))
                    self._rule_hit("换行保真", "kept_first")
        # ★ 2.0.3 长度比闸门：原文较长而译文长度比异常，多半是漏译
        # （模型只翻了前半句）或过度扩写。带针对性要求重试一次；
        # 仍异常则保留首次译文（漏译的译文仍比俄文原文强，不回退原文）。
        # 短文本（<40 字符或可译词 <4）噪声大，不检；短标签不检。
        #
        # ★ 2.0.7 下限按语言对：俄→中天然压缩（西里尔 2 字节/字符、中文
        # 信息密度高），实测bio表 0.24~0.35 全是正确译文；旧下限 0.30
        # 把 77 条正确译文判成漏译、各白跑一次重试。英→中类似。
        # 同语系/中→外维持 0.30。上限 2.0 不变（扩写仍要抓）。
        _to_zh = self.tgt in ("zh", "chs", "zh-CN", "zh-Hans", "简体中文",
                              "中文", "chs")
        _low_ratio = 0.15 if (_to_zh and lang in ("rus", "eng", "ukr", "auto",
                                                  "text")) else 0.30
        if (self._style_hint != "label" and len(normalized.strip()) >= 40
                and len(re.findall(r"[а-яёА-ЯЁ]{2,}|[A-Za-z]{2,}",
                                   normalized)) >= 4):
            src_len = max(1, len(normalized.strip()))
            ratio = len(out.strip()) / src_len
            # 漏译的真正特征：译文短到几乎没内容（<12 字符），无论语言对
            # 都要抓；否则用语言对下限。
            if ratio < _low_ratio and len(out.strip()) < 12 \
                    or not (_low_ratio <= ratio <= 2.0):
                note = ("译文长度与原文严重不符，疑似漏译或过度扩写；"
                        "逐句对照原文完整翻译，不增译、不漏译、不概括。")
                try:
                    if mapping:
                        retry_raw = self._translate_plain(
                            masked, lang, note=note)
                        retry = restore_runtime_tokens(retry_raw, mapping)
                    else:
                        retry = self._translate_plain(
                            normalized, lang, note=note)
                except Exception:
                    retry = None
                ok_ratio = False
                if retry and not looks_like_prompt_echo(retry) \
                        and _low_ratio <= len(retry.strip()) / src_len <= 2.0:
                    ok_ratio = True
                if ok_ratio:
                    self.detail("  译文长度比异常（%.2f），已按完整对照重试：%s"
                                % (ratio, str(text)[:50]))
                    self._rule_hit("长度比（漏译/扩写）", "retried_ok")
                    out = retry
                else:
                    self.detail("  译文长度比异常（%.2f），重试未恢复，"
                                "保留首次译文：%s"
                                % (ratio, str(text)[:50]))
                    self._rule_hit("长度比（漏译/扩写）", "kept_first")
        # ★ 2.0.9 数字保真闸门：源文的大额数字（金额/年份/坐标/密码）必须在
        # 译文中原样出现（含千分位或中文万/千计数）。实测源文 30000 卢布被
        # 译成 15,000，报酬腰斩。带“逐字保留数字”要求重试一次；仍缺则保留
        # 原文——错误的金额比未翻译的俄文更有害，原文至少不会误导玩家。
        if _to_zh:
            _missed = missing_big_numbers(normalized, out)
            if _missed:
                self.detail("  译文大额数字缺失/被改（%s），按逐字保留重试：%s"
                            % (",".join(_missed[:4]), str(text)[:50]))
                note = ("原文中的数字（尤其是金额、年份、坐标、密码）必须"
                        "逐字保留，不得换算、四舍五入或改写；"
                        "例如 30000 就写 30000，不要写成其它数字。")
                try:
                    if mapping:
                        retry_raw = self._translate_plain(
                            masked, lang, note=note)
                        retry_num = restore_runtime_tokens(retry_raw, mapping)
                    else:
                        retry_num = self._translate_plain(normalized, lang,
                                                          note=note)
                except Exception:
                    retry_num = None
                still = missing_big_numbers(normalized, retry_num) \
                    if retry_num else _missed
                if retry_num and not looks_like_prompt_echo(retry_num) \
                        and not still:
                    self.detail("  数字保真重试成功：%s" % str(text)[:50])
                    self._rule_hit("数字保真", "retried_ok")
                    out = retry_num
                else:
                    # ★ 2.0.12：不再整段回退原文。重试仍缺数字时，保留已
                    # 译好的中文正文，仅把缺失数字按原值补进去——一整段
                    # 看不懂的外文比一个数字缺失严重得多。仅当译文根本
                    # 没有中文（完全没翻）才回退原文。
                    patched = patch_missing_numbers(
                        normalized, out,
                        skip=missing_model_tokens(normalized, out))
                    if patched and patched != out:
                        self.detail("  数字保真重试仍缺（%s），已补数字保留译文：%s"
                                    % (",".join(still[:4]), str(text)[:50]))
                        self._rule_hit("数字保真", "fixed")
                        out = patched
                    else:
                        self.detail("  数字保真重试仍缺失（%s），保留原文：%s"
                                    % (",".join(still[:4]), str(text)[:50]))
                        self._rule_hit("数字保真", "kept_source")
                        return text
        # 粗口保真闸门：原文有脏话/辱骂/威胁，译文却完全软化时，带
        # “保留同等力度”的补充要求重试一次；仍软化则保留第一次结果。
        if has_source_profanity(normalized) and not has_zh_profanity(out):
            try:
                if mapping:
                    retry_raw = self._translate_plain(
                        masked, lang, note=_PROFANITY_RETRY_NOTE)
                    retry = restore_runtime_tokens(retry_raw, mapping)
                else:
                    retry = self._translate_plain(
                        normalized, lang, note=_PROFANITY_RETRY_NOTE)
            except Exception:
                retry = None
            if retry and not looks_like_prompt_echo(retry) \
                    and has_zh_profanity(retry):
                self.detail("  粗口被软化，已按原文力度重试：%s"
                            % str(text)[:50])
                self._rule_hit("粗口力度", "retried_ok")
                out = retry
            else:
                self._rule_hit("粗口力度", "kept_first")
        # 完整性闸门：源文有可译内容，模型却原样回显/输出英文时重试一次。
        # 不处理纯型号（FX / ППШ-41）和无空格缩写，避免无意义请求。
        if (not has_cjk(out) and not has_cjk(text)
                and (re.search(r"[a-zа-яё]{2,}", text)
                     or " " in text.strip())):
            note = ("必须译成简体中文，不要保留英文或俄文原文；"
                    "人名音译，物品/任务按上下文意译。")
            try:
                if mapping:
                    retry_raw = self._translate_plain(
                        masked, lang, note=note)
                    retry = restore_runtime_tokens(retry_raw, mapping)
                else:
                    retry = self._translate_plain(
                        normalized, lang, note=note)
            except Exception:
                retry = None
            if retry and not looks_like_prompt_echo(retry) \
                    and has_cjk(retry):
                self.detail("  译文无中文，已按完整性要求重试：%s"
                            % str(text)[:50])
                self._rule_hit("完整性（无中文）", "retried_ok")
                out = retry
            else:
                # ★ 2.0.13 第三级兜底：整段回显时强制切成短块逐块译。
                # 旧版到这里直接 kept_first，长英文句（如 dm_anomalies_13
                # 341 字符）就整段不翻。只有切片也失败才保留原文。
                sliced = None
                try:
                    if mapping:
                        raw = self._force_sliced(masked, lang, note=note)
                        sliced = restore_runtime_tokens(raw, mapping) \
                            if raw is not None else None
                    else:
                        sliced = self._force_sliced(normalized, lang,
                                                    note=note)
                except Exception:
                    sliced = None
                if sliced and not looks_like_prompt_echo(sliced) \
                        and has_cjk(sliced):
                    self.detail("  译文无中文，切片强制翻译成功：%s"
                                % str(text)[:50])
                    self._rule_hit("完整性（无中文）", "sliced_ok")
                    out = sliced
                elif len(normalized.strip()) <= 48:
                    # ★ 2.0.24（IWP 全量审计 D5）：短名回显第 4 级兜底。
                    # 成就名/绰号/军衔名（Rag and Bone、Lt-Colonel Shulga）
                    # 对「翻译这句话」类指令持续回显（实测 187 条）——改发
                    # 「定名」指令（要求给出中文译名，而非翻译整句）再试
                    # 一次；仍回显才保留原文。通用规则，不依赖具体 MOD。
                    _name_note = ("输入是游戏内的名称（成就、人物、物品、"
                                  "地点、组织）。为该名称给出简洁的中文译名"
                                  "（音译或意译均可），不要解释，不要原样"
                                  "保留英文或俄文。")
                    nm = None
                    try:
                        if mapping:
                            raw = self._translate_plain(masked, lang,
                                                         note=_name_note)
                            nm = restore_runtime_tokens(raw, mapping)
                        else:
                            nm = self._translate_plain(normalized, lang,
                                                       note=_name_note)
                    except Exception:
                        nm = None
                    if (nm and not looks_like_prompt_echo(nm)
                            and has_cjk(nm)
                            and nm.strip() != normalized.strip()):
                        self.detail("  短名回显，已按定名指令给出译名：%s"
                                    % str(text)[:50])
                        self._rule_hit("完整性（无中文）", "named_ok")
                        out = nm
                    else:
                        self._rule_hit("完整性（无中文）", "kept_first")
                else:
                    self._rule_hit("完整性（无中文）", "kept_first")
        # 残留俄文闸门：中文译文里还夹着西里尔词，通常是引号内的格变专名
        # 被模型当成不可译名原样保留。带“引号内也要译”要求重试一次；
        # 仍残留则保留首次译文，不强行音译未知专名。
        residual = _CYR_WORD_RE.findall(out)
        if has_cjk(out) and residual:
            note = _RESIDUAL_CYRILLIC_NOTE % "、".join(
                list(dict.fromkeys(residual))[:3])
            try:
                if mapping:
                    retry_raw = self._translate_plain(
                        masked, lang, note=note)
                    retry = restore_runtime_tokens(retry_raw, mapping)
                else:
                    retry = self._translate_plain(
                        normalized, lang, note=note)
            except Exception:
                retry = None
            if retry and not looks_like_prompt_echo(retry) \
                    and has_cjk(retry) and not _CYR_WORD_RE.search(retry):
                self.detail("  译文残留俄文，已按引号内也译重试：%s"
                            % str(text)[:50])
                self._rule_hit("残留源文", "retried_ok")
                out = retry
            else:
                self._rule_hit("残留源文", "kept_first")
        # ★ 2.0.13 确定性后修复（零模型请求）：圆点注入 + 冠词粘连。
        # 这些是模型的格式噪声，用源文比对即可确定性清除，不必重译整段。
        cleaned, bullet_fixed = clean_injected_bullets(normalized, out)
        if bullet_fixed:
            self.detail("  清除模型注入圆点：%s" % str(text)[:50])
            self._rule_hit("圆点注入", "fixed")
            out = cleaned
        cleaned2, article_fixed = clean_stuck_article(out)
        if article_fixed:
            self.detail("  剥离粘连冠词：%s" % str(text)[:50])
            self._rule_hit("冠词粘连", "fixed")
            out = cleaned2
        # ★ 2.0.14 确定性后修复（零模型请求）：标点规范化 + 型号/年份保真。
        if has_cjk(out):
            out, punct_fixed = normalize_cn_punctuation(out)
            if punct_fixed:
                self._rule_hit("标点规范", "fixed")
        # 型号被模型换成别的（FNX-45→P220）。★ 2.0.24（IWP 全量审计
        # D2/D3）：旧实现只补「（原文型号 …）」注记——错误内容原样留在
        # 成品里还污染玩家阅读。现在先带「型号逐字保留」要求定向重试
        # 一次，仍失败才句末追加裸型号（无「原文」字样）。
        miss_model = missing_model_tokens(normalized, out)
        if miss_model and has_cjk(out):
            _model_note = ("武器/物品型号（如 AK-100、FNX-45）必须原样"
                           "保留，不得替换为其他型号、省略或换算。")
            retried = None
            try:
                if mapping:
                    raw = self._translate_plain(masked, lang,
                                                note=_model_note)
                    retried = restore_runtime_tokens(raw, mapping)
                else:
                    retried = self._translate_plain(normalized, lang,
                                                    note=_model_note)
            except Exception:
                retried = None
            if (retried and not looks_like_prompt_echo(retried)
                    and has_cjk(retried)
                    and not missing_model_tokens(normalized, retried)):
                retried, _ = normalize_cn_punctuation(retried)
                self.detail("  型号保真重试成功：%s" % str(text)[:50])
                self._rule_hit("型号保真", "retried_ok")
                out = retried
            else:
                out = out.rstrip() + "（%s）" % "、".join(miss_model[:3])
                self._rule_hit("型号保真", "fixed")
                self.detail("  型号被替换且重试仍失败，已补回型号：%s"
                            % "、".join(miss_model[:3]))
        # ★ 2.0.24（IWP 全量审计 D10）：半音译粘连闸门。模型把专名音译
        # 一半、原文留一半（「克里门enko队长」「Tkachenko将军」
        # 「$clr_2We正看到」），词库回填要求整词边界、残留西里尔闸门不认
        # 拉丁碎片，这类残词此前完全无人拦。带「整词音译」指令重试一次；
        # 仍粘连则保留现译文只记规则命中——碎片句仍比整段原文可读，
        # 且已在终检点名清单里供人工复核。
        if _to_zh and has_cjk(out):
            glue = find_glue_residue(out)
            if glue:
                _glue_note = ("译文里出现了音译一半、原文留一半的粘连词"
                              "（如「克里门enko」）。人名、绰号、术语要么"
                              "整体音译成中文，要么整词保留原文并加空格，"
                              "禁止字母直接贴着汉字。")
                retried = None
                try:
                    if mapping:
                        raw = self._translate_plain(masked, lang,
                                                    note=_glue_note)
                        retried = restore_runtime_tokens(raw, mapping)
                    else:
                        retried = self._translate_plain(normalized, lang,
                                                        note=_glue_note)
                except Exception:
                    retried = None
                if (retried and not looks_like_prompt_echo(retried)
                        and has_cjk(retried)
                        and not find_glue_residue(retried)):
                    self.detail("  粘连残词（%s），已按整词音译重试：%s"
                                % ("、".join(glue), str(text)[:50]))
                    self._rule_hit("粘连残词", "retried_ok")
                    out = retried
                else:
                    self._rule_hit("粘连残词", "kept_first")
                    self.detail("  粘连残词重试仍失败：%s | %s"
                                % ("、".join(glue), str(text)[:50]))
        # ★ 2.0.18 出口级换行对齐：换行闸门在流程前段，其后数字/完整性/
        # 残留闸门的重试可能再次破坏换行（实测 marsh 背景段 4→2 行）。
        # 在写回前做最后一次对齐，保证任何闸门都破坏不了换行结构。
        if source_nl and out.count("\n") != source_nl:
            realigned = restore_linebreaks(out, source_nl)
            if realigned != out:
                self.detail("  出口换行对齐（%d→%d）：%s"
                            % (out.count("\n"), source_nl, str(text)[:50]))
                self._rule_hit("出口换行对齐", "fixed")
                out = realigned
        final = escape_newlines(out)
        # C. 字面 \n 保真：源有字面换行而译文吞成空格/多加时，按源数量对齐。
        final, nl_fixed = reconcile_literal_newlines(text, final)
        if nl_fixed:
            self._rule_hit("字面换行保真", "fixed")
            self.detail("  字面换行数不符，已按原文对齐：%s"
                        % str(text)[:50])
        self._tm_store(normalized, lang, final)
        return final

    def _force_sliced(self, text, lang, note=""):
        """★ 2.0.13 未翻译兜底：把整段强制切成短块逐块翻译。

        完整性闸门重试仍无中文时说明模型把整段英文原样回显——整段越长越
        容易回显。这里无视 safe_chars 强行按句切成 ≤100 字符的小块，每块
        单独请求（短句翻译成功率高得多），拼回。占位符用 split_protected
        保护不被切断。任一块仍是纯英文则整条判失败（返回 None），交回原文。
        """
        if not text or not text.strip():
            return None
        parts = split_protected(text, 100, _PLACEHOLDER_RE)
        if len(parts) <= 1 and len(text) <= 100:
            # 本来就是短句还回显，切片也帮不上，直接失败
            return None
        pieces = []
        for seg in parts:
            try:
                got = self._translate_segments(seg, lang, note=note)
            except Exception:
                return None
            if not got or not has_cjk(got):
                return None      # 有一块没译出，整条不算成功
            pieces.append(got)
        return "".join(pieces) if pieces else None

    def _translate_plain(self, text, lang, note=""):
        """翻译一个文本串；强制术语模式下整体翻译后回填指定译名。"""
        if lang == self.tgt:
            return text
        if not self.glossary:
            return self._translate_segments(text, lang, note=note)
        if self.force_glossary and self.gloss is not None:
            return self._translate_force(text, lang, note=note)
        return self._translate_segments(text, lang, note=note)

    @staticmethod
    def _inline_term_value(text, start, end, value):
        """源文本身在引号内时，去掉词条目标的外层引号，避免““纪律团””。"""
        v = str(value)
        quotes = "\"'«»“”‘’"
        if (start > 0 and end < len(text)
                and text[start - 1] in quotes and text[end] in quotes
                and len(v) >= 2 and v[0] in quotes and v[-1] in quotes):
            return v[1:-1]
        return v

    def _translate_sliced(self, text, lang, note=""):
        """旧的强制术语做法：把术语之间的片段**各自**送翻，术语直接拼接。

        保留是为了兜底（占位符法校验不过时回退到这里）。实测这条路径在
        一句含多个术语时句子质量差：片段之间没有上下文，会产出
        「去那里西德洛维奇，拿去吧战利品，然后——神器并且进入区域。」这种
        拼接感很强的译文，引号与标点也会被打散。
        """
        out = []
        last = 0
        for start, end, key in self._glossary_spans(text):
            if start > last:
                out.append(self._translate_segments(
                    text[last:start], lang, note=note))
            out.append(self._inline_term_value(
                text, start, end, self.glossary[key]))
            last = end
        if last < len(text):
            out.append(self._translate_segments(
                text[last:], lang, note=note))
        return "".join(out) if out else text

    def _repair_residual_source_terms(self, src, out):
        """A 译文里仍原样留着源文术语时，按词库直接替换，不调用模型。

        只处理 hard canon 且通过引号/大小写策略的 span；替换不改动其它
        文字、标点或换行。比整条重译零风格风险，专门兜住 `"Автомобилист"`
        这类模型当成专名保留的情况。
        """
        if not src or not out or self.gloss is None:
            return out
        spans = self._glossary_spans(src)
        if not spans:
            return out
        quotes = "\"'«»“”‘’"
        for start, end, key in spans:
            term = src[start:end]
            value = self.glossary.get(key)
            if not term or value is None:
                continue
            # 只处理真正的俄文词；拉丁型号（damper.sys、AK-74）留在代码/文件名里，
            # 不能因为词库里有 damper→阻尼器 就把 damper.sys 改成阻尼器.sys。
            if not _CYR_WORD_RE.search(term):
                continue
            if re.search(r"[\\/]", term) or re.search(
                    r"\.[A-Za-z0-9]{1,5}$", term):
                continue
            v = str(value)
            if (self._inside_quotes(src, start, end) and len(v) >= 2
                    and v[0] in quotes and v[-1] in quotes):
                v = v[1:-1]
            if not v or v == term or term not in out:
                continue
            pattern = re.compile(
                r"(?<![А-Яа-яЁё])%s(?![А-Яа-яЁё])" % re.escape(term))
            out2 = pattern.sub(lambda _m: v, out)
            if out2 != out:
                self.detail("  残留源词按词库替换：%s → %s"
                            % (term, v))
                out = out2
        return out

    def _repair_residual_latin_names(self, src, out):
        """★ 2.0.14 英文/拉丁专名残留回填（零模型请求，通用型）。

        模型常把句子中间的人名/绰号原样保留（「Nimble 和 Sidorovich」），
        而词库里明明有标准译名（机灵鬼/西德洛维奇）。旧版
        `_repair_residual_source_terms` 只处理俄文（`_CYR_WORD_RE`），
        拉丁专名全部漏网。这里按词库把译文里残留的英文专名替换为中文译名。

        严格防误伤（词库含大量通用词）：
        - 只处理词库译名确实含中文的（音译/意译专名），译名是英文原样的跳过；
        - 词长 >= 4 且首字母大写（或是含空格的多词人名），排除纯技术码；
        - 排除含数字/点/斜杠的型号、路径、扩展名；
        - 词边界锚定，只替换译文里仍以原英文形态残留的（已译的不动）。
        ★ 2.0.15 两处扩展：
        - 词条层不再限于 names 层：异常/地点/物品等专名（whirligig→旋转木马）
          同样回填，防误伤由「译名含中文 + 首字母大写 + 词边界」继续保证；
        - 允许复数/所有格后缀（Whirligigs / Jupiter’s）：中文无复数，
          整体替换为译名，后缀一并吃掉。
        """
        if not src or not out or self.gloss is None or not self.gloss_names:
            return out
        # 从源文里找命中的专名 span（用 gloss.find，与硬词同一套索引）
        spans = self.gloss.find(src)
        if not spans:
            return out
        quotes = "\"'«»“”‘’"
        for start, end, key in spans:
            term = src[start:end]
            if len(term) < 4:
                continue
            # 拉丁专名：全 ASCII 字母/空格/连字符，首字母大写；排除技术码
            if not re.fullmatch(r"[A-Za-z][A-Za-z'’\- ]*", term):
                continue
            # ★ 2.0.15：首字母小写也放行——只要词库刻意收录了该小写词条
            # （如 whirligig→旋转木马），说明它是专名而非普通词。
            if not (term[0].isupper() or " " in term
                    or (key[:1] and key[0].islower())):
                continue
            value = self.glossary.get(key)
            if value is None:
                continue
            v = str(value)
            # 译名必须含中文，且不与原词相同（避免把 "AK-74" 换自己）
            if not has_cjk(v) or v == term:
                continue
            if (self._inside_quotes(src, start, end) and len(v) >= 2
                    and v[0] in quotes and v[-1] in quotes):
                v = v[1:-1]
            # ★ 2.0.15 允许可选复数/所有格后缀（中文无复数，整体替换）；
            # 模型常改变专名大小写（源 whirligig → 译 Whirligigs），用 re.I。
            pat_str = (r"(?<![A-Za-z])%s(?:s|’s|'s)?(?![A-Za-z])"
                       % re.escape(term))
            # 只替换译文里仍以原英文形态残留的（词边界）
            if not re.search(pat_str, out, re.I):
                continue
            pattern = re.compile(pat_str, re.I)
            out2 = pattern.sub(lambda _m: v, out)
            if out2 != out:
                self.detail("  残留英文专名按词库替换：%s → %s"
                            % (term, v))
                out = out2
        return out

    def _repair_known_semantic_terms(self, src, out, lang=""):
        """英文源里两个已知语义错译的定点修复。

        Controller 在 STALKER 里是脑波怪，不是“控制器”；Freedom 是
        自由团，不是“自由派”。只在英文源、且源文确实出现该词时修，
        不改中文里正常的“控制器/自由”用法。
        """
        if not src or not out or lang != "eng":
            return out
        low = src.lower()
        if re.search(r"(?<![A-Za-z])controllers?(?![A-Za-z])", low):
            out = re.sub(r"控制者|控制器", "脑波怪", out)
        if re.search(r"(?<![A-Za-z])Freedom(?![A-Za-z])", src) or "freedom faction" in low:
            out = re.sub(r"自由派(?:团体|势力|派别|阵营|组织)?",
                         "“自由团”", out)
        return out

    def _translate_force(self, text, lang, note=""):
        """强制术语：先把术语换成占位符，**整串一次翻译**，再把占位符回填成
        词库指定译名。

        为什么不用旧做法：旧做法按术语切片，术语之间的每段各自翻译 —— 一句里
        出现 3 个术语就是 4 次请求，片段之间没有上下文，译文会有拼接感、标点与
        引号被打散（用户实测「非常差」）。改成占位符后整串只翻一次，语序、
        标点、语气完整；占位符由系统提示词第 1 条要求逐字保留（两档模型实测
        都能守住），最后回填，术语仍然是 100% 确定。
        校验不过（占位符丢失/控制符被改）时自动回退旧做法，不会产出坏译文。
        """
        hits = self._glossary_spans(text)
        if not hits:
            # 无强制术语时仍要透传 note：数字保真/上下文/占位符/换行等
            # 闸门的重试指令全靠它，漏传会让重试永远无效、整条保留原文。
            return self._translate_segments(text, lang, note=note)
        # ★ 2.0.25（aera 全量审计 D12 签名①）：超长多术语条目不走 $$T$$
        # 占位符法。实测百科长文（800+ 字符、多处色码/换行）里 1.8B 连续
        # 破坏 $$T1$$，两次重试失败 → 切片兜底的片段占位符也失败 → 整条
        # 保留原文（15 条百科条目全漏）。长文改走普通整译，术语交译后
        # 词库回填（_repair_residual_source_terms/_latin_names 实测有效）。
        if len(text) >= 600:
            self.detail("  长文多术语，跳过占位符法改普通整译+回填：%s"
                        % str(text)[:50])
            return self._translate_segments(text, lang, note=note)

        mapping = {}
        parts = []
        last = 0
        for i, (start, end, key) in enumerate(hits, 1):
            ph = "$$T%d$$" % i
            mapping[ph] = self._inline_term_value(
                text, start, end, self.glossary[key])
            parts.append(text[last:start])
            parts.append(ph)
            last = end
        parts.append(text[last:])
        masked = "".join(parts)

        hard = [pc["content"] for pc in self.p.cutTextWithSeparator(text)
                if not pc["needTrans"] and pc["content"].strip()]

        try:
            out = self._translate_masked(masked, lang, note=note)
        except Exception as e:
            self.detail("  占位符整译失败（%s: %s），回退到切片方式"
                        % (type(e).__name__, e))
            return self._translate_sliced(text, lang, note=note)

        bad = [ph for ph in mapping if out.count(ph) != 1]
        if bad:
            self.detail("  占位符未原样保留（%s），回退到切片方式"
                        % "、".join(bad[:3]))
            return self._translate_sliced(text, lang, note=note)
        lost = [h for h in hard if h not in out]
        if lost:
            self.detail("  控制符被改动（%s），回退到切片方式"
                        % "、".join(repr(h) for h in lost[:3]))
            return self._translate_sliced(text, lang, note=note)

        for ph, val in mapping.items():
            out = out.replace(ph, val)
        return out

    def _translate_masked(self, masked, lang, note=""):
        """把已占位符化的整串交给模型翻一次（超长时按句切分，不丢占位符）。"""
        with self._stat_lock:
            self.req_count += 1
            self.trans_chars += len(masked)
        if len(masked) <= self.safe_chars:
            return self.srv.translate(
                masked, lang, self.tgt, extra_sys=note or None,
                context=getattr(self, "_ctx_hint", "") or None,
                style=getattr(self, "_style_hint", "dialogue"),
                temperature=self._temp_for_style())
        parts = split_protected(masked, self.safe_chars, _PLACEHOLDER_RE)
        with self._stat_lock:
            self.long_chunks += max(0, len(parts) - 1)
        self.log("  长文本 %d 字符 → 自动切为 %d 段翻译（%s 上下文窗）"
                 % (len(masked), len(parts), self.ctx_size))
        return "".join(self._translate_masked(seg, lang, note)
                       for seg in parts)


    def _fallback_done_dir(self):
        """输入目录不可写（Program Files/只读盘/游戏目录受保护）时，
        把译文落到本机可变数据目录下，按输入路径派生唯一名。"""
        safe = re.sub(r'[<>:"/\\|?*]+', "_",
                      os.path.abspath(self.in_dir)).strip("_")
        return os.path.join(_DATA_ROOT, "translated_output", safe[-120:])

    def _resolve_done_dir(self, preferred):
        """确定实际输出目录：首选可写就用；不可写自动回退并明确告知。"""
        try:
            if _writable(preferred):
                return preferred
        except Exception:
            pass
        fb = self._fallback_done_dir()
        os.makedirs(fb, exist_ok=True)
        self.log("输出目录不可写（%s），译文改存到：%s"
                 % (preferred, fb), "warn")
        return fb

    def _session_tables(self):
        """当前 MOD 的文本表（兼容 configs/text 与 config/text 两种布局）。"""
        for root, _dirs, files in self._pruned_walk():
            for fn in files:
                if not fn.lower().endswith(".xml"):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), self.in_dir)
                low = rel.lower().replace(os.sep, "/")
                if not (low.startswith("configs/text/")
                        or low.startswith("config/text/")):
                    continue
                if "sgtat_auto_texts" in low or "/___" in low:
                    continue
                yield rel, os.path.join(root, fn)

    def _session_candidates(self):
        """抽取名字型候选；返回 {lower: (source, category, lang, rel, id)}。"""
        core = {k.lower() for k in self.glossary}
        stop = set(GLOSSARY_HINT_STOPWORDS) | {
            "нет", "да", "все", "всё", "это", "так", "там", "тут", "очень",
            "можно", "нужно", "надо", "есть", "был", "была", "были",
        }
        cand = {}
        self._session_core_hits = set()
        for rel, full in self._session_tables():
            if self._cancelled():
                return cand
            try:
                ents = self._parse(self.p.parse_xray_text_xml, full,
                                   read_encoding_chain(full))
            except Exception:
                continue
            for ent in ents:
                cat = session_category(rel, ent.id)
                if not cat:
                    continue
                lang, src = self._pick_source(ent)
                if not src or has_cjk(src):
                    continue
                bare = session_term_shape(src, cat)
                if not bare:
                    continue
                low = bare.lower()
                if low in core:
                    # 核心词已在通用库；只把它登记为“本次 MOD 见过”，
                    # 让对话里也能作为软提示，不重新翻译、不硬替换。
                    self._session_core_hits.add(bare)
                    continue
                if (low in stop
                        or looks_like_code_or_debug(rel, bare)
                        or looks_like_identifier_text(bare)):
                    continue
                if low not in cand:
                    cand[low] = (bare, cat,
                                 detect_text_lang(bare) or lang or "rus",
                                 rel, ent.id)
        order = {"人物与军衔": 0, "阵营与组织": 1, "地点与区域": 2,
                 "变异生物": 3, "物品·武器·装备·神器": 4,
                 "成就·称号·属性标签": 5}
        rows = sorted(cand.values(),
                      key=lambda x: (order.get(x[1], 9), len(x[0]), x[0]))
        kept = {}
        used = {k: 0 for k in _SESSION_LIMITS}
        for src, cat, lang, rel, eid in rows:
            if used.get(cat, 0) >= _SESSION_LIMITS.get(cat, 0):
                continue
            used[cat] += 1
            kept[src.lower()] = (src, cat, lang, rel, eid)
        return kept

    @staticmethod
    def _clean_term_line(line):
        s = (line or "").strip()
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", s)
        return s.strip().strip("\"'“”‘’` ").strip()

    def _translate_session_batch(self, terms, lang):
        """批量翻译名词；行数不符时返回空，交回单条重试。"""
        self.session_requests += 1
        try:
            out = self.srv.translate(
                "\n".join(terms), lang, self.tgt,
                extra_sys=_SESSION_TERM_NOTE)
        except Exception:
            return {}
        lines = [self._clean_term_line(x) for x in (out or "").splitlines()
                 if x.strip()]
        if len(lines) != len(terms):
            return {}
        result = {}
        for src, dst in zip(terms, lines):
            if (dst and dst != src and has_cjk(dst)
                    and not looks_like_prompt_echo(dst) and len(dst) <= 60):
                result[src] = dst
        return result

    def _build_session_glossary(self):
        """扫描当前 MOD，补齐名字型术语；缓存只在本机，不随包。"""
        if not self.srv or not self.srv.is_running():
            return
        cand = self._session_candidates()
        if not cand:
            self.session_keys = set(getattr(self, "_session_core_hits", set()))
            self.session_stats = {"candidates": 0, "cached": 0,
                                  "translated": 0, "failed": 0}
            return
        raw = "\n".join("%s|%s|%s" % (v[3], v[4], v[0])
                        for v in sorted(cand.values()))
        fp = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()
        cache_path = os.path.join(self.session_cache_dir, fp + ".json")
        cached = {}
        try:
            data = json.load(open(cache_path, encoding="utf-8"))
            if isinstance(data.get("terms"), dict):
                cached = {str(k): str(v) for k, v in data["terms"].items()}
        except Exception:
            cached = {}
        cache_low = {k.lower(): v for k, v in cached.items()}
        translated = {}
        missing = [(v[0], v[2]) for low, v in cand.items()
                   if low not in cache_low]
        by_lang = {}
        for src, lang in missing:
            by_lang.setdefault(lang, []).append(src)
        for lang, items in by_lang.items():
            for i in range(0, len(items), 20):
                batch = items[i:i + 20]
                got = self._translate_session_batch(batch, lang)
                translated.update(got)
                for src in batch:
                    if src in got:
                        continue
                    self.session_requests += 1
                    try:
                        out = self.srv.translate(
                            src, lang, self.tgt,
                            extra_sys=_SESSION_TERM_NOTE)
                    except Exception:
                        continue
                    dst = self._clean_term_line(out)
                    if (dst and dst != src and has_cjk(dst)
                            and not looks_like_prompt_echo(dst)
                            and len(dst) <= 60):
                        translated[src] = dst
        merged = dict(cached)
        merged.update(translated)
        self.session_keys = set(merged) | set(
            getattr(self, "_session_core_hits", set()))
        self.session_only_keys = set(merged)
        for src, dst in merged.items():
            self.glossary[src] = dst
        self.gloss = self._new_gloss_index()
        try:
            os.makedirs(self.session_cache_dir, exist_ok=True)
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"_format": "ai-translate-session-terms/1",
                           "fingerprint": fp, "terms": merged},
                          f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, cache_path)
        except OSError:
            pass
        self.session_stats = {
            "candidates": len(cand),
            "cached": len(cache_low),
            "translated": len(translated),
            "failed": max(0, len(missing) - len(translated)),
        }
        self.log("会话词表：候选 %d 条，缓存 %d，新译 %d，失败 %d（本机缓存，不随包）"
                 % (self.session_stats["candidates"],
                    self.session_stats["cached"],
                    self.session_stats["translated"],
                    self.session_stats["failed"]), "dim")

    def _build_freq_names(self):
        """★ 2.0.3 高频专名定译：全文扫描正文高频词，复用会话词表的
        批量定译与本机缓存管线，结果并入**软提示层**（术语提示只在语义
        相符时采用），收敛自造译名的跨文件漂移。"""
        if not self.srv or not self.srv.is_running():
            return
        total, mid, lang_of = {}, {}, {}
        scanned = 0
        for rel, full in self._collect_text_targets():
            if self._cancelled():
                return
            if scanned >= _FREQ_SCAN_FILE_CAP:
                break
            scanned += 1
            try:
                ents = self._parse(self.p.parse_xray_text_xml, full,
                                   read_encoding_chain(full))
            except Exception:
                continue
            for ent in ents:
                lang, src = self._pick_source(ent)
                if not src or has_cjk(src):
                    continue
                for w in _FREQ_WORD_RE.findall(src):
                    lang_of.setdefault(w, lang)
                    total[w] = total.get(w, 0) + 1
                for w in _FREQ_MID_RE.findall(src):
                    mid[w] = mid.get(w, 0) + 1
        stop = GLOSSARY_HINT_STOPWORDS
        cand = []
        for w, n in total.items():
            if n < _FREQ_NAME_MIN_TOTAL or mid.get(w, 0) < _FREQ_NAME_MIN_MID:
                continue
            if len(w) <= 3 or w.lower() in stop or w in self.glossary:
                continue
            cand.append((n, w))
        cand.sort(reverse=True)
        cand = [w for _n, w in cand[:_FREQ_NAME_LIMIT]]
        self.freq_stats = {"candidates": len(cand), "cached": 0,
                           "translated": 0, "failed": 0}
        if not cand:
            self.freq_names = {}
            return
        raw = "\n".join("%s|%s" % (lang_of.get(w, self.src_lang), w)
                        for w in cand)
        fp = hashlib.sha1(
            ("freq/1/" + raw).encode("utf-8", "replace")).hexdigest()
        cache_path = os.path.join(self.session_cache_dir,
                                  "freq_" + fp + ".json")
        cached = {}
        try:
            data = json.load(open(cache_path, encoding="utf-8"))
            if isinstance(data.get("terms"), dict):
                cached = {str(k): str(v) for k, v in data["terms"].items()}
        except Exception:
            cached = {}
        cache_low = {k.lower(): v for k, v in cached.items()}
        self.freq_stats["cached"] = len(cache_low)
        translated = {}
        missing = [(w, lang_of.get(w, self.src_lang)) for w in cand
                   if w.lower() not in cache_low]
        by_lang = {}
        for w, lang in missing:
            by_lang.setdefault(lang, []).append(w)
        for lang, items in by_lang.items():
            for i in range(0, len(items), 20):
                batch = items[i:i + 20]
                got = self._translate_session_batch(batch, lang)
                translated.update(got)
                for w in batch:
                    if w in got:
                        continue
                    self.session_requests += 1
                    try:
                        out = self.srv.translate(
                            w, lang, self.tgt,
                            extra_sys=_SESSION_TERM_NOTE)
                    except Exception:
                        continue
                    dst = self._clean_term_line(out)
                    if (dst and dst != w and has_cjk(dst)
                            and not looks_like_prompt_echo(dst)
                            and len(dst) <= 60):
                        translated[w] = dst
        merged = dict(cached)
        merged.update(translated)
        self.freq_stats["translated"] = len(translated)
        self.freq_stats["failed"] = max(0, len(missing) - len(translated))
        self.freq_names = merged
        self.glossary.update(merged)
        self.gloss_soft |= set(merged)
        self.gloss = self._new_gloss_index()
        try:
            os.makedirs(self.session_cache_dir, exist_ok=True)
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"_format": "ai-translate-freq-names/1",
                           "fingerprint": fp, "terms": merged},
                          f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, cache_path)
        except OSError:
            pass
        self.log("高频专名定译：候选 %d，缓存 %d，新译 %d（软提示层）"
                 % (self.freq_stats["candidates"],
                    self.freq_stats["cached"],
                    self.freq_stats["translated"]), "dim")

    def _is_stale_empty_output(self, out_path):
        """早期缺陷会给不含文本条目的文件生成空 XML 占位，
        这类“译文”必须能被重跑覆盖；有真实条目的输出一律保留。"""
        try:
            ents = self.p.parse_xray_text_xml(
                out_path, read_encoding_chain(out_path))
        except Exception:
            return False
        return len(ents) == 0

    def _load_ledger(self):
        try:
            with open(self._ledger_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_ledger(self):
        if not self._ledger_path:
            return
        with self._ledger_lock:
            try:
                tmp = self._ledger_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._ledger, f, ensure_ascii=False,
                              sort_keys=True)
                os.replace(tmp, self._ledger_path)
                self._ledger_saved = time.time()
            except OSError:
                pass

    def _record_done(self, rel, full, out_path):
        try:
            ist = os.stat(full)
            ost = os.stat(out_path)
        except OSError:
            return
        with self._ledger_lock:
            self._ledger[rel.replace("\\", "/")] = {
                "in_size": ist.st_size,
                "in_mtime_ns": ist.st_mtime_ns,
                "out_size": ost.st_size,
            }
            # 每文件全量重写是 O(n²)（几千文件时写放大明显）；5 秒节流，
            # 崩溃最多让最近几秒的文件下次重译（不会漏、不会错）。
            if time.time() - self._ledger_saved > 5:
                self._save_ledger()

    def _output_covers_input(self, full, out_path):
        """输出 XML 是否包含输入的全部 string id（用于旧输出版本迁移）。"""
        try:
            in_ents = self._parse(self.p.parse_xray_text_xml, full,
                                  read_encoding_chain(full))
            out_ents = self.p.parse_xray_text_xml(
                out_path, read_encoding_chain(out_path))
        except Exception:
            return False
        if not in_ents:
            # 输入里没有条目：交给 _run_text_file 走"清理空占位输出"的既有路径
            return False
        return {e.id for e in in_ents} <= {e.id for e in out_ents}

    def _should_skip(self, rel, full, out_path, kind):
        """该文件是否可以跳过（= 输入未变且输出完整）。"""
        if not os.path.isfile(out_path):
            return False
        try:
            ist = os.stat(full)
            ost = os.stat(out_path)
        except OSError:
            return False
        ent = self._ledger.get(rel.replace("\\", "/"))
        if (ent
                and ent.get("in_size") == ist.st_size
                and ent.get("in_mtime_ns") == ist.st_mtime_ns
                and ent.get("out_size") == ost.st_size):
            return True
        if self._ledger_present:
            # 有账本 = 严格模式：没记录过的输出一律重做（可能是中断残留，
            # 也可能是早期产物；重做一次后即入账）。
            return False
        # 首次升级：早期输出没有账本。文本按"输出是否覆盖输入 id"
        # 判断（能识别缺新条目/半截 XML），其余格式退回 mtime 判断。
        if kind == "text":
            ok = self._output_covers_input(full, out_path)
        else:
            ok = ost.st_mtime >= ist.st_mtime and ost.st_size > 0
        if ok:
            self._record_done(rel, full, out_path)   # 补账，下次走严格模式
        return ok

    def _write_atomic(self, out_path, writer):
        """先写 <path>.tmp 再 os.replace：中断不会留下半截"成品"。"""
        parent = os.path.dirname(out_path) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = out_path + ".tmp"
        try:
            writer(tmp)
            os.replace(tmp, out_path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _final_audit(self, done_dir):
        """终检（2.0.2）：整 MOD 交付前的全量复核，只报告、不删改。

        分层口径 —— 与逐条校验互补，不重复：
          ① **账本在盘**：账本里的每个输出文件存在且非空（原子写之外的第二道
             兜底；账号 out_size>0 而盘上为 0 = 异常）。
          ② **文本表可解析 + ID 全保留**：本次改写过的文本表（_audit_tables）
             逐张解析输出 XML；解析失败 = 表坏；输出缺输入里已有的 string id
             = 漏条目。原样复制的文件不验结构（复制不改内容）。
          ③ **掩码残留**：$$R0$$ 形态是送翻时的临时掩码，正常成品不该出现
             （restore 失败整条回退原文）；扫到即点名"疑似"让人复核。
          ④ **条目未翻译**（2.0.7）：改写过的文本表里，输出仍与源文逐字相同
             且含西里尔——这是"保留原文"的落盘形态（翻译异常/专名跳过）。
             排除已登记专名/术语库既有条目后即疑似漏译，点名让人补。
          ⑤ **上下文串入**（2.0.8）：输出仍带「前一句/后一句」标签 = 译文
             被相邻条目污染，点名补译。
          ⑥ **& 保真哨兵**（2.0.22）：第三方产物见过 `Heckler & Koch` →
             `Heckler  Koch` 的静默吞 &（& 位置塌成双空格）。本插件解析/
             写回链路已验证往返保真（normalize 转义裸 & + generateOutputXml
             再转义），残余风险只剩模型译文吞 &；源含 & 且译文出现源没有
             的双空格即点名复核，只报告不删改。
        终检不通过不影响已有输出、不改 ok —— 账本续跑机制下次会补漏重译。
        范围：文本表 XML（数量最大的产出）。脚本字面层/引用层复制件走 ①。
        """
        with self._ledger_lock:
            ledger_items = list(self._ledger.items())
        missing, empty = [], []
        for rel, info in ledger_items:
            out = os.path.join(done_dir, rel.replace("/", os.sep))
            try:
                size = os.path.getsize(out)
            except OSError:
                missing.append(rel)
                continue
            if size == 0 and int(info.get("out_size") or 0) > 0:
                empty.append(rel)
        bad_parse, lost_ids, residue, n_tables = [], [], [], 0
        untranslated = []          # ★ 2.0.7 条目级漏译（输出=俄文源文）
        ctx_bleed = []             # ★ 2.0.8 上下文串入（前一句/后一句标签）
        amp_lost = []              # ★ 2.0.22 & 保真哨兵（源含 & 译文双空格）
        _CYR_AUDIT = re.compile(r"[а-яёА-ЯЁ]{2,}")
        for rel in sorted(self._audit_tables):
            n_tables += 1
            out = os.path.join(done_dir, rel.replace("/", os.sep))
            full = os.path.join(self.in_dir, rel.replace("/", os.sep))
            try:
                out_ents = self._parse(self.p.parse_xray_text_xml, out,
                                       read_encoding_chain(out))
            except Exception as e:
                bad_parse.append("%s（%s）" % (rel, str(e)[:60]))
                continue
            if os.path.isfile(full):
                try:
                    in_ents = self._parse(self.p.parse_xray_text_xml, full,
                                          read_encoding_chain(full))
                    in_ids = {e.id for e in (in_ents or [])}
                    out_ids = {e.id for e in (out_ents or [])}
                    miss = in_ids - out_ids
                    if miss:
                        lost_ids.append("%s（缺 %d 条：%s…）"
                                        % (rel, len(miss),
                                           ", ".join(sorted(miss)[:3])))
                    # ④ 条目级漏译：同一 id 输出文本仍是输入俄文原样。
                    # 排除已登记专名（词库命中）——那是有意保留，不是漏译。
                    in_map = {e.id: e for e in (in_ents or [])}
                    hits = []
                    for e in (out_ents or []):
                        src_e = in_map.get(e.id)
                        if src_e is None:
                            continue
                        for lang_key, out_val in (e.texts or {}).items():
                            in_val = (src_e.texts or {}).get(lang_key)
                            if not in_val or not out_val:
                                continue
                            if in_val != out_val:
                                continue          # 改过 = 已翻
                            if not _CYR_AUDIT.search(in_val):
                                continue          # 非俄文源（英文等）另算
                            if termbase_lookup(in_val) is not None:
                                continue          # 词库条目：有意保留
                            hits.append(e.id)
                            break
                    if hits:
                        untranslated.append("%s（%d 条：%s…）"
                                            % (rel, len(hits),
                                               ", ".join(hits[:3])))
                    # ⑤ 上下文串入：输出文本仍带「前一句/后一句」标签。
                    # 这是译文被相邻条目污染的直接证据，必须点名补译。
                    bleed_ids = []
                    for e in (out_ents or []):
                        for out_val in (e.texts or {}).values():
                            if looks_like_context_echo(out_val):
                                bleed_ids.append(e.id)
                                break
                    if bleed_ids:
                        ctx_bleed.append("%s（%d 条：%s…）"
                                         % (rel, len(bleed_ids),
                                            ", ".join(bleed_ids[:3])))
                    # ⑥ & 保真哨兵：源含 & 的已翻条目，译文出现源没有的
                    # 双空格 = 吞 & 特征签名（Heckler & Koch → Heckler  Koch）。
                    # 只点名，不自动改写（译成「黑克勒-科赫」专名属正常）。
                    amp_ids = []
                    for e in (out_ents or []):
                        src_e = in_map.get(e.id)
                        if src_e is None:
                            continue
                        for lang_key, out_val in (e.texts or {}).items():
                            in_val = (src_e.texts or {}).get(lang_key)
                            if not in_val or not out_val or in_val == out_val:
                                continue          # 未翻/空值不看
                            if "&" not in in_val:
                                continue          # 源无 & 不看
                            if "  " in out_val and "  " not in in_val:
                                amp_ids.append(e.id)
                                break
                    if amp_ids:
                        amp_lost.append("%s（%d 条：%s…）"
                                        % (rel, len(amp_ids),
                                           ", ".join(amp_ids[:3])))
                except Exception:
                    pass    # 输入表已不存在/解析失败：以输出可解析为准
            try:
                raw = read_text_any(out)
            except Exception:
                raw = ""
            if _MASK_RESIDUE_RE.search(raw or ""):
                residue.append(rel)
        problems = (len(missing) + len(empty) + len(bad_parse)
                    + len(lost_ids) + len(residue) + len(untranslated)
                    + len(ctx_bleed) + len(amp_lost))
        if problems:
            self.log("终检：账本 %d 个文件 / 改写文本表 %d 张，发现 %d 处问题"
                     % (len(ledger_items), n_tables, problems), "warn")
            for rel in missing[:10]:
                self.log("  终检｜账本有记录但输出缺失：%s" % rel, "warn")
            for rel in empty[:10]:
                self.log("  终检｜输出为空文件：%s" % rel, "warn")
            for rel in bad_parse[:10]:
                self.log("  终检｜文本表解析失败：%s" % rel, "warn")
            for rel in lost_ids[:10]:
                self.log("  终检｜输出疑似漏条目：%s" % rel, "warn")
            for rel in residue[:10]:
                self.log("  终检｜疑似未还原掩码（$$R…）：%s" % rel, "warn")
            for rel in untranslated[:20]:
                self.log("  终检｜条目未翻译（保留俄文）：%s" % rel, "warn")
            for rel in ctx_bleed[:20]:
                self.log("  终检｜上下文串入（前一句/后一句）：%s" % rel,
                         "warn")
            for rel in amp_lost[:20]:
                self.log("  终检｜疑似吞 &（& 塌成双空格）：%s" % rel, "warn")
            if untranslated or ctx_bleed:
                self.log("  终检｜上述条目只点名前 20 张表；"
                         "可清空输出目录后重跑补译", "warn")
            if problems > 40:
                self.log("  终检｜其余问题只点名前 40 处，其余见详细日志",
                         "warn")
        else:
            self.log("终检通过：账本 %d 个文件、改写文本表 %d 张全部可解析，"
                     "无缺失/漏条目/掩码残留" % (len(ledger_items), n_tables),
                     "ok")

    def run(self):
        done_dir = self._resolve_done_dir(self.out_dir)
        self.out_dir = done_dir
        self.done_dir = done_dir
        self._excluded_paths = {os.path.normcase(os.path.abspath(done_dir))}
        os.makedirs(done_dir, exist_ok=True)
        self._ledger_path = os.path.join(done_dir, ".ai_translate_done.json")
        self._ledger_present = os.path.isfile(self._ledger_path)
        self._ledger = self._load_ledger()

        if self.auto_session_terms and not self.preview_reference:
            try:
                self._build_session_glossary()
                self.req_count += self.session_requests
            except Exception as e:
                self.log("会话词表生成失败，继续使用通用术语库：%s：%s"
                         % (type(e).__name__, e), "warn")

        if self.auto_freq_names and not self.preview_reference:
            try:
                self._build_freq_names()
            except Exception as e:
                self.log("高频专名定译失败，跳过：%s：%s"
                         % (type(e).__name__, e), "warn")

        reuse = {}
        if (self.reuse_dir and os.path.isdir(self.reuse_dir)
                and not self.reference_mode):
            reuse = self._read_existing(self.reuse_dir)
            self.log("已载入复用译文 %d 条" % len(reuse))

        try:
            if self.reference_mode and self.mode in ("auto", "text"):
                ok = self._run_reference_layer(done_dir, reuse)
                if (not ok and self.mode == "auto"
                        and not self._table_entries
                        and not self._cancelled()):
                    # ★ 2.0.23：引用层收编为固定模式后，没有 configs/text
                    # 文本表的 MOD（纯脚本/ltx）自动回退整目录模式，不再
                    # 警告后终止。引用索引为空，only_referenced 若开着会把
                    # 所有条目过滤掉，回退时强制关闭。
                    if self.only_referenced:
                        self.log("无引用索引，「只译被引用条目」在回退模式下不生效",
                                 "warn")
                    self.only_referenced = False
                    self.log("回退：按整目录直接翻译模式继续", "warn")
                    ok = self._run_auto(done_dir, reuse)
            elif self.mode == "auto":
                ok = self._run_auto(done_dir, reuse)
            elif self.mode == "text":
                temp_dir = os.path.join(done_dir, "__temp__")
                os.makedirs(temp_dir, exist_ok=True)
                reuse.update(self._read_existing(temp_dir))
                targets = self._collect_text_targets()
                total = len(targets)
                self.log("待翻译文本 XML %d 个（递归子目录，已排除输出目录）"
                         % total)
                if total == 0:
                    self.log("未发现任何 .xml 文件。请把目录选到 configs"
                             "（文本 XML 在其 text\\ 与 ui\\ 子目录内）", "warn")
                skipped_empty = 0
                trans_files = 0
                failed = 0
                for idx, (rel, full) in enumerate(targets, 1):
                    if self._cancelled():
                        self.log("已取消")
                        ok = False
                        break
                    out_path = os.path.join(done_dir, rel)
                    if self._should_skip(rel, full, out_path, "text"):
                        continue
                    self._log_progress(idx, total, os.path.basename(full))
                    self.progress({"file": rel, "file_idx": idx, "files": total,
                                   "ids": trans_files, "total_ids": 0,
                                   "tabs": trans_files,
                                   "pending": self.pending_files,
                                   "reqs": self.req_count, "unit": "个文件"})
                    try:
                        n_texts = self._run_text_file(
                            os.path.basename(full), full, out_path,
                            os.path.join(temp_dir, rel), reuse, idx, total)
                    except Exception as e:
                        failed += 1
                        self.log("文件处理失败，已跳过：%s（%s: %s）"
                                 % (rel, type(e).__name__, e), "err")
                        continue
                    if n_texts > 0:
                        trans_files += 1
                        self._record_done(rel, full, out_path)
                    if n_texts == 0:
                        skipped_empty += 1
                else:
                    ok = True
                if skipped_empty:
                    self.log("跳过 %d 个不含 <string> 文本条目的 XML"
                             % skipped_empty)
                if failed:
                    self.log("有 %d 个文件处理失败（已跳过，其余照常完成），"
                             "详见日志" % failed, "warn")
            else:
                ok = self._run_recursive(done_dir, reuse)
        finally:
            # 结束时强制落盘（节流写可能还没写过）。但**空账本不写**：
            # 用户一开跑就取消时若落一个空账本，下次会进入严格模式，
            # 把早期遗留的输出全部重译一遍（安全但白白耗时）。
            if not (self.reference_mode and self.preview_reference):
                if self._ledger or self._ledger_present:
                    self._save_ledger()
                removed = self._prune_empty_dirs(done_dir)
                if removed:
                    self.log("已清理 %d 个空目录" % removed)

        # ★ 2.0.2 终检：整 MOD 交付前的最后一道闸。逐条校验（占位符 mask/
        #   校验/回填、失败保留原文）在翻译时就做了，这里验收"整面墙"——
        #   只报告不删改；取消/部分完成时也跑（报告的就是已落盘的部分）。
        if not (self.reference_mode and self.preview_reference):
            try:
                self._final_audit(done_dir)
            except Exception as e:
                self.log("终检未完成（不影响已生成的输出）：%s: %s"
                         % (type(e).__name__, e), "warn")

        # ★ 2.0.3 提示词体检：规则违规遥测汇总，为提示词迭代提供数据。
        try:
            self._audit_report()
        except Exception:
            pass

        self.log("全部完成：请求 %d 段，%s 字符%s" %
                 (self.req_count, _fmt_size(self.trans_chars),
                  ("，超长文本自动分句 %d 次" % self.long_chunks)
                  if self.long_chunks else ""))
        if self.tm_stats.get("scope") != "off" or self.tm_stats.get("hits"):
            self.log("TM-lite：%s，精确复用 %d 条，节省 %s 字符"
                     % ("跨表" if self.tm_stats.get("scope") == "global"
                        else "表内", self.tm_stats.get("hits", 0),
                        _fmt_size(self.tm_stats.get("saved_chars", 0))),
                     "dim")
        if self.empty_files:
            self.log("有 %d 个文件未提取到可译文本（前 20 个见详细日志）"
                     % self.empty_files)
        if self.failed_strings:
            self.log("有 %d 条文本翻译失败（已保留原文，详见详细日志）"
                     % self.failed_strings, "warn")
        if self.redundant:
            self.log("以下 ID 在多个文件重复，请人工核对：%s"
                     % ", ".join(self.redundant[:50]))
        self.log("译文目录：%s" % done_dir, "ok")
        return ok

    # ── ID 引用层模式（跨 MOD 通用）────────────────────────────────
    # 补丁/模组的正文在 configs/text 的条目里，XML/脚本只放条目 ID。
    # 这里不猜新 ID、不改写引用，只把“定义该 ID 的文本表条目”译成中文；
    # 引用层文件原样复制。这样 295 个引用层文件不会漏，也不会被改坏。
    def _read_localization(self):
        info = {"language": "", "files": []}
        path = os.path.join(self.in_dir, "configs", "localization.ltx")
        if not os.path.isfile(path):
            return info
        for line in read_text_any(path).splitlines():
            line = line.strip()
            if not line or line.startswith(";") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lower()
            if key == "language":
                info["language"] = value.strip().lower()
            elif key == "files":
                info["files"] = [x.strip() for x in value.split(",")
                                 if x.strip()]
        return info

    def _is_excluded(self, path):
        ap = os.path.normcase(os.path.abspath(path))
        return any(ap == ex or ap.startswith(ex + os.sep)
                   for ex in self._excluded_paths)

    def _iter_text_tables(self):
        root = os.path.join(self.in_dir, "configs", "text")
        out = []
        if not os.path.isdir(root):
            return out
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if not self._is_excluded(os.path.join(cur, d))]
            for fn in files:
                full = os.path.join(cur, fn)
                if fn.lower().endswith(".xml") and not self._is_excluded(full):
                    out.append(full)
        out.sort()
        return out

    def _build_text_index(self):
        self._table_entries = {}
        self._id_files = {}
        self._localization = self._read_localization()
        self._lang_hint = (self._localization.get("language") or "").lower()
        if not self._lang_hint:
            # 没写 localization.ltx 时，从 configs/text/<lang>/ 推断
            for full in self._iter_text_tables():
                parts = full.replace("\\", "/").lower().split("/configs/text/")
                if len(parts) == 2 and "/" in parts[1]:
                    self._lang_hint = parts[1].split("/")[0]
                    break
        for full in self._iter_text_tables():
            try:
                ents = self._parse(self.p.parse_xray_text_xml, full,
                                   read_encoding_chain(full))
            except Exception as e:
                self.detail("  文本表解析失败，跳过：%s（%s）"
                            % (os.path.basename(full), e))
                continue
            table = {}
            for ent in ents:
                if ent.id:
                    table[ent.id] = ent
            self._table_entries[full] = table
            for eid in table:
                self._id_files.setdefault(eid, []).append(full)
        self.log("文本表：%d 个文件 / %d 个 ID（语言 %s，注册表 %d 张）"
                 % (len(self._table_entries), len(self._id_files),
                    self._localization.get("language") or "未声明",
                    len(self._localization.get("files") or [])))

    @staticmethod
    def _looks_like_ref_value(value):
        v = (value or "").strip()
        if not (2 <= len(v) <= 120):
            return False
        if re.search(r"\s", v):
            return False
        if v.lower().endswith((".xml", ".ltx", ".script", ".dds", ".ogg",
                               ".wav", ".txt", ".ini")):
            return False
        if "/" in v or "\\" in v or ":" in v:
            return False
        # ★ 2.0.24（IWP 全量审计 D9）：未解析报告要求下划线形态。旧判据
        # 把一切标识符形态字符串都算"未解析引用"，实测 CSV 里
        # ammo/gr/ap/fmj/OK/third/dummy 等 8 类全是 XML 配置参数值，
        # 不是条目 ID，稀释真正待确认项。能在索引里解析到的 ID 走
        # add_ref 第一条分支，不受本判据影响——只收紧报告，零覆盖变化。
        # XRay 文本表 ID 绝大多数带下划线（st_/ui_/mgr_/GENERATE_NAME_…）；
        # 无下划线的裸词 ID（controls/ok）本来就解析得到，不会进报告。
        if "_" not in v:
            return False
        return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.\-]*$", v))

    def _scan_references(self):
        refs = {}
        ref_files = set()
        unresolved = {}
        text_root = os.path.abspath(
            os.path.join(self.in_dir, "configs", "text"))
        configs_root = os.path.join(self.in_dir, "configs")

        def add_ref(value, full, count_unresolved=True):
            v = (value or "").strip()
            if not v:
                return False
            if v in self._id_files:
                refs[v] = refs.get(v, 0) + 1
                ref_files.add(full)
                return True
            if count_unresolved and self._looks_like_ref_value(v):
                unresolved[v] = unresolved.get(v, 0) + 1
            return False

        for cur, dirs, files in os.walk(configs_root):
            ap = os.path.abspath(cur)
            if ap == text_root or ap.startswith(text_root + os.sep):
                continue
            if self._is_excluded(cur):
                dirs[:] = []
                continue
            for fn in files:
                if not fn.lower().endswith(".xml"):
                    continue
                full = os.path.join(cur, fn)
                text = read_text_any(full)
                for v in _XML_REF_TAG_RE.findall(text):
                    add_ref(v, full)
                for v in _XML_REF_ATTR_RE.findall(text):
                    add_ref(v, full)

        scripts_root = os.path.join(self.in_dir, "scripts")
        if os.path.isdir(scripts_root):
            for cur, dirs, files in os.walk(scripts_root):
                if self._is_excluded(cur):
                    dirs[:] = []
                    continue
                for fn in files:
                    if not fn.lower().endswith(".script"):
                        continue
                    full = os.path.join(cur, fn)
                    text = read_text_any(full)
                    ids = list(_SCRIPT_TRANSLATE_RE.findall(text))
                    for v in _SCRIPT_QUOTED_RE.findall(text):
                        if v in self._id_files and v not in ids:
                            ids.append(v)
                    for v in ids:
                        add_ref(v, full, count_unresolved=False)

        self._ref_counts = refs
        self._ref_files = ref_files
        self._unresolved = unresolved
        self.log("引用层：%d 个 XML/脚本，%d 条引用（%d 个不同 ID）；"
                 "未解析 %d 个"
                 % (len(ref_files), sum(refs.values()), len(refs),
                    len(unresolved)))
        if unresolved:
            top = sorted(unresolved.items(), key=lambda kv: -kv[1])[:20]
            self.log("未解析引用（保留原文，前 20）：%s"
                     % "、".join("%s×%d" % (k, v) for k, v in top), "warn")
        return refs, ref_files, unresolved

    def _pick_source(self, ent):
        """按游戏语言取源文：显式源语言 → localization → rus 优先。

        STALKER 系 MOD 的主语言基本是俄语。旧逻辑用 vendor 的
        getRecommendLangText 会优先 eng，把英语意译当原文（例如
        “all right”被翻成“所有 向右”、“rest”被翻成“休息”），
        俄文原文反而没被使用。这里默认 rus 优先，只有缺 rus 才用 eng。
        """
        texts = ent.texts or {}
        if not isinstance(texts, dict):
            return self.p.getRecommendLangText(ent, self.tgt)
        order = []
        if self.src_lang in ("rus", "eng", "ukr"):
            order.append(self.src_lang)
        if self._lang_hint:
            order.append(self._lang_hint)
        order += ["rus", "text", "eng", "ukr", "tur"]
        seen = set()
        for lang in order:
            if lang in seen:
                continue
            seen.add(lang)
            value = texts.get(lang, "")
            if value and value.strip():
                return lang, value
        return self.p.getRecommendLangText(ent, self.tgt)

    def _reference_candidates(self):
        total = todo = protected = 0
        tm_values = []
        for full, table in self._table_entries.items():
            rel = os.path.relpath(full, self.in_dir)
            for ent in table.values():
                _lang, src = self._pick_source(ent)
                if not src or not src.strip():
                    continue
                if self._looks_like_target(src):
                    continue
                if not _has_translatable_text(src):
                    continue
                if looks_like_code_or_debug(rel, src):
                    continue
                total += 1
                tm_values.append(src)
                if self.only_referenced and ent.id not in self._ref_counts:
                    continue
                if self.protect_proper and looks_like_proper_text(rel, src):
                    protected += 1
                    continue
                todo += 1
        self._tm_enable_global(tm_values)
        return total, todo, protected

    def _run_reference_layer(self, done_dir, reuse):
        self._build_text_index()
        if not self._table_entries:
            self.log("没有找到 configs/text 文本表", "warn")
            return False
        self._scan_references()
        total, todo, protected = self._reference_candidates()
        self.log("引用层预览：可译条目 %d 条，其中待译 %d 条，保护专名 %d 条"
                 % (total, todo, protected))
        if self.preview_reference:
            self.log("预览完成（未写盘）。取消勾选「先预览」后再开始即执行。",
                     "ok")
            return True

        # 早期生成的根目录表没有注册进 localization.ltx，
        # 属于旧机制残留；引用层模式不再产出它，顺手清掉避免误部署。
        for stale in ("___auto_put_this_to_text_folder.xml",
                      "待确认_未解析引用.csv"):
            try:
                os.remove(os.path.join(done_dir, stale))
            except OSError:
                pass

        tables = sorted(self._table_entries)
        n_tables = len(tables)
        changed = copied = translated = skipped = failed = 0

        def _one(item):
            idx, full = item
            rel = os.path.relpath(full, self.in_dir)
            out_path = os.path.join(done_dir, rel)
            st = self._translate_table(full, out_path, rel)
            return idx, rel, st

        items = list(enumerate(tables, 1))
        done_n = 0
        trans_total = 0
        ent_total = 0
        trans_tables = 0

        def _report(rel):
            """每处理完一张文本表就回报一次：已处理张数 / 总张数 + 累计翻译量。"""
            self.progress({"file": rel, "file_idx": done_n, "files": n_tables,
                           "ids": trans_total, "total_ids": ent_total,
                           "tabs": trans_tables, "pending": self.pending_files,
                           "reqs": self.req_count, "unit": "个文件"})

        if self.parallel > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            self.log("多路翻译：%d 路并发处理 %d 张文本表（每路 %d token）"
                     % (self.parallel, n_tables, self.ctx_size), "ok")
            results = []
            with ThreadPoolExecutor(max_workers=self.parallel) as pool:
                futures = [pool.submit(_one, item) for item in items]
                for fut in as_completed(futures):
                    if self._cancelled():
                        for f in futures:
                            f.cancel()
                        self.log("已取消，已处理的文本表保留在输出目录")
                        return False
                    idx, rel, st = fut.result()
                    done_n += 1
                    trans_total += st["translated"]
                    ent_total += st.get("total", 0)
                    if st["translated"]:
                        trans_tables += 1
                    results.append((idx, rel, st))
                    _report(rel)
            results.sort(key=lambda x: x[0])
        else:
            results = []
            for item in items:
                if self._cancelled():
                    self.log("已取消，已处理的文本表保留在输出目录")
                    return False
                idx, rel, st = _one(item)
                done_n += 1
                trans_total += st["translated"]
                ent_total += st.get("total", 0)
                if st["translated"]:
                    trans_tables += 1
                results.append((idx, rel, st))
                _report(rel)

        for idx, rel, st in results:
            translated += st["translated"]
            skipped += st["skipped"]
            failed += st["failed"]
            changed += 1 if st["changed"] else 0
            copied += 1 if st["copied"] else 0
            if idx == 1 or idx == n_tables or idx % 20 == 0:
                self.log("文本表 %d/%d（%d%%）：%s"
                         % (idx, n_tables, 100 * idx // n_tables, rel))
        copied_refs = self._copy_reference_layer(done_dir)
        lit = {"files": 0, "entries": 0}
        if self.translate_literals:
            lit = self._run_literal_layer(done_dir)
        self.empty_files = sum(1 for t in self._table_entries.values()
                               if not t)
        self.log("引用层完成：改写文本表 %d 个，原样复制 %d 个；"
                 "复制引用/配置 %d 个；译 %d 条，跳过噪声 %d 条，失败 %d 条；"
                 "脚本/XML 字面文本：改写 %d 个文件 / %d 条"
                 % (changed, copied, copied_refs, translated, skipped, failed,
                    lit["files"], lit["entries"]), "ok")
        return True

    def _run_literal_layer(self, done_dir):
        """把脚本/XML 里**不是 ID** 的字面文本抽到新表并注册。

        文本表只解决“ID → 正文”这一层；脚本和 UI XML 里仍有直接写着的
        俄文/英文（补丁的 scripts.xml / ui.xml / gameplay.xml 就是这些）。
        不处理它们，游戏里就会看到俄文或 ID。这里复用旧的提取器：
        字面文本 → 新 ID，原文件只换 ASCII ID，新表 UTF-8。
        """
        prefix = "sgtat_gen_text_%d_" % int(time.time())
        gen = [0]
        extract = {}
        written = 0
        total = 0
        targets = []
        for full in self._iter_files():
            rel = os.path.relpath(full, self.in_dir)
            kind = self._classify_file(full, rel)
            if kind in ("cfg", "script"):
                targets.append((kind, rel, full))
        targets.sort(key=lambda x: x[1])
        for kind, rel, full in targets:
            if self._cancelled():
                self.log("已取消：字面文本层已处理的文件保留在输出目录")
                break
            out_path = os.path.join(done_dir, rel)
            old_mode = self.mode
            self.mode = "cfgxml" if kind == "cfg" else "scriptE"
            try:
                rep = self._process_non_text(full, {}, extract, prefix, gen)
            except Exception as e:
                self.failed_strings += 1
                self.detail("  字面文本提取失败：%s（%s: %s）"
                            % (rel, type(e).__name__, e))
                rep = None
            finally:
                self.mode = old_mode
            if rep is None:
                continue
            whole, rep_dict, enc = rep
            if not rep_dict:
                continue
            try:
                self._write_atomic(
                    out_path,
                    lambda p: self.p.generateOutputFileFromString(
                        p, whole, needXmlHeader=False,
                        encoding=enc or "utf-8"))
            except Exception as e:
                self.failed_strings += 1
                self.detail("  字面文本回写失败：%s（%s: %s）"
                            % (rel, type(e).__name__, e))
                continue
            written += 1
            total += len(rep_dict)
            self._record_done(rel, full, out_path)
            if written % 20 == 0:
                self.log("字面文本：已改写 %d 个文件 / %d 条…"
                         % (written, total))
        if extract:
            name = "sgtat_auto_texts"
            table_path = os.path.join(done_dir, "configs", "text",
                                      name + ".xml")
            self._write_atomic(
                table_path,
                lambda p: self.p.generateOutputXml(p, extract,
                                                   splitLength=1000))
            self._register_localization(done_dir, name)
            self.log("字面文本表：%s（%d 条），已写入 localization.ltx"
                     % (table_path, len(extract)), "ok")
        return {"files": written, "entries": total}

    def _register_localization(self, done_dir, table_name):
        """把生成的文本表名追加到 configs/localization.ltx 的 files 行。"""
        src = os.path.join(self.in_dir, "configs", "localization.ltx")
        out = os.path.join(done_dir, "configs", "localization.ltx")
        if os.path.isfile(src):
            text = read_text_any(src)
        else:
            text = ("[string_table]\nlanguage = %s\nfont_prefix =\n"
                    % (self._lang_hint or "rus"))
        lines = text.splitlines()
        found = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.lower().startswith("files") and "=" in s:
                key, value = line.split("=", 1)
                names = [x.strip() for x in value.split(",") if x.strip()]
                if table_name not in names:
                    names.append(table_name)
                lines[i] = key + "= " + ", ".join(names)
                found = True
                break
        if not found:
            lines.extend(["", "files = " + table_name])
        parent = os.path.dirname(out) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, out)

    @staticmethod
    def _entry_style(rel, src):
        low = (rel or "").lower().replace(os.sep, "/")
        if "dialog" in low:
            return "dialogue"
        if ("/ui/" in low or "ui_" in low or "items" in low):
            return "label"
        s = (src or "").strip()
        if len(s) <= 48 and not re.search(r"[.!?。！？；;:]", s):
            return "label"
        if ("encyclopedia" in low or "news" in low
                or "enc_" in low):
            return "formal"
        return "dialogue"

    def _scene_context(self, metas, idx):
        """取同表前后各 2 条源文作场景上下文（最多 240 字符）。"""
        parts = []
        for j in range(idx - 1, max(-1, idx - 3), -1):
            src = (metas[j].get("src") or "").strip()
            if src:
                parts.insert(0, "前一句：" + src)
        for j in range(idx + 1, min(len(metas), idx + 3)):
            src = (metas[j].get("src") or "").strip()
            if src:
                parts.append("后一句：" + src)
        text = "\\n".join(parts)
        return text[:240]

    def _parse_numbered_batch(self, out, n):
        found = {}
        for line in (out or "").splitlines():
            m = re.match(r"^\s*\[(\d+)\]\s*(.*)$", line)
            if not m:
                continue
            i = int(m.group(1))
            if 1 <= i <= n and i not in found:
                found[i] = m.group(2).strip()
        if len(found) != n or any(not found.get(i) for i in range(1, n + 1)):
            return None
        return [found[i] for i in range(1, n + 1)]

    def _translate_dialogue_batch(self, batch):
        """批量翻译 2～4 条连续短对话；失败返回 {}，交由逐条路径。"""
        # ★ 2.0.24（IWP 全量审计 D1）：批量路径与单条路径同等保护——
        # 送翻前 mask 运行期占位符（%c[default]、$clr、%s…），译后逐行
        # 还原并校验；任何不一致整批丢弃交回单条路径（那里有完整闸门）。
        # 旧实现直接送原文，实测 16 条 %c[default]→%c[默认]、$clr_2REMOVE
        # 被拆译、色码元组内插换行——引擎按未知色名/坏变量渲染，运行期破坏。
        lines = []
        mappings = []
        for x in batch:
            masked, mapping = mask_runtime_tokens(x["src"])
            lines.append(masked)
            mappings.append(mapping)
        pairs = []
        seen = set()
        for x in batch:
            for k, v in self._glossary_hits(x["src"]):
                if k not in seen:
                    seen.add(k)
                    pairs.append((k, v))
        with self._stat_lock:
            self.batch_stats["batches"] += 1
            self.req_count += 1
            self.trans_chars += sum(len(x["src"]) for x in batch)
        try:
            out = self.srv.translate_batch(
                lines, batch[0]["lang"], self.tgt,
                glossary_hits=pairs or None,
                temperature=self.dialogue_temp)
        except Exception:
            self.batch_stats["failed"] += 1
            return {}
        parsed = self._parse_numbered_batch(out, len(batch))
        if not parsed:
            self.batch_stats["failed"] += 1
            return {}
        # ★ 2.0.24（IWP 全量审计 D11）：批处理错位签名。模型把上一行的
        # 译文复制/顺延到下一行（实测 agr_technic_dialogs_737 拿到 735
        # 源文的另一种译法、本条一字未译）——行数一致、长度比 0.23 也
        # 高于闸门下限，唯一可靠信号是「不同源文、完全相同译文」。
        # 命中任何一对即整批丢弃，交单条路径逐条重翻。
        # 短应答豁免：「是的。/好的。」类问候语不同源文同译是正常收敛，
        # 只有 ≥30 字的整句译文重复才可能是错位（实测错位样本 ~90 字）。
        for _i in range(len(parsed)):
            for _j in range(_i + 1, len(parsed)):
                if (len(parsed[_i].strip()) >= 30
                        and parsed[_i].strip() == parsed[_j].strip()
                        and batch[_i]["src"].strip()
                        != batch[_j]["src"].strip()):
                    self.batch_stats["failed"] += 1
                    return {}
        result = {}
        for x, mapping, text in zip(batch, mappings, parsed):
            text = clean_translation_output(text)
            if mapping:
                text = restore_runtime_tokens(text, mapping)
                if text is None:
                    # 掩码丢失/重复/被改动：整批不可信，回退单条路径
                    self.batch_stats["failed"] += 1
                    return {}
            if text.count("\\n") != x["src"].count("\\n"):
                self.batch_stats["failed"] += 1
                return {}
            if (self.tgt == "chs"
                    and missing_big_numbers(x["src"], text)):
                self.batch_stats["failed"] += 1
                return {}
            if self.tgt == "chs" and find_glue_residue(text):
                # 粘连残词交单条路径重试（批量里无法定向重试）
                self.batch_stats["failed"] += 1
                return {}
            text = self._repair_residual_source_terms(x["src"], text)
            text = self._repair_residual_latin_names(x["src"], text)
            xlang = x["lang"]
            if xlang in ("auto", "text", "rus", "eng"):
                det = detect_text_lang(x["src"])
                if det:
                    xlang = det
            text = self._repair_known_semantic_terms(
                x["src"], text, xlang)
            if self._entry_style(x["rel"], x["src"]) == "dialogue":
                text = style_cleanup_cn(text)
            if (not text or not has_cjk(text)
                    or text.strip() == x["src"].strip()
                    or looks_like_prompt_echo(text)
                    or (has_source_profanity(x["src"])
                        and not has_zh_profanity(text))):
                self.batch_stats["failed"] += 1
                return {}
            lead = x["src"][:len(x["src"]) - len(x["src"].lstrip())]
            trail = x["src"][len(x["src"].rstrip()):]
            result[x["id"]] = (x["lang"], lead + text.strip() + trail)
        self.batch_stats["lines"] += len(batch)
        return result

    def _translate_table(self, full, out_path, rel):
        st = {"translated": 0, "skipped": 0, "failed": 0,
              "changed": False, "copied": False}
        try:
            ents = self._parse(self.p.parse_xray_text_xml, full,
                               read_encoding_chain(full))
        except Exception as e:
            st["failed"] += 1
            self.detail("  文本表解析失败：%s（%s）" % (rel, e))
            return st
        if not ents:
            self._copy_file(full, out_path)
            st["copied"] = True
            self._record_done(rel, full, out_path)
            return st
        # 多路并发时表级 TM 的 _tm_local_enabled/_tm_cache 是实例状态，
        # 会被其他 worker 覆盖；只保留任务级全局 TM（在并发前已算好）。
        if self.tm_lite and not self._tm_global_enabled and self.parallel == 1:
            tm_values = []
            for ent in ents:
                _lang, src = self._pick_source(ent)
                if not src or not src.strip():
                    continue
                if self._looks_like_target(src) or not _has_translatable_text(src):
                    continue
                if (looks_like_code_or_debug(rel, src)
                        or looks_like_generated_noise(rel, src)
                        or looks_like_technical_text(src)
                        or (looks_like_identifier_text(src)
                            and not is_player_facing_label(rel, src))):
                    continue
                if self.protect_proper and looks_like_proper_text(rel, src):
                    continue
                tm_values.append(src)
            self._tm_enable_table(tm_values, rel)
        changes = {}
        metas = []
        for ent in ents:
            lang0, src0 = self._pick_source(ent)
            metas.append({
                "id": ent.id, "ent": ent, "lang": lang0, "src": src0,
                "rel": rel, "style": self._entry_style(rel, src0 or ""),
            })
        st["total"] = len(metas)
        batched_ids = set()
        # ★ 2.0.13：批量不再因 force_glossary 整体关闭。原实现里只要开着
        # 强制术语，dialog 表就全走逐条——实测请求数翻倍是速度主因之一。
        # 术语安全由下面 _ok() 保证：含 hard 术语/占位符/词库整条命中的条目
        # 一律踢出批次走逐条，批量只处理「无硬术语的连续短对话」。
        if "dialog" in rel.lower():
            def _ok(meta):
                src0 = meta["src"]
                if not src0 or not src0.strip():
                    return False
                if len(src0) < 4 or len(src0) > 220 or chr(10) in src0:
                    return False
                if _RUNTIME_TOKEN_RE.search(src0):
                    return False
                # 含 hard 术语的句子不进批量：批量只用提示，容易丢术语；
                # 交给逐条路径（占位符/校验更完整）。
                if (self.gloss_hard is not None
                       and any(k in self.gloss_hard
                               for k, _v in self._glossary_hits(src0))):
                    return False
                if self._looks_like_target(src0) or not _has_translatable_text(src0):
                    return False
                if src0.strip() in self.glossary:
                    return False
                if (looks_like_code_or_debug(rel, src0)
                        or looks_like_generated_noise(rel, src0)
                        or looks_like_technical_text(src0)
                        or (looks_like_identifier_text(src0)
                            and not is_player_facing_label(rel, src0)
                            and not _id_player_visible(meta["id"]))):
                    return False
                if self.only_referenced and meta["id"] not in self._ref_counts:
                    return False
                # ★ 2.0.7：术语库整条命中的条目必须进批次（写入点直接取译名）
                if src0 not in self.glossary \
                        and termbase_lookup(src0) is not None:
                    return True
                if self.protect_proper and looks_like_proper_text(rel, src0):
                    return False
                return True
            i = 0
            while i < len(metas):
                if not _ok(metas[i]):
                    i += 1
                    continue
                batch = [metas[i]]
                total = len(metas[i]["src"])
                j = i + 1
                while (j < len(metas) and len(batch) < 4
                       and _ok(metas[j])
                       and metas[j]["lang"] == metas[i]["lang"]
                       and total + len(metas[j]["src"]) <= 700):
                    batch.append(metas[j])
                    total += len(metas[j]["src"])
                    j += 1
                if len(batch) >= 2:
                    got = self._translate_dialogue_batch(batch)
                    if got:
                        changes.update(got)
                        batched_ids.update(got)
                        st["translated"] += len(got)
                        i = j
                        continue
                i += 1
        for meta_idx, meta in enumerate(metas):
            ent = meta["ent"]
            if ent.id in batched_ids:
                continue
            if self._cancelled():
                break
            lang, src = meta["lang"], meta["src"]
            if not src or not src.strip():
                continue
            if self._looks_like_target(src):
                continue
            exact = src.strip()
            if (exact in self.glossary
                    and (exact not in self.session_only_keys
                         or is_player_facing_label(rel, src))):
                # 制作名单/音乐/武器短名即使命中了核心词条，也要服从
                # “保护专名”；否则 credits 里的 Harris 会被改写成中文。
                if not (self.protect_proper
                        and looks_like_hard_protected_file(rel)):
                    lead = src[:len(src) - len(src.lstrip())]
                    trail = src[len(src.rstrip()):]
                    new_exact = lead + self.glossary[exact] + trail
                    if new_exact != src:
                        changes[ent.id] = (lang, new_exact)
                        st["translated"] += 1
                    continue
            if not _has_translatable_text(src):
                continue
            if looks_like_code_or_debug(rel, src):
                # ★ 2.0.22：全大写型号/弹药规格且词库有整串中文译名时直取，
                # 不再被 code 过滤整条拦掉（见 _is_model_code_form 说明）。
                _mc = (termbase_exact_translation(src)
                       if (src not in self.glossary
                           and not looks_like_hard_protected_file(rel)
                           and _is_model_code_form(rel, src)) else None)
                if _mc is not None and _mc != src:
                    changes[ent.id] = (lang, _mc)
                    st["translated"] += 1
                    continue
                st["skipped"] += 1
                continue
            # ★ 2.0.21（全 MOD 重放审计）：术语直取/句式直译提前到
            # noise/tech/identifier 判定之前——词库整串命中 = 明确的
            # 翻译意图，任何筛选都不该拦（实测弹药名 '12x76 "Arrow"
            # slugs' 被 technical 跳过、WOT 升级名被 identifier 跳过，
            # 而词库均有同形态译名）。credits/音乐等强保护文件除外。
            if not looks_like_hard_protected_file(rel):
                _exact = termbase_exact_translation(src) \
                    if src not in self.glossary else None
                if _exact is not None and _exact != src:
                    changes[ent.id] = (lang, _exact)
                    st["translated"] += 1
                    continue
                # ★ 2.0.13 通用新闻句式：固定英文句式 + 词库名词零成本直译。
                if src not in self.glossary:
                    _pat = pattern_translate(src, termbase_exact_translation)
                    if _pat is not None:
                        changes[ent.id] = (lang, _pat)
                        st["translated"] += 1
                        continue
            if (looks_like_generated_noise(rel, src)
                    or looks_like_technical_text(src)
                    or (looks_like_identifier_text(src)
                        and not is_player_facing_label(rel, src)
                        and not _id_player_visible(ent.id))):
                st["skipped"] += 1
                continue
            if self.only_referenced and ent.id not in self._ref_counts:
                continue
            if self.protect_proper and looks_like_proper_text(rel, src):
                # ★ 2.0.21：保护命中先试硬层子串替换（Vasya "Boar"→
                # 瓦西娅"野猪" 形态），词库无可用词才保留原文。
                _p = (partial_glossary_translation(src, rel)
                      if src not in self.glossary else None)
                if _p is not None and _p != src:
                    changes[ent.id] = (lang, _p)
                    st["translated"] += 1
                else:
                    st["skipped"] += 1
                continue
            use_lang = self.src_lang if self.src_lang != "auto" else lang
            # 字段名不可靠：不少 MOD 把英语放进 <rus>，或把俄语放进 <text>。
            # 即使显式选了 rus/eng，正文明显是另一种语言时也按正文判断；
            # ukr 不覆盖（俄/乌都是西里尔，粗判分不出来）。
            if use_lang in ("auto", "text", "rus", "eng"):
                det = detect_text_lang(src)
                if det and det != use_lang:
                    use_lang = det
            ctx = ""
            if meta["style"] == "dialogue" and "dialog" in rel.lower():
                ctx = self._scene_context(metas, meta_idx)
            try:
                out = self._translate_one(
                    src, use_lang, context=ctx, style=meta["style"])
            except Exception as e:
                st["failed"] += 1
                self.detail("  条目翻译失败，保留原文：%s（%s: %s）"
                            % (ent.id, type(e).__name__, e))
                continue
            if not out or not out.strip():
                continue
            out = clean_translation_output(out)
            lead = src[:len(src) - len(src.lstrip())]
            trail = src[len(src.rstrip()):]
            new = lead + out.strip() + trail
            if new == src:
                continue
            changes[ent.id] = (lang, new)
            st["translated"] += 1
        if not changes:
            self._copy_file(full, out_path)
            st["copied"] = True
            self._record_done(rel, full, out_path)
            return st
        try:
            open(full, "rb").read().decode("utf-8-sig")
            utf8_table = True
        except (OSError, UnicodeDecodeError):
            utf8_table = False
        if utf8_table:
            wrote = self._write_table_changes(full, out_path, changes, rel)
        else:
            # 老 MOD 常见 cp1251 表；中文无法用 cp1251 写回，转成 UTF-8
            # 并重写声明。会丢注释/多语言标签，但不会漏译。
            wrote = self._write_table_as_utf8(full, out_path, ents,
                                             changes, rel)
        if wrote:
            st["changed"] = True
            self._audit_tables.add(rel)   # ★ 2.0.2：登记进终检清单
            self._record_done(rel, full, out_path)
        else:
            st["copied"] = True
        return st

    def _write_table_as_utf8(self, full, out_path, ents, changes, rel):
        """非 UTF-8 文本表：保持 <rus>/<eng>/<tur> 结构转 UTF-8。

        不能走 generateOutputXml：它只写 <text>，会把原文的 <rus>/<eng>
        标签全部丢掉（实测这个 MOD 的 stable_dialogs 从 52 KB 缩到 24 KB，
        引擎按 rus 读时就可能读不到中文）。这里逐条改 <rus> 内容，
        只把 XML 声明的编码换成 utf-8。
        """
        try:
            raw = open(full, "rb").read()
        except OSError:
            return False
        text = None
        had_bom = raw.startswith(b"\xef\xbb\xbf")
        for enc in ("utf-8-sig", "utf-8", "cp1251", "cp1252"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("utf-8", "replace")
        applied = missed = 0
        for eid, (lang, new_text) in changes.items():
            text, ok = self._patch_table_text(text, eid, lang, new_text)
            if ok:
                applied += 1
            else:
                missed += 1
        if applied == 0:
            self.log("文本表条目定位失败，保留原文：%s" % rel, "warn")
            self._copy_file(full, out_path)
            return False
        if missed:
            self.detail("  %s：%d 条已改写，%d 条定位失败（保留原文）"
                        % (rel, applied, missed))
        text = re.sub(
            r'(<\?xml[^>]*?\bencoding\s*=\s*["\'])[^"\']+(["\'])',
            r"\1utf-8\2", text, count=1, flags=re.I)
        if not text.lstrip().startswith("<?xml"):
            text = '<?xml version="1.0" encoding="utf-8"?>\n' + text
        data = (b"\xef\xbb\xbf" if had_bom else b"") + text.encode("utf-8")
        parent = os.path.dirname(out_path) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = out_path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out_path)
        except OSError as e:
            self.log("文本表写入失败（保留原文）：%s（%s）" % (rel, e), "err")
            self._copy_file(full, out_path)
            return False
        self.log("文本表转 UTF-8 并保留语言标签：%s" % rel, "warn")
        return True

    @staticmethod
    def _patch_table_text(text, eid, lang, new_text):
        tag = {"text": "text", "rus": "rus", "eng": "eng",
               "ukr": "ukr", "tur": "tur"}.get(lang, "text")
        block_re = re.compile(
            r"(<string\b[^>]*\bid\s*=\s*[\"']%s[\"'][^>]*>)(.*?)"
            r"(</string>)" % re.escape(eid), re.S)
        m = block_re.search(text)
        if not m:
            return text, False
        inner = m.group(2)
        tag_re = re.compile(r"(<%s\b[^>]*>)(.*?)(</%s>)" % (tag, tag), re.S)
        tm = tag_re.search(inner)
        # ★ 2.0.13 空白保真：源 <text> 内容的前导/尾随空白（缩进、换行）
        # 属于排版约定，模型译文常把它们抹平（实测 ui_hint_localization_rus
        # 丢前导空格）。这里把源内容的首尾空白套回译文，避免游戏内错位。
        src_body = tm.group(2) if tm else ""
        lead = src_body[:len(src_body) - len(src_body.lstrip())]
        trail = src_body[len(src_body.rstrip()):]
        new_body = lead + str(new_text).strip() + trail
        esc = (new_body.replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;"))
        if tm:
            inner2 = inner[:tm.start(2)] + esc + inner[tm.end(2):]
        else:
            inner2 = (inner.rstrip() + "\n\t\t<%s>%s</%s>\n\t"
                      % (tag, esc, tag))
        return text[:m.start(2)] + inner2 + text[m.end(2):], True

    def _write_table_changes(self, full, out_path, changes, rel):
        try:
            raw = open(full, "rb").read()
        except OSError:
            return False
        had_bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            self.log("非 UTF-8 文本表暂不原地改写（保留原文）：%s" % rel,
                     "warn")
            self._copy_file(full, out_path)
            return False
        applied = missed = 0
        for eid, (lang, new_text) in changes.items():
            text, ok = self._patch_table_text(text, eid, lang, new_text)
            if ok:
                applied += 1
            else:
                missed += 1
        if applied == 0:
            self.log("文本表条目定位失败，保留原文：%s" % rel, "warn")
            self._copy_file(full, out_path)
            return False
        if missed:
            self.detail("  %s：%d 条已改写，%d 条定位失败（保留原文）"
                        % (rel, applied, missed))
        # ★ 2.0.11：UTF-8 表原地改写时同步把 XML 声明的 encoding 改成 utf-8。
        # 旧逻辑只写字节不改声明：源码声明 windows-1251、内容却是 UTF-8 中文，
        # 引擎若按声明解码就会乱码（实测 IWP 输出 5/5 张表声明不符）。
        text = re.sub(
            r'(<\?xml[^>]*?\bencoding\s*=\s*["\'])[^"\']+(["\'])',
            r"\1utf-8\2", text, count=1, flags=re.I)
        if not text.lstrip().startswith("<?xml"):
            text = '<?xml version="1.0" encoding="utf-8"?>\n' + text
        data = (b"\xef\xbb\xbf" if had_bom else b"") + text.encode("utf-8")
        parent = os.path.dirname(out_path) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = out_path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out_path)
        except OSError as e:
            self.log("文本表写入失败（保留原文）：%s（%s）" % (rel, e), "err")
            self._copy_file(full, out_path)
            return False
        return True

    def _copy_reference_layer(self, done_dir):
        if not self.copy_reference:
            return 0
        copied = 0
        for full in sorted(self._ref_files):
            rel = os.path.relpath(full, self.in_dir)
            out = os.path.join(done_dir, rel)
            if os.path.isfile(out) and _files_equal(full, out):
                continue
            try:
                self._copy_file(full, out)
                copied += 1
            except Exception as e:
                self.detail("  复制引用层失败：%s（%s）" % (rel, e))
        # 顶层 configs/*.ltx：localization.ltx 是文本表注册表，
        # external.ltx/fonts.ltx 等是解析/字体配置；补丁把它们算作有用文件。
        # 原样复制（不改内容），保证“不漏文件”。
        cfg = os.path.join(self.in_dir, "configs")
        if os.path.isdir(cfg):
            for fn in sorted(os.listdir(cfg)):
                if not fn.lower().endswith(".ltx"):
                    continue
                full = os.path.join(cfg, fn)
                if not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, self.in_dir)
                out = os.path.join(done_dir, rel)
                if os.path.isfile(out) and _files_equal(full, out):
                    continue
                try:
                    self._copy_file(full, out)
                    copied += 1
                except Exception:
                    pass
        if self._unresolved:
            try:
                path = os.path.join(done_dir, "待确认_未解析引用.csv")
                with open(path, "w", encoding="utf-8-sig",
                          newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["id", "count"])
                    for key, count in sorted(
                            self._unresolved.items(),
                            key=lambda kv: (-kv[1], kv[0])):
                        writer.writerow([key, count])
            except Exception:
                pass
        return copied

    def _copy_file(self, src, dst):
        parent = os.path.dirname(dst) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = dst + ".tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)

    @staticmethod
    def _prune_empty_dirs(root):
        """自底向上删除不含任何文件的空目录；保留根目录本身。
        返回删除的目录数。"""
        removed = 0
        if not os.path.isdir(root):
            return 0
        for cur, dirs, files in os.walk(root, topdown=False):
            if os.path.abspath(cur) == os.path.abspath(root):
                continue
            try:
                if not os.listdir(cur):
                    os.rmdir(cur)
                    removed += 1
            except OSError:
                pass
        return removed

    def _classify_file(self, full, rel):
        """文件类型判定（转发到模块级 classify_xray_file，与勘察共用一处口径）。"""
        return classify_xray_file(full, rel)

    def _run_auto(self, done_dir, reuse):
        temp_dir = os.path.join(done_dir, "__temp__")
        os.makedirs(temp_dir, exist_ok=True)
        reuse.update(self._read_existing(temp_dir))

        targets = []
        for root, _dirs, files in self._pruned_walk():
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, self.in_dir)
                kind = self._classify_file(full, rel)
                if kind:
                    targets.append((kind, rel, full))
        targets.sort(key=lambda x: x[1])
        total = len(targets)
        counts = {}
        for kind, _, _ in targets:
            counts[kind] = counts.get(kind, 0) + 1
        self.log("识别到可翻译文件 %d 个：文本XML %d / 配置XML %d / ltx %d / 脚本 %d"
                 % (total, counts.get("text", 0), counts.get("cfg", 0),
                    counts.get("ltx", 0), counts.get("script", 0)))

        extract = {}
        prefix = "sgtat_gen_text_%d_" % int(time.time())
        gen = [0]
        chars_all = set()
        written = 0
        failed = 0
        trans_files = 0
        for idx, (kind, rel, full) in enumerate(targets, 1):
            if self._cancelled():
                self.log("已取消，已完成文件保留在输出目录")
                return False
            out_path = os.path.join(done_dir, rel)
            if self._should_skip(rel, full, out_path, kind):
                continue
            self._log_progress(idx, total, rel)
            self.progress({"file": rel, "file_idx": idx, "files": total,
                           "ids": trans_files, "total_ids": 0,
                           "tabs": trans_files, "pending": self.pending_files,
                           "reqs": self.req_count, "unit": "个文件"})
            if kind == "text":
                tmp = os.path.join(temp_dir, rel)
                try:
                    n_texts = self._run_text_file(
                        os.path.basename(full), full, out_path,
                        tmp, reuse, idx, total)
                except Exception as e:
                    failed += 1
                    self.log("文件处理失败，已跳过：%s（%s: %s）"
                             % (rel, type(e).__name__, e), "err")
                    continue
                if n_texts > 0:
                    written += 1
                    trans_files += 1
                    self._record_done(rel, full, out_path)
            else:
                mode = {"cfg": "cfgxml", "ltx": "ltx",
                        "script": "scriptL"}[kind]
                old_mode = self.mode
                self.mode = mode
                try:
                    rep = self._process_non_text(
                        full, reuse, extract, prefix, gen)
                except Exception as e:
                    failed += 1
                    self.log("文件处理失败，已跳过：%s（%s: %s）"
                             % (rel, type(e).__name__, e), "err")
                    continue
                finally:
                    self.mode = old_mode
                if rep is None:
                    continue
                whole_text, rep_dict, enc = rep
                if kind == "script":
                    if "chars_all_in" in extract:
                        chars_all |= extract["chars_all_in"]
                if rep_dict or self.output_anyway:
                    if kind in ("cfg", "ltx"):
                        self._write_atomic(
                            out_path,
                            lambda p: self.p.generateOutputFileFromString(
                                p, whole_text, needXmlHeader=False,
                                encoding=enc or "utf-8"))
                    else:
                        self._write_atomic(
                            out_path,
                            lambda p: self.p.generateOutputFileFromString(
                                p, whole_text, needXmlHeader=False))
                    written += 1
                    self._record_done(rel, full, out_path)

        if "chars_all_in" in extract:
            del extract["chars_all_in"]
        if chars_all:
            chars = "".join(chars_all)
            for i, pie in enumerate(self.p.splitTextToPiecesAtLength(chars, 500)):
                extract["text_gen_for_font_%d" % i] = pie
        if extract:
            self._write_atomic(
                os.path.join(done_dir, "___auto_put_this_to_text_folder.xml"),
                lambda p: self.p.generateOutputXml(p, extract, splitLength=1000))
        self.log("已写出文件 %d 个（镜像原目录结构，格式保持不变）" % written)
        if failed:
            self.log("有 %d 个文件处理失败（已跳过，其余照常完成），详见日志"
                     % failed, "warn")
        return True

    def _flush_temp(self, temp_path, done):
        """把当前进度写进 __temp__：取消/中断后重跑可直接复用已译部分。"""
        if not done:
            return
        try:
            self._write_atomic(
                temp_path,
                lambda p: self.p.generateOutputXml(p, done, splitLength=1000))
            self._temp_clock = time.time()
        except Exception as e:
            self.detail("  临时进度写入失败：%s: %s" % (type(e).__name__, e))

    def _run_text_file(self, fn, full, out_path, temp_path, reuse, idx, total):
        """处理单个文本 XML；返回条目数，取消时返回 -1（区别于"空文件"的 0）。"""
        chain = read_encoding_chain(full)
        texts = self._parse(self.p.parse_xray_text_xml, full, chain)
        self.detail("  共 %d 条文本" % len(texts))
        if not texts:
            if os.path.exists(out_path):
                if self._is_stale_empty_output(out_path):
                    try:
                        os.remove(out_path)
                        self.detail("  已清理空占位输出：%s" % out_path)
                    except OSError:
                        pass
                else:
                    self.log("  未解析出条目，但既有译文有内容，已保留：%s"
                             "（确认是空文件请手动删除）" % out_path, "warn")
            return 0
        if chain and chain[0] not in _ENC_UTF8:
            samples = []
            for ent in texts[:20]:
                samples.extend((ent.texts or {}).values())
            self._warn_mojibake(fn, samples)
        done = {}
        self._temp_clock = time.time()
        metas = []
        for ent in texts:
            lang0, src0 = self.p.getRecommendLangText(ent, self.tgt)
            metas.append({
                "id": ent.id, "ent": ent, "lang": lang0,
                "src": src0 if isinstance(src0, str) else "",
                "rel": full,
                "style": self._entry_style(full, src0 or ""),
            })
        batched = set()
        if "dialog" in full.lower() and not self.force_glossary:
            def _ok(meta):
                src0 = meta["src"]
                if not src0 or not src0.strip() or meta["id"] in reuse:
                    return False
                if len(src0) < 4 or len(src0) > 220 or chr(10) in src0:
                    return False
                if _RUNTIME_TOKEN_RE.search(src0):
                    return False
                # 含 hard 术语的句子不进批量：批量只用提示，容易丢术语；
                # 交给逐条路径（占位符/校验更完整）。
                if (self.gloss_hard is not None
                       and any(k in self.gloss_hard
                               for k, _v in self._glossary_hits(src0))):
                    return False
                if self._looks_like_target(src0) or not _has_translatable_text(src0):
                    return False
                if (looks_like_code_or_debug(full, src0)
                        or looks_like_generated_noise(full, src0)
                        or looks_like_technical_text(src0)
                        or (looks_like_identifier_text(src0)
                            and not is_player_facing_label(rel, src0)
                            and not _id_player_visible(meta["id"]))):
                    return False
                if self.only_referenced and meta["id"] not in self._ref_counts:
                    return False
                if termbase_lookup(src0) is not None:
                    return True
                if self.protect_proper and looks_like_proper_text(full, src0):
                    return False
                return True
            i = 0
            while i < len(metas):
                if not _ok(metas[i]):
                    i += 1
                    continue
                batch = [metas[i]]
                total = len(metas[i]["src"])
                j = i + 1
                while (j < len(metas) and len(batch) < 4
                       and _ok(metas[j])
                       and metas[j]["lang"] == metas[i]["lang"]
                       and total + len(metas[j]["src"]) <= 700):
                    batch.append(metas[j])
                    total += len(metas[j]["src"])
                    j += 1
                if len(batch) >= 2:
                    got = self._translate_dialogue_batch(batch)
                    if got:
                        for eid, (_lang, new) in got.items():
                            done[eid] = new
                        batched.update(got)
                        i = j
                        continue
                i += 1
        for n, ent in enumerate(texts, 1):
            if self._cancelled():
                self._flush_temp(temp_path, done)
                self.log("已取消：本文件已完成 %d/%d 条（已存入 __temp__，"
                         "重跑可复用）" % (len(done), len(texts)))
                return -1
            if ent.id in self._past:
                if ent.id not in self.redundant:
                    self.redundant.append(ent.id)
            else:
                self._past.add(ent.id)
            if ent.id in batched:
                continue
            if ent.id in reuse:
                done[ent.id] = reuse[ent.id]
                continue
            lang, src = self.p.getRecommendLangText(ent, self.tgt)
            if self.tgt == lang:
                done[ent.id] = src
            elif src and lang == "text" and self.src_lang == "auto" \
                    and self._looks_like_target(src):
                done[ent.id] = src
            elif src:
                use_lang = self.src_lang if self.src_lang != "auto" else lang
                # 字段名不可靠：不少 MOD 把英语放进 <rus>，或把俄语放进 <text>。
                # 自动模式下按本条正文的字母构成覆盖字段判断。
                if use_lang in ("auto", "text", "rus", "eng"):
                    det = detect_text_lang(src)
                    if det and det != use_lang:
                        use_lang = det
                style = self._entry_style(full, src)
                ctx = ""
                if style == "dialogue" and "dialog" in full.lower():
                    ctx = self._scene_context(metas, n - 1)
                try:
                    done[ent.id] = self._translate_one(
                        src, use_lang, context=ctx, style=style)
                except Exception as e:
                    self.failed_strings += 1
                    self.detail("  条目翻译失败，保留原文：%s（%s: %s）"
                                % (ent.id, type(e).__name__, e))
                    done[ent.id] = src
            else:
                done[ent.id] = ""
            if time.time() - self._temp_clock > 120:
                self._flush_temp(temp_path, done)
            if n % 20 == 0:
                self.progress({"file": fn, "ids": n, "total_ids": len(texts),
                               "file_idx": idx, "files": total,
                               "reqs": self.req_count, "unit": "个文件"})
        if not done:
            try:
                if os.path.exists(out_path) and \
                        self._is_stale_empty_output(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return 0
        self._write_atomic(
            out_path,
            lambda p: self.p.generateOutputXml(p, done, splitLength=1000))
        self.progress({"file": fn, "ids": len(texts), "total_ids": len(texts),
                       "file_idx": idx, "files": total, "reqs": self.req_count,
                       "unit": "个文件"})
        return len(texts)

    def _pruned_walk(self):
        """遍历输入树，统一排除：输出目录（按绝对路径，即使它在输入树内或
        不叫 translated_hy_mt2）、__temp__、.git，避免把自己的产物再翻一遍。"""
        for root, dirs, files in os.walk(self.in_dir):
            kept = []
            for d in dirs:
                if d.endswith(".git") or d in ("__temp__", "translated_hy_mt2"):
                    continue
                ap = os.path.normcase(os.path.abspath(os.path.join(root, d)))
                if any(ap == ex or ap.startswith(ex + os.sep)
                       for ex in self._excluded_paths):
                    continue
                kept.append(d)
            dirs[:] = kept
            yield root, dirs, files

    def _collect_text_targets(self):
        """递归收集文本 XML（相对路径, 绝对路径），排除输出目录。"""
        targets = []
        for root, _dirs, files in self._pruned_walk():
            for fn in files:
                if fn.lower().endswith(".xml"):
                    full = os.path.join(root, fn)
                    targets.append((os.path.relpath(full, self.in_dir), full))
        targets.sort()
        return targets

    def _iter_files(self):
        for root, _dirs, files in self._pruned_walk():
            for fn in files:
                yield os.path.join(root, fn)

    def _run_recursive(self, done_dir, reuse):
        suffix = {
            "cfgxml": ".xml", "ltx": ".ltx",
            "scriptL": ".script", "scriptE": ".script",
        }.get(self.mode)
        if not suffix:
            self.log("未知文件类型：%r（应为 auto/text/cfgxml/ltx/scriptE/scriptL）"
                     % self.mode, "err")
            return False
        targets = [p for p in self._iter_files() if p.lower().endswith(suffix)]
        if self.mode == "cfgxml":
            targets = [p for p in targets
                       if os.path.basename(os.path.dirname(p)).lower() != "text"]
        total = len(targets)
        extract = {}
        text_id_prefix = "sgtat_gen_text_%d_" % int(time.time())
        gen = [0]
        self.log("待处理 %s 文件 %d 个" % (suffix, total))
        failed = 0
        for idx, full in enumerate(targets, 1):
            if self._cancelled():
                self.log("已取消")
                return False
            rel = os.path.relpath(full, self.in_dir)
            out_path = os.path.join(done_dir, rel)
            if self._should_skip(rel, full, out_path, self.mode):
                continue
            self._log_progress(idx, total, rel)
            try:
                rep = self._process_non_text(full, reuse, extract,
                                             text_id_prefix, gen)
            except Exception as e:
                failed += 1
                self.log("文件处理失败，已跳过：%s（%s: %s）"
                         % (rel, type(e).__name__, e), "err")
                continue
            if rep is None:
                continue
            whole_text, rep_dict, enc = rep
            if rep_dict or self.output_anyway:
                if self.mode in ("ltx", "cfgxml"):
                    self._write_atomic(
                        out_path,
                        lambda p: self.p.generateOutputFileFromString(
                            p, whole_text, needXmlHeader=False,
                            encoding=enc or "utf-8"))
                else:
                    self._write_atomic(
                        out_path,
                        lambda p: self.p.generateOutputFileFromString(
                            p, whole_text, needXmlHeader=False))
                self._record_done(rel, full, out_path)
        if self.mode == "scriptL" and "chars_all_in" in extract:
            chars = "".join(extract["chars_all_in"])
            extract.clear()
            for i, pie in enumerate(self.p.splitTextToPiecesAtLength(chars, 500)):
                extract["text_gen_for_font_%d" % i] = pie
        if extract:
            self._write_atomic(
                os.path.join(done_dir, "___%s__put_this_to_text_folder.xml"
                             % self.mode),
                lambda p: self.p.generateOutputXml(p, extract, splitLength=1000))
        if failed:
            self.log("有 %d 个文件处理失败（已跳过，其余照常完成），详见日志"
                     % failed, "warn")
        return True

    def _process_non_text(self, full, reuse, extract, prefix, gen):
        chain = read_encoding_chain(full)
        if self.mode == "cfgxml":
            whole, cands, enc = self._parse(
                self.p.parse_xray_cfgxml_xml, full, chain)
        elif self.mode == "ltx":
            whole, cands, enc = self._parse(
                self.p.parse_xray_ltx_file, full, chain)
        else:
            whole, cands = self._parse(
                self.p.parse_xray_script_file, full, chain)
            # 脚本按原编码写回（只把字面文本换成 ASCII 新 ID，不改编码）
            enc = "utf-8"
            for candidate_enc in chain:
                try:
                    whole.encode(candidate_enc)
                    enc = candidate_enc
                    break
                except (UnicodeEncodeError, LookupError):
                    continue
        if chain and chain[0] not in _ENC_UTF8 and looks_like_mojibake(whole):
            self.log("⚠ %s 疑为编码误判（乱码特征），请先用「编码转换」页"
                     "统一转成 UTF-8" % os.path.basename(full), "warn")
        if not cands:
            self.empty_files += 1
            if self.empty_files <= 20:
                self.detail("  %s 未提取到可译文本（%s 模式的提取规则见上游）"
                            % (os.path.basename(full), self.mode))
        prewrapped = set()
        if self.mode in ("scriptE", "scriptL"):
            # 上游提取器不认识上下文；这里只保留显示类调用/文本变量赋值，
            # 跳过 log、string_find/string_gsub、比较运算。见 script_literal_allowed。
            # 上游还会漏掉 set_level_changer_invitation / set_description /
            # AddItem(game.translate_string(...))，这里按显示调用补回。
            extra = extra_script_display_literals(whole)
            prewrapped = script_translate_literal_args(whole)
            cands = list(dict.fromkeys(list(cands) + sorted(extra)))
            cands = [c for c in cands
                     if c in extra or script_literal_allowed_in_file(whole, c)]
        if self._cancelled():
            return None
        rep = {}
        for cand in cands:
            # 这里不能按 reuse 跳过：reuse 的键是文本 XML 的 string id，
            # 与 ltx/cfg/script 的候选串不是同一命名空间；真命中了也拿不到
            # 译文，只会把原文留在输出里。非文本模式的复用由账本负责。
            if self.mode == "cfgxml":
                if cand.strip() == "" or self.p.doesTextLookLikeId(cand):
                    continue
                send = cand
                quoted = False
            elif self.mode == "ltx":
                if self.p.doesTextLookLikeId(cand):
                    continue
                quoted = len(cand) > 1 and cand.startswith('"') and cand.endswith('"')
                send = cand[1:-1] if quoted else cand
            else:
                if len(cand) < 2:
                    continue
                qch = cand[0]
                send = cand[1:-1]
                if (self.p.doesTextLookLikeId(send)
                        or self.p.doesTextLookLikeScript(send)):
                    continue
            rel = os.path.relpath(full, self.in_dir)
            if looks_like_code_or_debug(rel, send):
                continue
            if looks_like_identifier_text(send):
                continue
            # 术语直取只用于 UTF-8 的文本表；脚本/ltx 按源编码（cp1251）
            # 写回，塞中文译名会 UnicodeEncodeError，交给模型走转码路径。
            _exact = termbase_exact_translation(send) \
                if send not in self.glossary and enc.lower() in (
                    "utf-8", "utf8", "utf-8-sig") else None
            if _exact is not None and _exact != send:
                # 术语命中当"已翻译结果"，继续走后续 rep 替换流程；
                # 这里绝不能 return 单值——_process_non_text 契约是三元组。
                rep[send] = _exact
                continue
            if self.protect_proper and looks_like_proper_text(rel, send):
                # ★ 2.0.21：硬层子串替换仅限 UTF-8 表（cp1251 写回塞中文
                # 会 UnicodeEncodeError，与术语直取同口径）。
                if enc.lower() in ("utf-8", "utf8", "utf-8-sig"):
                    _p = (partial_glossary_translation(send, rel)
                          if send not in self.glossary else None)
                    if _p is not None and _p != send:
                        rep[send] = _p
                continue
            if self._cancelled():
                return None
            try:
                lang = self.src_lang
                if lang in ("auto", "text"):
                    lang = (detect_text_lang(send) or self._lang_hint
                            or "rus")
                transed = self._translate_one(send, lang)
            except Exception as e:
                self.failed_strings += 1
                self.detail("  条目翻译失败，保留原文：%s（%s: %s）"
                            % (str(send)[:40], type(e).__name__, e))
                continue
            if self.mode == "ltx" and quoted:
                transed = '"%s"' % transed
            if self.mode in ("cfgxml", "ltx", "scriptE"):
                key = prefix + str(gen[0])
                gen[0] += 1
                extract[key] = transed
                if self.mode == "scriptE":
                    # 脚本里裸 ID 是未定义全局变量（nil）；补丁统一用
                    # game.translate_string("id")。已经在 translate_string
                    # 里的字面串只换 ID，不能再套一层。
                    if cand in prewrapped:
                        # 外层已有 game.translate_string(...)，只把引号内
                        # 的字面串换成带引号的新 ID，不能退化成裸变量。
                        rep[cand] = '"%s"' % key
                    else:
                        rep[cand] = 'game.translate_string("%s")' % key
                else:
                    rep[cand] = key
            else:
                if "chars_all_in" not in extract:
                    extract["chars_all_in"] = set()
                for ch in transed:
                    extract["chars_all_in"].add(ch)
                inline = unescape_newlines(transed)
                rep[qch + self.p.escapeLiteralText(send, quote=qch) + qch] = (
                    qch + self.p.escapeLiteralText(inline, quote=qch) + qch)
        if not rep:
            return (whole, {}, enc)
        whole = self.p.replaceFromText(whole, rep)
        return (whole, rep, enc)



_UI = None          # 2.x: api.ui（register(api) 注入；模块级 helper 用）
_HOST = None        # 2.x B249+: api.host（host.http_get/http_post 小请求门面）


def _combo_get(w):
    """读下拉当前项文本（2.x `api.ui.combo_value`）。"""
    try:
        return _UI.combo_value(w) or ""
    except Exception:
        return ""


def _combo_set(w, values, idx=0):
    """重建下拉列表并让第 idx 项成为当前项。

    2.x 的 `combo_set_items` 只能重建列表、**固定把第 0 项设为当前项**
    （没有"选中第 N 项"的入口，`set_text` 也不支持 QComboBox）⇒ 这里把目标项
    轮转到首位后再设置。代价只是下拉表顺序随选中项变化，不影响取值。
    """
    values = [str(v) for v in (values or [])]
    if not values:
        return
    try:
        idx = int(idx or 0)
    except Exception:
        idx = 0
    if idx < 0 or idx >= len(values):
        idx = 0
    ordered = values[idx:] + values[:idx]
    try:
        _UI.combo_set_items(w, ordered)
    except Exception:
        pass


def _entry_get(w):
    """读单行输入框（2.x `api.ui.get_text`）。"""
    try:
        return _UI.get_text(w) or ""
    except Exception:
        return ""


def _entry_set(w, value):
    try:
        _UI.set_text(w, str(value))
    except Exception:
        pass


def _multiline_get(w):
    """读多行输入框（2.x `api.ui.get_text`，QPlainTextEdit 同一入口）。"""
    try:
        return _UI.get_text(w) or ""
    except Exception:
        return ""


def _multiline_set(w, value):
    try:
        _UI.set_text(w, str(value))
    except Exception:
        pass


def _chk_set(w, on):
    try:
        _UI.set_checked(w, bool(on))
    except Exception:
        pass


def _chk_get(w):
    try:
        return bool(_UI.checkbox_state(w))
    except Exception:
        return False


def _label_set(w, text):
    """写标签文本（2.x `api.ui.set_text` 支持 QLabel）。"""
    try:
        _UI.set_text(w, str(text))
    except Exception:
        pass


def _variant_of_label(label):
    """把后端下拉的显示文本还原成内部键（与 VARIANT_LABELS 一一对应）。"""
    label = str(label or "")
    if label.startswith("CUDA 13"):
        return "cuda13"
    if label.startswith("CUDA"):
        return "cuda"
    if label.startswith("Vulkan"):
        return "vulkan"
    return "cpu"


# 源语言下拉：显示文本带中文注解，内部只认前面的语言码
_LANG_LABELS = ["auto（按文件语言）", "eng", "rus", "ukr"]
_LANG_KEYS = ["auto", "eng", "rus", "ukr"]


def _lang_index(value):
    try:
        return _LANG_KEYS.index(str(value or "auto"))
    except ValueError:
        return 0


def _ctx_index(value):
    try:
        return [str(v) for v in CUSTOM_CTX_CHOICES].index(str(value))
    except ValueError:
        return 1


def _tier_of_label(label):
    return _LABEL_TO_TIER.get(str(label or ""), "7b")


class AiTranslateTab(object):
    def __init__(self, api, parent):
        self.api = api
        self.ui = api.ui
        self.parent = parent
        self.cfg = load_config()
        self.server = None
        self.parser = None
        self.dl_cancel = None
        self.tr_cancel = None
        self.scan_cancel = None
        self.probe_cache = {}
        self._last_ui_byte = -1
        self.log_lines = []
        # 术语分两层：builtin（随包只读）+ 用户覆盖层（可变），见 load_glossary_layers
        (self.gloss_user, self._gloss_set, self._gloss_deleted,
         self._gloss_clear) = load_glossary_layers()
        # 附加提示词必须由 **UI 线程**读控件后缓存到这里；工作线程只读这个
        # 字符串（早期工作线程直接读 tk.Text，异常被吞成空串，
        # 导致该功能实际从未生效）。
        self._prompt_extra_text = str(self.cfg.get("prompt_extra") or "")
        self._starting = False
        self._cfg_save_warned = False
        self._hw_cuda13_ok = False
        self._hw_vram_mb = 0
        self._log_pending = False
        self.scan_info = None
        self.scan_dir = None
        self._mode_plain = self.cfg.get("mode", "auto")
        # 工作线程 → UI 线程的投递队列（2.x 的控件只在 UI 线程可用）
        self._q = queue.Queue()
        # ★ 跨线程只读缓存：工作线程只读这里，绝不碰控件
        #   （2.x 有线程闸门：非 UI 线程调控件返回 None + 记 Warn，不抛异常）
        variant0 = self.cfg.get("variant")
        if variant0 not in VARIANT_ORDER:
            variant0 = VARIANT_ORDER[0]
        tier0 = self.cfg.get("tier")
        if tier0 not in TIER_ORDER:
            tier0 = TIER_ORDER[0]
        self.sel = {
            "variant": variant0,
            "tier": tier0,
            "ctx": str(self.cfg.get("custom_ctx", 8192)),
            "parallel": int(self.cfg.get("parallel", 1)),
            "mode": self.cfg.get("mode", "auto"),
            "lang": self.cfg.get("src_lang", "auto"),
        }
        # ★ 2.0.23：reference_layer/copy_reference/session_glossary/tm_lite/
        # freq_names 收编为内部固定开启——引用层是核心工作模式（关闭退回
        # 1.x 旧路径：不知引用关系、补丁不完整），完整补丁是交付物的一部分，
        # 其余三个自带门控或失败回退，关闭只损失质量不省成本，不再提供开关。
        # official_prompts 是 A/B 实验键，UI 不再展示，config.json 可手改。
        self.flags = {
            "glossary_force": bool(self.cfg.get("glossary_force", True)),
            "reference_layer": True,
            "copy_reference": True,
            "only_referenced": bool(self.cfg.get("only_referenced", False)),
            "protect_proper_v2": bool(self.cfg.get("protect_proper_v2", True)),
            "preview_reference": bool(self.cfg.get("preview_reference", False)),
            "translate_literals": bool(
                self.cfg.get("translate_literals", True)),
            "session_glossary": True,
            "tm_lite": True,
            "freq_names": True,
            "official_prompts": bool(
                self.cfg.get("official_prompts", False)),
        }

        try:
            self.parser = _load_vendor()
        except Exception:
            self.parser = None

        self._build()
        self.refresh_gloss_view()
        self.refresh_local_status()
        self.on_hw_check(silent=True)
        self._sweep_orphan_servers()
        # 上次填过待翻译目录就自动识别一次待翻译文件总数
        try:
            in_dir = self.cfg.get("in_dir") or ""
            if in_dir and os.path.isdir(in_dir):
                self._auto_scan_in_dir(in_dir)
        except Exception:
            pass

    def _sweep_orphan_servers(self):
        """后台清理上次异常退出遗留的引擎（只认本插件 exe + 父进程已死）。

        不阻塞启动：Toolhelp32 枚举通常只要几毫秒；即使失败也绝不影响
        正常流程。清到东西才写一行日志，避免每次启动都刷屏。
        """
        def work():
            try:
                killed = _kill_orphan_servers(self.cfg)
            except Exception:
                return
            if killed:
                self.ui_call(
                    self.log,
                    "已清理 %d 个上次异常退出遗留的引擎进程（%s），"
                    "显存/内存已回收"
                    % (len(killed), ", ".join("PID %d" % p for p in killed)),
                    "ok")
        threading.Thread(target=work, daemon=True).start()

    def on_hw_check(self, silent=False):
        """① 硬件检测：后台执行，给出一句话推荐并预选后端/档位。"""
        def work():
            try:
                hw = detect_hardware()
            except Exception as e:
                self.ui_call(self.log, "硬件检测失败：%s" % e, "err")
                return
            self.ui_call(self._apply_hw, hw, silent)
        threading.Thread(target=work, daemon=True).start()

    def _apply_hw(self, hw, silent=False):
        # 硬件检测是后台回调：当前可能停在任意步骤页，①/②/③的控件未必存在。
        # 推荐值一律先写缓存；只有控件在当前页时才顺带刷 UI。
        if getattr(self, "hw_label", None) is not None:
            _label_set(self.hw_label, hw["short"])
        self._hw_cuda13_ok = bool(hw.get("cuda13_ok"))
        self._hw_vram_mb = int(hw.get("vram") or 0)
        # ★ 2.0.2：显存到手后先别急着刷 —— 下面可能按硬件自动选后端/档位，
        #   那两个都会改变预算（变体）与权重（档位），收尾统一重建多路选项。
        if not self.cfg.get("variant_chosen"):
            vi = VARIANT_ORDER.index(hw["variant"]) \
                if hw.get("variant") in VARIANT_ORDER else 0
            v = VARIANT_ORDER[vi]
            self.sel["variant"] = v
            self.cfg["variant"] = v
            vcombo = getattr(self, "variant_combo", None)
            if vcombo is not None:
                _combo_set(vcombo,
                           [VARIANT_LABELS[x] for x in VARIANT_ORDER], vi)
                self._on_variant(save_chosen=False)
            else:
                save_config(self.cfg)
        if not self.cfg.get("tier_chosen"):
            idx = TIER_ORDER.index(hw["tier"]) if hw.get("tier") in TIER_ORDER else 0
            t = TIER_ORDER[idx]
            self.sel["tier"] = t
            self.cfg["tier"] = t
            tcombo = getattr(self, "tier_combo", None)
            if tcombo is not None:
                _combo_set(tcombo, [TIER_SHORT[x] for x in TIER_ORDER], idx)
                self._on_tier(save_chosen=False)
            else:
                save_config(self.cfg)
        if not silent:
            self.log("硬件：%s" % hw["short"], "ok")
        # ★ 2.0.2：预算与档位/后端都已定，统一重建多路选项（内含显存估算刷新）。
        self._refresh_parallel_ui()
        self.refresh_local_status()

    def ui_call(self, fn, *a, **k):
        """把 UI 操作送回宿主 UI 线程。

        2.x 的 `api.ui.*` 有线程闸门：**非 UI 线程调用返回 None 并记 Warn，
        不抛异常** —— 工作线程直接调控件只会静默失效。所以先判线程：
        - UI 线程：当场同步执行；
        - B249+：**跟随宿主**，回调直接交给原生 `ui.post`（宿主负责切线程 +
          异常隔离，每条回调是独立 Qt 事件、不会像"一次抽干几千条"那样卡住
          主线程，因此不再需要自建队列节流）；
        - 老宿主（B245 无 post）：回退自建队列 + `ui.timer` 泵（纯兼容层，
          B249+ 上完全不激活）。

        `post` 的 pybind 签名是 `post(fn, *args)`，只收位置参数，故把 (fn,a,k)
        闭包进无参 lambda 再投，避免关键字参数被 py::args 拒绝。
        """
        ui = getattr(self, "ui", None)
        if ui is None:
            # 无 UI 门面（裸对象/测试桩）：没有 post/队列可投，退化为当场执行
            try:
                fn(*a, **k)
            except Exception as e:
                self._log_ui_error(fn, e)
            return
        try:
            on_ui = bool(ui.on_ui_thread())
        except Exception:
            # 探测失败时默认"不在 UI 线程"：把回调送进 post/队列。
            # 反方向（默认在 UI 线程直接调用）在 B249+ 上会被宿主线程闸门
            # 静默吞掉——表现为"点了没反应"，比多一层投递难查得多。
            on_ui = False
        if on_ui:
            try:
                fn(*a, **k)
            except Exception as e:
                self._log_ui_error(fn, e)
            return
        post = getattr(ui, "post", None)
        if callable(post):
            try:
                post(lambda: fn(*a, **k))
                return
            except Exception:
                pass  # post 异常 → 退回兼容队列
        self._q.put((fn, a, k))

    def _log_ui_error(self, fn, exc):
        """UI 回调异常写详细日志：否则只表现为"点了没反应"。"""
        try:
            self.api.log("UI 回调异常: %s — %s: %s"
                         % (getattr(fn, "__name__", fn),
                            type(exc).__name__, exc))
        except Exception:
            pass

    def _pump(self):
        # 一次把队列抽干会让主线程长时间回不到事件循环 —— 几千条日志排队时
        # 窗口表现为"卡死"。这里限制单次预算（200 项 / 15 ms），剩下的下个
        # tick 继续，保证重绘与输入事件始终有机会执行。
        # 2.x：定时器由宿主持有并自动重复（`ui.timer`），这里不再自己重排。
        t0 = time.time()
        for _ in range(200):
            try:
                fn, a, k = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*a, **k)
            except Exception as e:
                self._log_ui_error(fn, e)
            if time.time() - t0 > 0.015:
                break

    def _reg(self, w):
        """占位：更新泵由宿主 QTimer 负责，这里只把控件原样返回
        （保留调用点形状，减少无意义的批量改动）。"""
        return w

    def log(self, msg, tag="info"):
        line = "%s  %s%s" % (time.strftime("%H:%M:%S"),
                             _TAG_PREFIX.get(tag, ""), msg)
        self.log_lines.append(line)
        del self.log_lines[:-400]
        # 2.x 的 `api.log` 只有一个参数、也没有颜色通道：用文字前缀表达等级，
        # 与界面日志行保持同一套标记。
        try:
            self.api.log(line)
        except Exception:
            pass
        # 一次只追加一行：`log_view_append` 不重写全文，不会 O(n²)、不破坏
        # 选中与滚动位置（整块重写在批量翻译时会拖慢主线程）。
        self.ui_call(self._append_log, line)

    def _append_log(self, line):
        view = getattr(self, "log_view", None)
        if view is None:
            return
        try:
            if self.ui.alive(view):
                self.ui.log_view_append(view, line)
        except Exception:
            pass

    def _flush_log(self):
        """兼容入口：把内存里的日志整块对齐到视图（仅在视图被重建后使用）。"""
        try:
            self.ui.log_view_clear(self.log_view)
            for line in list(self.log_lines):
                self.ui.log_view_append(self.log_view, line)
        except Exception:
            pass

    def dl_log(self, msg):
        self.log(msg)

    def _build(self):
        """单个宿主工具页，内部 6 个页签（①→⑤ + ⑥术语可选）。

        真机 BETA.246 的 `tabs_add_page` 直接塞控件会被拉到约 100px 高；
        每个页签内先放一个 `viewport`，步骤控件落在 viewport body 上，
        并用 `viewport_set_min_body_height(..., 80)` 压掉空白。
        进度条和日志入口放在 tabs 外面，切页时常驻底部。
        """
        ui = self.ui
        p = self.parent
        ui.label(p, "AI 离线翻译（本地推理，不联网翻译）· 按 ①→⑥ 操作")

        steps = (
            ("hw", "① 硬件"),
            ("runtime", "② 环境"),
            ("model", "③ 模型"),
            ("engine", "④ 引擎"),
            ("job", "⑤ 翻译"),
            ("glossary", "⑥ 术语（可选）"),
        )
        self.step_defs = steps
        pages = None
        try:
            tabs = ui.tabs(p)
            if tabs:
                pages = [ui.tabs_add_page(tabs, label)
                         for _key, label in steps]
                self.tabs = tabs
                if not pages or not all(pages):
                    pages = None
                else:
                    # 默认停在①（否则宿主会把最后添加的页签设为当前）
                    try:
                        ui.tabs_set_current(tabs, 0)
                    except Exception:
                        pass
        except Exception as exc:
            pages = None
            try:
                self.api.log("页签创建失败，回退单页：%s" % exc)
            except Exception:
                pass

        # ★ 控件直接 pack 到 page —— 与平台内置 fs_page 同构（它从不在页签里套
        #   viewport），BETA.249 上内容才能完整显示。
        #   Qt 的 QVBoxLayout 会把 page 剩余高度按 sizePolicy 均分给控件；像①页
        #   只有 3 个 Preferred 控件时，就被拉成"每行之间一大块空白"。解法：在
        #   每页最后放一个 Expanding 的弹性占位（QSplitter 格，fs_page 同款控件），
        #   由它吃掉剩余高度，把上面的控件顶回顶部按 sizeHint 紧凑排列。
        #   注：ui.split 是合法 2.x 门面（py_host 有 .def("split")），此处仅作
        #   弹性占位、不套滚动区；契约测试原把它与旧式 pack/row 并列禁用
        #   属误伤，已在 test_2x_contract.py 修正。
        if pages:
            for (_key, _label), page in zip(steps, pages):
                getattr(self, "_build_step_" + _key)(page)
                try:
                    spring = ui.split(page, False)
                    if spring:
                        ui.split_add_pane(spring, 0)
                except Exception:
                    pass  # 老宿主没有 split 门面时，仅少了紧凑效果，不影响功能
            self.api.log("AI 翻译界面已分 6 个页签（①→⑤ + ⑥术语可选）")
        else:
            for key, _label in steps:
                getattr(self, "_build_step_" + key)(p)

        # 底部固定区：进度条常驻；日志不占页面空间，点按钮弹窗查看。
        ui.label(p, "进度")
        self.progress = ui.progress(p)
        ui.set_progress(self.progress, 0)
        self.stage_label = ui.label(p, "阶段：待命")
        self.count_label = ui.label(p, "已翻译 0 / 0")
        self.btn_show_log = ui.button(p, "查看日志（弹窗）", self.on_show_log)
        self.log_view = None

        # 更新泵：B249+ 用宿主原生 ui.post（由 ui_call 按需投递 _pump），
        # 不再常驻 QTimer；老宿主无 post 时回退 QTimer 轮询。
        self._use_host_post = callable(getattr(ui, "post", None))
        if not self._use_host_post:
            try:
                ui.timer(p, 30, self._pump)
            except Exception:
                pass
        self._on_tier(save_chosen=False)
        if self.parser is None:
            self.log("XML 解析模块加载失败（assets/xray_pkg.zip 缺失？）", "err")

    def _section(self, p, text):
        """分节标题。2.x 的 label style 只是挂了个属性（没有对应 QSS 规则），
        所以只用纯文本，靠空行/顺序分隔。"""
        return self.ui.label(p, text)

    def _build_step_hw(self, p):
        ui = self.ui
        self._section(p, "① 硬件检测")
        self.btn_hw = ui.button(p, "检测硬件", self.on_hw_check)
        self.hw_label = ui.label(p, "尚未检测")

    def _build_step_runtime(self, p):
        ui = self.ui
        self._section(p, "② 运行环境（显卡驱动 / CPU）")
        ui.label(p, "后端")
        variant_vals = [VARIANT_LABELS[v] for v in VARIANT_ORDER]
        self.variant_combo = ui.combo(
            p, variant_vals, on_change=lambda v: self._on_variant(v))
        _combo_set(self.variant_combo, variant_vals,
                   VARIANT_ORDER.index(self.sel["variant"]))
        self.btn_probe_rt = ui.button(
            p, "测速选源", lambda: self.on_probe("rt"))
        self.btn_dl_rt = ui.button(
            p, "下载运行库", lambda: self.on_download("rt"))
        self.btn_dl_cancel = ui.button(
            p, "取消", self.on_cancel_download, enabled=False)

        ui.label(p, "下载源")
        self.source_combo = ui.combo(p, ["先测速，自动选最快"])
        self.env_label = ui.label(p, "")

    def _build_step_model(self, p):
        ui = self.ui
        self._section(p, "③ 离线模型（四选一）")
        ui.label(p, "模型档位")
        tier_vals = [TIER_SHORT[t] for t in TIER_ORDER]
        self.tier_combo = ui.combo(
            p, tier_vals, on_change=lambda v: self._on_tier(v))
        _combo_set(self.tier_combo, tier_vals,
                   TIER_ORDER.index(self.sel["tier"]))
        self.btn_probe_model = ui.button(
            p, "测速选源", lambda: self.on_probe("model"))
        self.btn_dl_model = ui.button(
            p, "下载模型", lambda: self.on_download("model"))

        ui.label(p, "自选上下文（仅「自选模型」档生效）")
        ctx_vals = [str(v) for v in CUSTOM_CTX_CHOICES]
        self.ctx_combo = ui.combo(
            p, ctx_vals, on_change=lambda v: self._on_custom_ctx(v))
        _combo_set(self.ctx_combo, ctx_vals, _ctx_index(self.sel["ctx"]))

        ui.label(p, "模型下载源")
        self.model_source_combo = ui.combo(p, ["先测速，自动选最快"])
        self.model_env_label = ui.label(p, "")

        # 模型文件：drop_box 是只读 QLineEdit ⇒ 与前一行的标签自动并成
        # 宿主的"字段行"，顺带成为拖放目标（把 .gguf 拖进来即可）。
        ui.label(p, "模型文件（可拖入 .gguf）")
        self.model_box = ui.drop_box(
            p, on_file=self._pick_model, placeholder="选择或拖入 .gguf")
        self.btn_choose_model = ui.button(
            p, "选择模型", self.on_choose_model)
        self.btn_choose_exe = ui.button(p, "自选 exe", self.on_choose_exe)

    def _build_step_engine(self, p):
        ui = self.ui
        self._section(p, "④ 启动引擎")
        ui.label(p, "多路翻译（文本表并发路数）")
        # ★ 2.0.2：路数选项按检测到的硬件动态生成（1..预算内最高档）；硬件/
        #   模型信息未就绪时先给固定档（1/2/4），硬件检测回调回来后重建。
        #   构建期不写配置：已存路数超出预算的收窄由 _apply_hw →
        #   _refresh_parallel_ui 点名日志后写回。
        choices = self._parallel_choices()
        if choices:
            labels = [lb for _, lb in choices]
            ns = [n for n, _ in choices]
        else:
            labels = [PARALLEL_LABELS[v] for v in PARALLEL_ORDER]
            ns = list(PARALLEL_ORDER)
        cur = self.current_parallel()
        idx = ns.index(cur) if cur in ns else 0
        self.parallel_combo = ui.combo(
            p, labels, on_change=lambda v: self._on_parallel(v))
        _combo_set(self.parallel_combo, labels, idx)
        self.vram_label = ui.label(p, "显存估算：等待选择模型…")

        self.btn_start = ui.button(p, "启动引擎", self.on_start)
        self.btn_stop = ui.button(p, "停止", self.on_stop, enabled=False)
        self.engine_status = ui.label(p, "引擎未启动")
        ui.label(p, "引擎就绪后，到「⑤ 翻译」页开始处理。")
        self.refresh_vram_estimate()

    def _build_step_job(self, p):
        ui = self.ui
        self._section(p, "⑤ 翻译（自动识别全部格式，输出保持原目录结构）")

        ui.label(p, "待翻译目录")
        self.in_box = ui.drop_box(
            p, on_file=self._pick_in,
            placeholder="待翻译目录（如 gamedata，可拖入）")
        self.btn_in = ui.button(p, "选择目录", self.on_choose_in)
        self.btn_scan = ui.button(p, "扫描选档", self.on_scan_dir)

        ui.label(p, "输出目录")
        self.out_box = ui.drop_box(
            p, on_file=self._pick_out,
            placeholder="默认 输入目录/translated_hy_mt2")
        self.btn_out = ui.button(p, "选择目录", self.on_choose_out)

        ui.label(p, "文件类型")
        mode_vals = [MODE_LABELS[m] for m in MODE_ORDER]
        self.mode_combo = ui.combo(p, mode_vals)
        _combo_set(self.mode_combo, mode_vals,
                   MODE_ORDER.index(self.sel["mode"]))

        ui.label(p, "源语言")
        self.lang_combo = ui.combo(p, list(_LANG_LABELS))
        _combo_set(self.lang_combo, list(_LANG_LABELS),
                   _lang_index(self.sel["lang"]))

        # 选项区（★ 2.0.23 精简）：只保留真正需要用户取舍的开关，每个后面
        # 挂一行简要说明。引用层/完整补丁/TM-lite/会话词表/高频专名已收编
        # 为内部固定开启，官方模板措辞是实验键不再展示（见 flags 初始化处）。
        self.force_chk = ui.checkbox(p, "强制术语", None)
        _chk_set(self.force_chk, self.flags["glossary_force"])
        ui.label(p, "　开：词库命中的词强制回填指定译名，译名不漂移；关：只作软提示，模型可能不遵守")
        self.protect_chk = ui.checkbox(p, "保护专名/歌名/型号", None)
        _chk_set(self.protect_chk, self.flags["protect_proper_v2"])
        ui.label(p, "　开：人名/歌名/型号整条跳过模型，只做词库子串替换；关：全部送模型，可能被改写")
        self.only_ref_chk = ui.checkbox(p, "只译被引用条目", None)
        _chk_set(self.only_ref_chk, self.flags["only_referenced"])
        ui.label(p, "　开：游戏没引用到的条目不送模型（省请求）；关：全量翻译，动态引用更保险")
        self.literal_chk = ui.checkbox(p, "翻译脚本/XML 字面文本", None)
        _chk_set(self.literal_chk, self.flags["translate_literals"])
        ui.label(p, "　开：脚本/UI 里写死的文本抽成补充词表一并翻译；关：这些文字保持原文")
        self.preview_chk = ui.checkbox(p, "先预览（不写盘）", None)
        _chk_set(self.preview_chk, self.flags["preview_reference"])
        ui.label(p, "　试运行：只统计可译/待译/保护条目，不写任何文件；正式翻译请保持关闭")

        self.btn_run = ui.button(p, "开始翻译", self.on_run)
        self.btn_run_cancel = ui.button(
            p, "取消", self.on_cancel_run, enabled=False)

    def _build_step_glossary(self, p):
        """⑥ 术语（可选）：附加提示词 + 强制术语，按需才切到这里。"""
        ui = self.ui
        self._section(p, "⑥ 术语（可选，不影响前五步）")
        ui.label(p, "附加提示词/约束（可选，追加到内置规则后）")
        self.btn_extra_demo = ui.button(p, "填入示例", self.on_extra_demo)
        self.extra_prompt = ui.text(p, height=2, wrap="word")
        _multiline_set(self.extra_prompt, self.cfg.get("prompt_extra", ""))

        ui.label(p, "强制术语 · 原文")
        self.gk_entry = ui.entry(p)
        ui.label(p, "强制术语 · 译文")
        self.gv_entry = ui.entry(p)
        self.btn_gloss_add = ui.button(p, "添加", self.on_gloss_add)
        self.btn_gloss_del = ui.button(p, "删除", self.on_gloss_del)
        self.gloss_count = ui.label(p, "共 0 条")
        self.btn_gloss_import = ui.button(p, "导入 JSON", self.on_gloss_import)
        self.btn_gloss_export = ui.button(p, "导出 JSON", self.on_gloss_export)
        self.btn_gloss_clear = ui.button(p, "清空", self.on_gloss_clear)

    def current_tier(self):
        """当前模型档位（读跨线程缓存 `self.sel`，不碰控件）。"""
        tier = self.sel.get("tier")
        return tier if tier in TIER_ORDER else "7b"

    def current_variant(self):
        """当前后端（读跨线程缓存 `self.sel`，不碰控件）。"""
        variant = self.sel.get("variant")
        return variant if variant in VARIANT_ORDER else "cpu"

    def current_parallel(self):
        """用户选定的路数（2.0.2 起是 1..PARALLEL_CAP 的任意整数）。"""
        try:
            p = int(self.sel.get("parallel", 1))
        except (TypeError, ValueError):
            return 1
        return p if 1 <= p <= PARALLEL_CAP else 1

    def _parallel_budget_mib(self):
        """多路预算（MiB）：GPU 后端按显存 ×0.9，CPU 后端按物理内存 ×0.7。

        检测不到（未检测/无独显/非 Windows）返回 0 —— 调用方回落固定档，
        绝不拿猜测值当预算。
        """
        if self.current_variant() == "cpu":
            ram = physical_ram_mb()
            return int(ram * PARALLEL_CPU_BUDGET) if ram > 0 else 0
        vram = int(getattr(self, "_hw_vram_mb", 0) or 0)
        return int(vram * PARALLEL_GPU_BUDGET) if vram > 0 else 0

    def _parallel_choices(self):
        """按当前模型/上下文/后端与检测到的硬件生成动态路数选项。

        返回 [(路数, 标签), ...]（1..max_n，预算内最高档标「推荐」）；
        信息不足（模型未就绪 / 预算未知 / GGUF 结构读不出）返回 None，
        调用方回落固定档 PARALLEL_ORDER。每档标签带上该路的显存/内存
        估算，与「显存估算」同一份口径（estimate_vram_mib）。
        """
        model = self.find_model()
        if not model:
            return None
        budget = self._parallel_budget_mib()
        if budget <= 0:
            return None
        ctx = self._route_ctx()
        variant = self.current_variant()
        key = (model, ctx, variant, budget)
        cache = getattr(self, "_par_choices_cache", None)
        if cache is not None and key in cache:
            return cache[key]
        est = estimate_vram_mib(
            model, ctx, 1, gpu_layers=0 if variant == "cpu" else 99)
        if not est.get("ok"):
            return None
        max_n = parallel_max_routes(budget, est["weights"], est["kv"],
                                    est["compute"])
        if variant == "cpu":
            note = " / 内存 %s" % _fmt_mem(budget // PARALLEL_CPU_BUDGET)
        else:
            note = " / 显卡 %s" % _fmt_mem(budget // PARALLEL_GPU_BUDGET)
        choices = []
        for n in range(1, max_n + 1):
            used = est["weights"] + est["kv"] * n + est["compute"]
            bits = []
            if n == 1:
                bits.append("最稳，默认")
            if n == max_n and max_n > 1:
                bits.append("推荐")
            bits.append("约 %s" % _fmt_mem(used))
            choices.append((n, "%d 路（%s%s）"
                            % (n, "，".join(bits), note if n == 1 else "")))
        if cache is None:
            cache = self._par_choices_cache = {}
        cache[key] = choices
        return choices

    def _parallel_max_n(self):
        """当前硬件预算允许的最高路数；信息不足时以 PARALLEL_CAP 为上限。"""
        choices = self._parallel_choices()
        return choices[-1][0] if choices else PARALLEL_CAP

    def _refresh_parallel_ui(self):
        """按当前硬件/模型重建「多路翻译」下拉（2.0.2 的所有刷新入口）。

        保持已选路数：仍在动态列表里就原样选中；超出预算（硬件收窄/换了
        更大的模型）则改选预算内最高档，并**点名写日志 + 写回设置** ——
        不静默丢弃用户的选择。
        """
        combo = getattr(self, "parallel_combo", None)
        choices = self._parallel_choices()
        if combo is not None:
            if choices:
                labels = [lb for _, lb in choices]
                ns = [n for n, _ in choices]
            else:
                labels = [PARALLEL_LABELS[v] for v in PARALLEL_ORDER]
                ns = list(PARALLEL_ORDER)
            cur = self.current_parallel()
            if cur in ns:
                idx = ns.index(cur)
            else:
                # 超预算：改选预算内最高档（动态表）或不超过它的最大固定档
                idx = len(ns) - 1 if choices else 0
                for i, n in enumerate(ns):
                    if n <= cur:
                        idx = i
                clamped = ns[idx]
                if clamped != cur and cur > 1:
                    self.log("多路 %d 超出当前硬件预算（模型/显存已变更），"
                             "改为 %d 路（已写回设置）" % (cur, clamped),
                             "warn")
                self.sel["parallel"] = clamped
                self.cfg["parallel"] = clamped
                save_config(self.cfg)
            _combo_set(combo, labels, idx)
        self.refresh_vram_estimate()

    def _on_parallel(self, value=None):
        if value:
            n = parallel_of_label(value)
            if n:
                self.sel["parallel"] = min(n, PARALLEL_CAP)
        self.cfg["parallel"] = self.current_parallel()
        save_config(self.cfg)
        self.refresh_vram_estimate()
        if self.current_parallel() > 1:
            self.log("多路翻译：%d 路并发；显存占用见「④ 引擎」页估算"
                     % self.current_parallel(), "dim")

    def _route_ctx(self):
        tier = self.current_tier()
        if tier == "custom":
            try:
                return max(1024, int(self.sel.get("ctx", 8192)))
            except (TypeError, ValueError):
                return 8192
        return int(TIER_PROFILES[tier]["ctx"])

    def refresh_vram_estimate(self):
        label = getattr(self, "vram_label", None)
        if label is None:
            return
        model = self.find_model()
        par = self.current_parallel()
        ctx = self._route_ctx()
        key = (model, ctx, par, self.current_variant())
        est = getattr(self, "_vram_cache", {}).get(key)
        if est is None:
            est = estimate_vram_mib(
                model, ctx, par,
                gpu_layers=99 if self.current_variant() != "cpu" else 0)
            if not hasattr(self, "_vram_cache"):
                self._vram_cache = {}
            self._vram_cache[key] = est
        if not est.get("ok") or not model:
            _label_set(label, "显存估算：未选择模型（下载后自动刷新）")
            return
        vram = int(getattr(self, "_hw_vram_mb", 0) or 0)
        suffix = (" / 显卡 %s" % _fmt_mem(vram)) if vram else ""
        warn = ""
        if vram and est["total"] > vram * 0.9:
            warn = "　⚠ 可能超出显存"
        _label_set(label, "显存估算：%s（权重 %s + KV %s × %d 路 + 缓冲 %d MB）"
                   "%s%s%s"
                   % (_fmt_mem(est["total"]), _fmt_mem(est["weights"]),
                      _fmt_mem(est["kv"] // par), par, est["compute"],
                      suffix, warn,
                      "　※ 按 GGUF 元数据估算" if est.get("approx") else ""))

    def _on_variant(self, value=None, save_chosen=True):
        """后端下拉变更（2.x 回调把新值作为第一个参数传进来）。"""
        if value:
            self.sel["variant"] = _variant_of_label(value)
        v = self.current_variant()
        if v == "cuda13" and not getattr(self, "_hw_cuda13_ok", False):
            self.log("当前驱动/显卡可能不支持 CUDA 13.4（需 580+ 驱动 / "
                     "Turing+）；仍可尝试，启动失败会自动回退 CUDA 12.4", "warn")
        self.cfg["variant"] = v
        if save_chosen:
            self.cfg["variant_chosen"] = True
        save_config(self.cfg)
        if getattr(self, "source_combo", None) is not None:
            _combo_set(self.source_combo, ["先测速，自动选最快"], 0)
            if getattr(self, "env_label", None) is not None:
                _label_set(self.env_label, "")
        self.refresh_local_status()
        if v in GPU_VARIANTS and not self.find_exe(v):
            self.log("%s 运行库未安装，点②「下载运行库」"
                     % VARIANT_LABELS[v].split("（")[0], "warn")
        # ★ 2.0.2：后端变化会改变预算来源（显存 ↔ 物理内存），重建多路选项。
        self._refresh_parallel_ui()

    def model_candidates(self, tier=None):
        tier = tier or self.current_tier()
        cands = []
        if tier == "custom":
            p = self.cfg.get("custom_model_path", "")
            return [p] if p else []
        cfg_path = self.cfg.get("model_paths", {}).get(tier)
        if cfg_path:
            cands.append(cfg_path)
        cands.append(os.path.join(self.cfg.get("model_dir", _MODELS_DIR),
                                  TIER_FILE[tier]))
        cands.append(os.path.join(_MODELS_DIR, TIER_FILE[tier]))
        cands.append(os.path.join(_SHIPPED_MODELS_DIR, TIER_FILE[tier]))
        return cands

    def find_model(self, tier=None):
        for c in self.model_candidates(tier):
            if c and os.path.isfile(c):
                return c
        return None

    def find_exe(self, variant=None):
        """严格按 variant 查找，**绝不跨后端回退**：
        否则选了 CUDA/Vulkan 却静默跑 CPU（GPU 不工作且难察觉）。"""
        variant = variant or self.current_variant()
        manual = self.cfg.get("exe_paths", {}).get(variant)
        if manual and os.path.isfile(manual):
            if self._exe_matches_variant(manual, variant):
                return manual
        for base in _bin_dirs():
            direct = os.path.join(base, variant, "llama-server.exe")
            if not os.path.isfile(direct):
                continue
            if variant in ("cuda", "cuda13") and not self._cudart_present(
                    os.path.dirname(direct)):
                continue
            return direct
        return None

    @staticmethod
    def _cudart_present(d):
        """CUDA 运行时是否**完整**安装。

        早期只要任意一个文件以 cudart/cublas/nvrtc 开头就算装好 ——
        半套解压也会被判为可用，随后 ggml-cuda 初始化失败、静默回退 CPU。
        现在要求 3 组标记**全部**命中；安装成功时会额外写一个 marker。
        """
        try:
            files = [f.lower() for f in os.listdir(d)]
        except OSError:
            return False
        has_core = any(f.startswith("cudart64") for f in files)
        if _CUDART_MARK in files:
            return has_core          # marker 在但核心 DLL 被删 → 仍需重装
        return all(any(f.startswith(m) for f in files) for m in _CUDART_MARKS)

    @staticmethod
    def _exe_matches_variant(exe_path, variant):
        """校验手动指定的 llama-server.exe 所在目录确实是该后端构建。"""
        d = os.path.dirname(exe_path)
        if os.path.basename(os.path.dirname(d)).lower() == variant:
            return True
        try:
            files = {f.lower() for f in os.listdir(d)}
        except OSError:
            return False
        if variant in ("cuda", "cuda13") and any(
                f.startswith("ggml-cuda") for f in files):
            return True
        if variant == "vulkan" and any("vulkan" in f for f in files):
            return True
        if variant == "cpu":
            has_gpu_backend = any(
                f.startswith("ggml-cuda") or "vulkan" in f
                for f in files)
            return not has_gpu_backend
        return False

    def refresh_local_status(self):
        model = self.find_model()
        exe = self.find_exe()

        def upd():
            if getattr(self, "model_box", None) is not None:
                _entry_set(self.model_box, model or "")
            eng = getattr(self, "engine_status", None)
            if eng is not None:
                if self.server and self.server.is_running():
                    _label_set(eng, "运行中（端口 %d）" % self.server.port)
                else:
                    bits = ["模型就绪" if model else "模型缺失",
                            "运行库就绪" if exe else "运行库缺失"]
                    _label_set(eng, "未启动（%s）" % " · ".join(bits))
            env_w = getattr(self, "env_label", None)
            if env_w is not None:
                env = ("运行库已装 · %s" % _DATA_ROOT) if exe \
                    else "运行库未安装"
                _label_set(env_w, env)
            # 控件读值只能在 UI 线程做：一并放进 upd，工作线程调用时也安全
            try:
                self._persist_cfg()
            except Exception:
                pass
        self.ui_call(upd)

    def _push_prompt_extra(self):
        """把「附加提示词/约束」推给引擎对象，并留痕一次。

        **只读 `_prompt_extra_text` 缓存，不碰控件**：本方法由 on_start/on_run
        的**工作线程**调用；tk.Text 在非主线程读取会抛
        `RuntimeError: main thread is not in main loop`（早期就是这样被
        吞成空串，功能实际从未生效）。缓存由 UI 线程的 _persist_cfg 更新。
        """
        if self.server is None:
            return
        txt = str(getattr(self, "_prompt_extra_text", "") or "").strip()
        self.server.prompt_extra = txt
        if txt:
            self.ui_call(
                self.log, "已启用附加提示词（%d 字符），作为「补充要求」"
                          "追加在内置规则后" % len(txt))

    def _persist_cfg(self):
        # 控件 → 缓存 → config.json。本方法只在 UI 线程执行（由 ui_call 保证）。
        # 分页会销毁非当前步骤的控件：缺失的控件不要覆盖已有缓存。
        if getattr(self, "tier_combo", None) is not None:
            self.sel["tier"] = _tier_of_label(_combo_get(self.tier_combo))
        if getattr(self, "variant_combo", None) is not None:
            self.sel["variant"] = _variant_of_label(
                _combo_get(self.variant_combo))
        self.cfg["tier"] = self.sel["tier"]
        self.cfg["variant"] = self.sel["variant"]
        par_text = _combo_get(getattr(self, "parallel_combo", None))
        # ★ 2.0.2：动态选项 —— 正则取"开头数字 路"，不再按固定表前缀匹配。
        n = parallel_of_label(par_text)
        if n:
            self.sel["parallel"] = min(n, PARALLEL_CAP)
        self.cfg["parallel"] = self.current_parallel()
        for key, attr in (("in_dir", "in_box"), ("out_dir", "out_box")):
            w = getattr(self, attr, None)
            if w is not None:
                self.cfg[key] = _entry_get(w)
        w = getattr(self, "lang_combo", None)
        if w is not None:
            lang = _combo_get(w)
            self.sel["lang"] = re.split(r"[\s（(]", lang, 1)[0] or "auto"
            self.cfg["src_lang"] = self.sel["lang"]
        w = getattr(self, "mode_combo", None)
        if w is not None:
            mode_text = _combo_get(w)
            for k in MODE_ORDER:
                if mode_text.startswith(k):
                    self.cfg["mode"] = k
                    break
            self.sel["mode"] = self.cfg["mode"]
            self._mode_plain = self.cfg["mode"]
        for key, attr in (
                ("glossary_force", "force_chk"),
                ("only_referenced", "only_ref_chk"),
                ("protect_proper_v2", "protect_chk"),
                ("preview_reference", "preview_chk"),
                ("translate_literals", "literal_chk")):
            w = getattr(self, attr, None)
            if w is not None:
                self.flags[key] = _chk_get(w)
        self.cfg.update(self.flags)
        try:
            self.cfg["prompt_extra"] = _multiline_get(
                self.extra_prompt).strip()
            # UI 线程读好 → 缓存给工作线程用（见 _push_prompt_extra）
            self._prompt_extra_text = self.cfg["prompt_extra"]
        except Exception:
            pass
        if not save_config(self.cfg) and not self._cfg_save_warned:
            self._cfg_save_warned = True
            self.log("配置保存失败（%s）：本次设置只在内存生效，重启后会丢失"
                     % (_LAST_CONFIG_ERROR or "未知原因"), "warn")
        elif _LAST_CONFIG_ERROR == "":
            self._cfg_save_warned = False

    def on_extra_demo(self):
        """填入示例模板：说明这个框是用来放哪类要求的（可整段删改）。"""
        _multiline_set(self.extra_prompt, EXTRA_PROMPT_DEMO)
        self.cfg["prompt_extra"] = EXTRA_PROMPT_DEMO
        self._prompt_extra_text = EXTRA_PROMPT_DEMO
        self.log("已填入示例模板（可整段删改）。注意此处是软引导："
                 "模型多数情况会遵守，但不保证 100%；"
                 "必须强制统一的译名请用「强制术语」放进术语库。", "warn")

    def refresh_gloss_view(self):
        n = len(self.gloss_user)

        def upd():
            if getattr(self, "gloss_count", None) is not None:
                _label_set(self.gloss_count, "共 %d 条" % n)
        self.ui_call(upd)

    def on_gloss_add(self):
        k = _entry_get(self.gk_entry).strip()
        v = _entry_get(self.gv_entry).strip()
        if not k or not v:
            self.log("请填写原文与强制译文两项", "warn")
            return
        self.gloss_user[k] = v
        self._gloss_set[k] = v
        self._gloss_deleted.discard(k)
        try:
            save_user_glossary(self._gloss_set, self._gloss_deleted,
                               self._gloss_clear)
        except Exception as e:
            self.log("术语库保存失败：%s" % e, "err")
            return
        _entry_set(self.gk_entry, "")
        _entry_set(self.gv_entry, "")
        self.refresh_gloss_view()
        self.log("已保存术语：%s => %s（共 %d 条）"
                 % (k, v, len(self.gloss_user)), "ok")

    def on_gloss_del(self):
        k = _entry_get(self.gk_entry).strip()
        if not k:
            self.log("在左侧输入框填写要删除的原文", "warn")
            return
        if k in self.gloss_user:
            del self.gloss_user[k]
            if k in self._gloss_set:
                del self._gloss_set[k]          # 删的是自己加的
            else:
                self._gloss_deleted.add(k)      # 删的是内置词条 → 记墓碑
            try:
                save_user_glossary(self._gloss_set, self._gloss_deleted,
                                   self._gloss_clear)
                self.log("已删除术语：%s" % k)
            except Exception as e:
                self.log("术语库保存失败：%s" % e, "err")
        else:
            self.log("术语库中没有该原文：%s" % k, "warn")
        _entry_set(self.gk_entry, "")
        _entry_set(self.gv_entry, "")
        self.refresh_gloss_view()

    def on_gloss_clear(self):
        if not self.gloss_user:
            return
        ok = self.ui.ask_yes_no(
            "清空术语库",
            "确定清空内置术语库的全部 %d 条记录吗？" % len(self.gloss_user))
        if not ok:
            return
        self.gloss_user = {}
        self._gloss_set = {}
        self._gloss_deleted = set()
        self._gloss_clear = True               # 连内置词条一起隐藏
        try:
            save_user_glossary(self._gloss_set, self._gloss_deleted,
                               self._gloss_clear)
        except Exception as e:
            self.log("术语库保存失败：%s" % e, "err")
            return
        self.refresh_gloss_view()
        self.log("术语库已清空（内置词条被用户层隐藏；重新导入即可恢复）")

    def on_gloss_import(self):
        try:
            f = self.ui.ask_open_file(
                "导入术语表 JSON（合并，同名词条以导入文件为准）",
                filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        except Exception:
            f = None
        if not f:
            return
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            self.log("术语表读取失败：%s" % e, "err")
            return
        if not isinstance(data, dict):
            self.log("术语表格式应为 {\"原文\": \"译文\"} 的 JSON 对象", "err")
            return
        incoming = flatten_glossary(data)
        if not incoming:
            self.log("术语表为空或格式无法识别（支持扁平表与 {\"分类\": {...}} 分类表）",
                     "err")
            return
        before = len(self.gloss_user)
        self.gloss_user.update(incoming)
        self._gloss_set.update(incoming)
        # 导入的条目优先生效：把它们从"墓碑"里移除
        self._gloss_deleted.difference_update(incoming)
        try:
            save_user_glossary(self._gloss_set, self._gloss_deleted,
                               self._gloss_clear)
        except Exception as e:
            self.log("术语库保存失败：%s" % e, "err")
            return
        self.refresh_gloss_view()
        self.log("已从 %s 合并 %d 条（术语库 %d → %d 条）"
                 % (os.path.basename(f), len(incoming), before,
                    len(self.gloss_user)), "ok")

    def on_gloss_export(self):
        try:
            f = self.ui.ask_save_file(
                "导出术语表 JSON",
                filetypes=[("JSON", "*.json")],
                initialfile="glossary_export.json")
        except Exception:
            f = None
        if not f:
            return
        if not f.lower().endswith(".json"):
            f += ".json"
        try:
            tmp = f + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.gloss_user, fh, ensure_ascii=False,
                          indent=1, sort_keys=True)
            os.replace(tmp, f)
        except Exception as e:
            self.log("导出失败：%s" % e, "err")
            return
        self.log("已导出 %d 条术语：%s" % (len(self.gloss_user), f), "ok")

    def _pick_model(self, path):
        if path.lower().endswith(".gguf"):
            tier = self.current_tier()
            if tier == "custom":
                self.cfg["custom_model_path"] = path
            else:
                self.cfg.setdefault("model_paths", {})[tier] = path
            save_config(self.cfg)
            self.refresh_local_status()
            # ★ 2.0.2：模型文件到手（权重已知），立即按预算重建多路选项。
            self._refresh_parallel_ui()

    def _pick_in(self, path):
        if os.path.isdir(path):
            self.cfg["in_dir"] = path
            self._persist_cfg()
            self._default_out(path)
            self._auto_scan_in_dir(path)

    def _auto_scan_in_dir(self, path):
        """自动识别这个 MOD 的待翻译文件总数（后台扫描，不卡界面）。"""
        self.scan_info = None
        self.scan_dir = None
        self._set_stage("阶段：正在识别待翻译文件…")
        mode = self.cfg.get("mode") or "auto"

        def work():
            try:
                info = analyze_input_dir(
                    path, mode,
                    log=lambda m: self.ui_call(self.log, m),
                    cancel=self.tr_cancel)
            except Exception as e:
                self.ui_call(self.log, "待翻译文件识别失败：%s" % e, "warn")
                return
            self.scan_info, self.scan_dir = info, path
            n = int(info.get("files_with_text") or 0)
            self.ui_call(self.log,
                         "待翻译文件：%d 个（自动识别整个 MOD 中含可译文本的文件）"
                         % n)
            self.ui_call(self._set_count, 0, n, "个文件")
            self.ui_call(self._set_stage,
                         "阶段：待命（已识别待翻译文件 %d 个）" % n)
        threading.Thread(target=work, daemon=True).start()

    def _default_out(self, in_dir):
        """输出目录默认 输入目录/translated_hy_mt2（用户可改）。"""
        cur = _entry_get(self.out_box) if getattr(self, "out_box", None) else ""
        if not cur:
            d = os.path.join(in_dir, "translated_hy_mt2")
            _entry_set(self.out_box, d)
            self.cfg["out_dir"] = d
            save_config(self.cfg)

    def _pick_out(self, path):
        if os.path.isdir(path):
            self.cfg["out_dir"] = path
            self._persist_cfg()

    def _ask_dir(self, title):
        """宿主自带目录对话框（系统原生样式）。"""
        try:
            d = self.ui.ask_directory(title)
        except Exception as e:
            self.log("目录选择框不可用（%s），请把文件夹直接拖入输入框" % e,
                     "warn")
            return None
        return d or None

    def on_choose_model(self):
        try:
            f = self.ui.ask_open_file(
                "选择 GGUF 模型",
                filetypes=[("GGUF 模型", "*.gguf"), ("所有文件", "*.*")])
        except Exception:
            f = None
        if f:
            self._pick_model(f)

    def on_choose_exe(self):
        try:
            f = self.ui.ask_open_file(
                "选择 llama-server.exe",
                filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        except Exception:
            f = None
        if f and os.path.basename(f).lower() == "llama-server.exe":
            self.cfg.setdefault("exe_paths", {})[self.current_variant()] = f
            save_config(self.cfg)
            self.refresh_local_status()

    def on_choose_in(self):
        d = self._ask_dir("选择待翻译目录")
        if d:
            _entry_set(self.in_box, d)
            self._pick_in(d)

    def on_choose_out(self):
        d = self._ask_dir("选择译文输出目录")
        if d:
            _entry_set(self.out_box, d)
            self._pick_out(d)

    def _on_custom_ctx(self, value=None):
        text = str(value if value else self.sel.get("ctx", "8192"))
        self.sel["ctx"] = text
        try:
            v = int(text)
        except Exception:
            return
        if v in CUSTOM_CTX_CHOICES:
            self.cfg["custom_ctx"] = v
            save_config(self.cfg)
            if self.current_tier() == "custom":
                self.refresh_local_status()
                # ★ 2.0.2：自选上下文改变每路 KV（预算切分的边际），重建多路选项。
                self._refresh_parallel_ui()

    def _on_tier(self, value=None, save_chosen=True):
        """切档：更新状态；引擎运行中切档提示需重启。"""
        if value:
            self.sel["tier"] = _tier_of_label(value)
        tier = self.current_tier()
        self.cfg["tier"] = tier
        if save_chosen:
            self.cfg["tier_chosen"] = True
        save_config(self.cfg)
        if getattr(self, "ctx_combo", None) is not None:
            try:
                self.ui.set_enabled(self.ctx_combo, tier == "custom")
            except Exception:
                pass
        if getattr(self, "model_box", None) is not None:
            self.refresh_local_status()
        if getattr(self, "model_source_combo", None) is not None:
            _combo_set(self.model_source_combo,
                       ["先测速，自动选最快"], 0)
            if getattr(self, "model_env_label", None) is not None:
                _label_set(self.model_env_label, "")
        if self.server and self.server.is_running():
            self.log("已切换模型档，运行中的引擎仍是旧模型，重启引擎生效",
                     "warn")
        # ★ 2.0.2：档位变化改变模型权重（预算里的固定成本），重建多路选项。
        self._refresh_parallel_ui()

    def _probe_task_key(self, kind):
        if kind == "rt":
            return "bin_" + self.current_variant()
        tier = self.current_tier()
        return TIER_PROFILES[tier]["task"] or \
            TIER_PROFILES["mt2"]["task"]

    def _sources_for(self, kind):
        key = self._probe_task_key(kind)
        return DOWNLOAD_TASKS[key], key

    def _source_combo(self, kind):
        return self.source_combo if kind == "rt" else self.model_source_combo

    def _populate_sources(self, kind, results):
        """把测速结果填入对应区的“源”下拉框：可达在前（按速度），
        不可达在后供人工尝试。"""
        combo = self._source_combo(kind)
        labels = []
        for r in results:
            if r.ok:
                labels.append("%s · %.2f MB/s" % (r.name, r.speed))
            else:
                labels.append("%s · 不可达" % r.name)
        if not labels:
            labels = ["测速失败，请重试"]
        _combo_set(combo, labels, 0)

    def on_probe(self, kind):
        """测速：kind='rt' 当前后端运行库；kind='model' 当前模型档。
        未测速过时，下载会自动先测速再拉取。"""
        if kind == "model" and self.current_tier() == "custom":
            self.log("自选档使用本机 .gguf，无需测速下载", "warn")
            return
        task, key = self._sources_for(kind)
        btn = self.btn_probe_rt if kind == "rt" else self.btn_probe_model
        self.ui.set_enabled(btn, False)
        self.log("测速：%s" % task["label"])

        def work():
            try:
                results = run_probe(
                    task["sources"],
                    log=lambda m, tag="info": self.ui_call(self.log, m, tag))
                self.probe_cache[key] = results
                env = detect_environment(results)

                def done():
                    self._populate_sources(kind, results)
                    label = self.model_env_label if kind == "model" \
                        else self.env_label
                    _label_set(label, env)
                    self.log(env)
                    self.ui.set_enabled(btn, True)
                self.ui_call(done)
            except Exception as e:
                self.ui_call(self.log, "测速失败：%s" % e, "err")
                self.ui_call(self.ui.set_enabled, btn, True)
        threading.Thread(target=work, daemon=True).start()

    def _resolve_download_sources(self, kind, key, task, chosen_label=""):
        """按用户在“源”下拉框的选择决定下载顺序。
        返回 (ordered[(name,url)], auto_probed, chosen_result|None)。
        选中源排第一，其余可达源按测速排序依次备援，失败自动切换。

        chosen_label 由 **UI 线程**读好再传进来：本方法跑在下载工作线程，
        直接去读界面控件（_combo_get）属于跨线程访问 GUI，会出现偶发异常或
        读到半更新状态（文件自己的 _plan_engine 就立了"控件值必须由 UI 线程
        预先传入"的规矩，这里是唯一漏网点）。
        """
        results = self.probe_cache.get(key)
        auto = False
        if results is None:
            results = run_probe(
                task["sources"],
                log=lambda m, tag="info": self.ui_call(self.log, m, tag))
            self.probe_cache[key] = results
            auto = True
            self.ui_call(lambda: self._populate_sources(kind, results))
        ok = sorted((r for r in results if r.ok),
                    key=lambda r: r.speed, reverse=True)
        chosen = None
        if chosen_label and " · " in chosen_label:
            sel_name = chosen_label.split(" · ", 1)[0]
            chosen = next((r for r in results if r.name == sel_name), None)
        ordered = []
        if chosen is not None:
            ordered.append((chosen.name, chosen.url))
        for r in ok:
            if chosen is None or r.name != chosen.name:
                ordered.append((r.name, r.url))
        seen = {n for n, _ in ordered}
        for n, u in task["sources"]:
            if n not in seen:
                ordered.append((n, u))
        return ordered, auto, chosen

    def on_download(self, kind):
        if self.dl_cancel is not None:
            self.log("已有下载任务在进行", "warn")
            return
        task, key = self._sources_for(kind)
        if kind == "model" and self.current_tier() == "custom":
            self.log("自选档请点「选择模型」指定本机 .gguf，无需下载", "warn")
            return
        self.dl_cancel = threading.Event()
        chosen_label = _combo_get(self._source_combo(kind))

        def progress(done, total, speed, eta, name):
            self.ui_call(self._set_stage, "阶段：下载中 · %s" % name)
            self.ui_call(self._set_dl_count, done, total, name)
            if total:
                pct = int(100.0 * done / total)
                if pct != getattr(self, "_last_pct", -1):
                    self._last_pct = pct
                    self.ui_call(self._set_progress, pct)
            if total and done - getattr(self, "_last_ui_byte", -1) > 8*1024*1024:
                self._last_ui_byte = done
                self.ui_call(self.log,
                             "  %.0f%%（%s/%s）%.1f MB/s 剩余 %s"
                             % (100.0*done/total, _fmt_size(done),
                                _fmt_size(total), speed, _fmt_eta(eta)))

        def work():
            try:
                ordered, auto, chosen = self._resolve_download_sources(
                    kind, key, task, chosen_label)
                if auto:
                    self.ui_call(self.log, "自动测速完成，可在“源”下拉框改选")
                if chosen is not None and not chosen.ok:
                    self.ui_call(self.log,
                                 "你选择的 %s 测速不可达，将先尝试它、"
                                 "失败后自动切换其他源" % chosen.name, "warn")
                if task["kind"] == "model":
                    model_dir = self.cfg.get("model_dir") or _MODELS_DIR
                    os.makedirs(model_dir, exist_ok=True)
                    target = os.path.join(model_dir, task["filename"])
                else:
                    target = os.path.join(
                        _DOWNLOAD_DIR, task["variant"] + "_runtime.zip")
                if os.path.isfile(target) and task["kind"] == "model":
                    self.ui_call(self.log, "模型已存在：%s" % target, "warn")
                    return
                if task["kind"] == "zip":
                    self.ui_call(self.log,
                                 "提示：运行库将从 GitHub 及其公共镜像下载，"
                                 "下载后会按官方 SHA-256 校验；原始包保留在 %s"
                                 % _DOWNLOAD_DIR, "warn")
                self.ui_call(self.log, "下载：%s（首选 %s，共 %d 个源备援）"
                             % (task["label"], ordered[0][0], len(ordered) - 1))
                # 校验放在 download_one 内部逐个源进行：某个镜像给了坏文件时
                # 删掉它并自动换下一个源（早期只是删文件让用户手动重试）。
                digest = download_one(
                    ordered, target,
                    log=lambda m, tag="info": self.ui_call(self.log, m, tag),
                    progress=progress,
                    cancel_event=self.dl_cancel,
                    verify=lambda p: verify_artifact(
                        p, task,
                        log=lambda m, tag="info": self.ui_call(self.log, m, tag),
                        known_hashes=self.cfg.get("artifact_hashes")),
                    total_hint=int(task.get("size_bytes") or 0) or
                    (int(float(task.get("size_hint") or 0) * (1 << 30))
                     or None))
                self.cfg.setdefault("artifact_hashes", {})[
                    os.path.basename(target)] = digest
                save_config(self.cfg)
                if task["kind"] == "zip":
                    dest = os.path.join(_BIN_DIR, task["variant"])
                    exe = extract_zip(
                        target, dest, "llama-server.exe",
                        log=lambda m, tag="info": self.ui_call(self.log, m, tag))
                    try:
                        os.remove(target)
                    except OSError:
                        pass
                    extra = task.get("extra_runtime")
                    if extra:
                        self._install_extra_runtime(extra, dest)
                    self.ui_call(self.log, "运行库就绪：%s" % exe, "ok")
                else:
                    tier = TIER_BY_TASK[key]
                    self.cfg.setdefault("model_paths", {})[tier] = target
                    save_config(self.cfg)
                    self.ui_call(self.log, "模型下载完成：%s" % target, "ok")
            except DownloadCancelled:
                self.ui_call(self.log, "已取消，断点已保留")
            except InvalidArtifact as e:
                self.ui_call(self.log,
                             "下载产物校验未通过（已依次尝试所有源）：%s" % e,
                             "err")
            except Exception as e:
                self.ui_call(self.log, "下载失败：%s" % e, "err")
            finally:
                self.ui_call(self._after_download)
        threading.Thread(target=work, daemon=True).start()
        self._last_pct = -1
        self.ui.set_enabled(self.btn_dl_rt, False)
        self.ui.set_enabled(self.btn_dl_model, False)
        self.ui.set_enabled(self.btn_dl_cancel, True)

    def _set_progress(self, pct):
        """把百分比写进进度条（只收整数；宿主负责钳到 0–100）。"""
        try:
            self.ui.set_progress(self.progress, int(pct))
        except Exception:
            pass

    def _set_stage(self, text):
        """阶段提示：下载 / 翻译共用同一条进度条，靠这行说明当前在做什么。"""
        try:
            _label_set(self.stage_label, text)
        except Exception:
            pass

    def _set_count(self, done, total, unit="", extra=""):
        """计数提示：按「实际要处理的文件数」显示 已完成/总数，比纯百分比直观。"""
        try:
            if total:
                _label_set(self.count_label,
                           "已翻译 %d / %d %s（%.0f%%）%s"
                           % (int(done), int(total), unit,
                              100.0 * min(done, total) / total, extra))
            else:
                _label_set(self.count_label, "已翻译 0 / 0 %s%s" % (unit, extra))
        except Exception:
            pass

    def _set_dl_count(self, done, total, name=""):
        """下载阶段复用同一行计数：显示已下载/总大小。"""
        try:
            if total:
                _label_set(self.count_label,
                           "已下载 %s / %s（%.0f%%）· %s"
                           % (_fmt_size(done), _fmt_size(total),
                              100.0 * done / total, name))
            else:
                _label_set(self.count_label, "已下载 %s · %s"
                           % (_fmt_size(done), name))
        except Exception:
            pass

    def _after_download(self):
        self.dl_cancel = None
        self._last_ui_byte = -1
        self._last_pct = -1
        self._set_progress(0)
        self._set_count(0, 0)
        self._set_stage("阶段：待命（下载已完成）")
        self.ui.set_enabled(self.btn_dl_rt, True)
        self.ui.set_enabled(self.btn_dl_model, True)
        self.ui.set_enabled(self.btn_dl_cancel, False)
        self.refresh_local_status()

    def _install_extra_runtime(self, extra, dest):
        """CUDA 后端第二步：下载并解压 cudart 运行时；完整安装则跳过。"""
        if self._cudart_present(dest):
            self.ui_call(self.dl_log,
                         "%s 已存在，跳过" % extra["label"])
            return
        self.ui_call(self.dl_log,
                     "继续下载%s（CUDA 必需，否则会回退 CPU）…"
                     % extra["label"])
        sources = _gh_sources(extra["url"])
        rt_zip = os.path.join(_DOWNLOAD_DIR, "cudart_runtime.zip")
        results = run_probe(sources)
        ok = [(r.name, r.url) for r in results if r.ok]
        if not ok:
            raise IOError("无法下载 %s：所有源不可达" % extra["label"])

        def rt_progress(done, total, speed, eta, name):
            if done % (20 * 1024 * 1024) >= 1048576:
                return
            self.ui_call(self.dl_log, "  cudart：%.0f%% %.1f MB/s"
                         % (100.0 * done / total if total else 0, speed))

        rt_task = {"kind": "zip", "name_prefix": "cudart64",
                   "size_hint": 0.37, "sha256": extra.get("sha256", "")}
        digest = download_one(
            ok, rt_zip,
            log=lambda m: self.ui_call(self.dl_log, m),
            progress=rt_progress,
            cancel_event=self.dl_cancel,
            verify=lambda p: verify_artifact(
                p, rt_task,
                log=lambda m, tag="info": self.ui_call(self.log, m, tag),
                known_hashes=self.cfg.get("artifact_hashes")),
            total_hint=int(float(rt_task.get("size_hint") or 0) * (1 << 30))
            or None)
        self.cfg.setdefault("artifact_hashes", {})["cudart_runtime.zip"] = digest
        save_config(self.cfg)
        _extract_zip_into(rt_zip, dest)
        try:
            with open(os.path.join(dest, _CUDART_MARK), "w",
                      encoding="utf-8") as f:
                json.dump({"sha256": digest, "installed": time.strftime(
                    "%Y-%m-%d %H:%M:%S")}, f)
        except OSError:
            pass
        try:
            os.remove(rt_zip)
        except OSError:
            pass
        self.ui_call(self.dl_log, "%s 安装完成" % extra["label"])

    def on_cancel_download(self):
        if self.dl_cancel:
            self.dl_cancel.set()
            self.dl_log("正在取消…")

    def _selected_mode(self):
        for k in ("text", "cfgxml", "ltx", "scriptE", "scriptL"):
            if _combo_get(self.mode_combo).startswith(k):
                return k
        return "text"

    def on_scan_dir(self):
        if self.scan_cancel is not None:
            self.scan_cancel.set()
            self.log("正在取消勘察…", "warn")
            return
        d = _entry_get(self.in_box)
        if not d or not os.path.isdir(d):
            self.log("请先选择有效的待翻译目录", "warn")
            return
        if os.path.dirname(os.path.abspath(d)) == os.path.abspath(d):
            try:
                ok = self.ui.ask_yes_no(
                    "目录看起来是磁盘根目录",
                    "「%s」是磁盘根目录，勘察会遍历整盘、耗时可能很长。\n"
                    "通常应选游戏或模组的 gamedata / configs 目录。\n"
                    "仍要继续吗？" % d)
            except Exception:
                ok = False
            if not ok:
                self.log("已取消勘察（建议改选 gamedata 或 configs 目录）")
                return
        mode = self._selected_mode()
        self._mode_plain = mode
        self.scan_cancel = threading.Event()
        self._set_btn_scan_text("取消勘察")
        self.log("正在勘察目录（文件/最长文本/上下文需求）…再点一次可取消")

        def work():
            try:
                info = analyze_input_dir(
                    d, mode,
                    log=lambda m: self.ui_call(self.log, m),
                    cancel=self.scan_cancel)
                if not info.get("cancelled"):
                    self.scan_info = info
                    self.scan_dir = d
                self.ui_call(self._report_scan, info, mode)
            except Exception as e:
                self.ui_call(self.log, "勘察失败：%s" % e, "err")
            finally:
                self.scan_cancel = None
                self.ui_call(self._set_btn_scan_text, "扫描选档")
        threading.Thread(target=work, daemon=True).start()

    def _set_btn_scan_text(self, text):
        """改「扫描选档 / 取消勘察」按钮文字（2.x set_text 支持 QPushButton）。"""
        _label_set(self.btn_scan, text)

    def _report_scan(self, info, mode):
        if info.get("cancelled"):
            self.log("已取消勘察（统计不完整，档位保持不变）", "warn")
            return
        rec_tier = info["recommend"]
        if info.get("truncated"):
            self.log("勘察达到文件数上限提前结束，结果只覆盖已读部分，"
                     "仅供参考", "warn")
        ready = [t for t in TIER_ORDER if self.find_model(t)]
        if rec_tier not in ready:
            pref = [t for t in (rec_tier, "mt2", "7b", "custom")
                    if t in ready]
            if pref:
                self.log("推荐的 %s 档模型尚未下载，已改选本机就绪的 %s 档"
                         "（下载对应模型后可手动切回）"
                         % (TIER_PROFILES[rec_tier]["combo"][:2],
                            TIER_PROFILES[pref[0]]["combo"][:2]), "warn")
                rec_tier = pref[0]
        rec = TIER_PROFILES[rec_tier]
        self.log("勘察结果：文件 %d 个 / %.1f MB；最大单文件 %.0f KB；"
                 "可翻译文本约 %d 条，最长 %d 字符（%s）"
                 % (info["files"], info["bytes"] / 1048576.0,
                    info["max_file"][0] / 1024.0, info["n_strings"],
                    info["max_str"],
                    os.path.basename(info["max_str_file"]) or "-"))
        self.log("待翻译文件（自动识别整个 MOD 中含可译文本的文件）：%d 个"
                 % int(info.get("files_with_text") or 0))
        for t in BUILTIN_TIERS:
            n = info["overlong"][t]
            if n:
                self.log("  %s 档将对 %d 条超长文本自动分句翻译"
                         % (TIER_PROFILES[t]["combo"][:2], n))
        self.log("推荐档位：%s" % rec["combo"])
        idx = TIER_ORDER.index(rec_tier)
        _combo_set(self.tier_combo, TIER_LABELS, idx)
        self.sel["tier"] = TIER_ORDER[idx]
        self._on_tier(save_chosen=False)
        ctx = decide_ctx(rec_tier, info,
                         cap=(int(self.cfg.get("custom_ctx", 8192))
                              if rec_tier == "custom" else None))
        self.log("该目录在推荐档的自动上下文：%d token（引擎启动时生效）" % ctx)

    def _plan_engine(self, allow_scan=False, tier=None, variant=None,
                     in_dir=None, interactive=False):
        """返回 (model, exe, tier, ctx)；缺组件时返回 None。
        所有控件值必须由调用方在 UI 线程预先传入，本函数其余部分
        只用 os/已缓存数据，可安全在工作线程运行。
        interactive=True（UI 线程调用）才弹缺组件对话框；工作线程只返回 None。
        allow_scan=False（UI 线程）只复用已勘察结果，不做大目录扫描。"""
        tier = tier or self.current_tier()
        variant = variant or self.current_variant()
        if in_dir is None:
            # 控件值由调用方在 UI 线程预先灌进 cfg（_persist_cfg）；
            # 本函数可能在 worker 里跑，绝不碰控件。
            in_dir = self.cfg.get("in_dir", "")
        model = self.find_model(tier)
        exe = self.find_exe(variant)
        if not model or not exe:
            if interactive:
                missing = "模型文件" if not model else "llama-server 运行库"
                self.ui.ask_yes_no(
                    "缺少组件",
                    "%s未就绪。请在下方「下载管理」中下载（可先测速选源）。" % missing)
            return None
        info = None
        if in_dir and os.path.isdir(in_dir):
            if self.scan_dir == in_dir and self.scan_info is not None:
                info = self.scan_info
            elif allow_scan:
                info = analyze_input_dir(
                    in_dir, self._mode_plain,
                    log=lambda m: self.ui_call(self.log, m),
                    cancel=self.tr_cancel)
                self.scan_info, self.scan_dir = info, in_dir
        cap = (int(self.cfg.get("custom_ctx", 8192))
               if tier == "custom" else None)
        ctx = decide_ctx(tier, info, cap=cap)
        return model, exe, tier, ctx

    def on_start(self):
        self._persist_cfg()
        plan = self._plan_engine(allow_scan=False, interactive=True)
        if not plan:
            return
        model, exe, tier, ctx = plan
        variant = self.current_variant()
        # ★ 2.0.2 启动前安全钳制（兜底二道闸）：正常情况下下拉已经只显示
        #   预算内的档位（_refresh_parallel_ui 收窄 + 写回）；这里防的是
        #   配置在别处被改、或检测回调尚未完成就点启动的窗口期。
        par = self.current_parallel()
        max_n = self._parallel_max_n()
        if par > max_n:
            self.log("多路 %d 超出当前硬件预算，按预算内最高档 %d 路启动"
                     "（已写回设置）" % (par, max_n), "warn")
            par = max_n
            self.sel["parallel"] = par
            self.cfg["parallel"] = par
            save_config(self.cfg)
        if self._starting:
            self.log("正在启动引擎，请稍候", "warn")
            return
        need_stop = False
        if self.server and self.server.is_running():
            sig = (self.server.model, self.server.variant,
                   self.server.ctx_size, getattr(self.server, "parallel", 1))
            if sig == (model, variant, ctx, par):
                self.log("引擎已在运行（相同模型/上下文/%d 路）" % par)
                return
            need_stop = True
        self._starting = True
        self.ui.set_enabled(self.btn_start, False)
        self.log("正在加载 %s，上下文 %d token × %d 路，首次加载较慢，请稍候…"
                 % (TIER_PROFILES[tier]["combo"][:2], ctx, par))

        def work():
            try:
                if need_stop:
                    # 停旧引擎会 taskkill + wait（最长约 15 秒）——必须在工作线程，
                    # 否则切模型时整个界面冻结（早期就是在界面回调里同步停的）。
                    self.ui_call(self.log, "模型或上下文已变更，先停止旧引擎…")
                    old = self.server
                    if old is not None:
                        old.stop()
                    self.server = None
                self._spawn_server(exe, model, variant, ctx, tier, par)
                self.ui_call(self._engine_ui, True)
            except Exception as e:
                self.ui_call(self.log, "引擎启动失败：%s" % e, "err")
                self.ui_call(self._engine_ui, False)
            finally:
                self._starting = False
                self.ui_call(self._engine_ui,
                             bool(self.server and self.server.is_running()))
        threading.Thread(target=work, daemon=True).start()

    def _spawn_server(self, exe, model, variant, ctx, tier, parallel=1):
        """创建并启动引擎；CUDA 13.4 起不来或没上卡时自动回退 CUDA 12.4。

        13.4 的可用性只有"驱动 ≥ 580 / 计算能力 ≥ 7.5"这个门槛判据，
        实际能否初始化还受 OEM 驱动、Optimus、cudart DLL 等影响，所以这里
        用真实启动 + gpu_verified 兜底，失败就退回兼容面更广的 12.4。
        """
        def build(v, e):
            srv = LlamaServer(
                e, model, v,
                log=lambda m, tag="info": self.ui_call(self.log, m, tag),
                ctx_size=ctx, tier=tier, parallel=parallel)
            # ★ 2.0.3 官方措辞 A/B 开关跟随每次引擎启动（含回退路径）。
            srv.official_prompts = bool(
                (getattr(self, "flags", {}) or {}).get(
                    "official_prompts", False))
            return srv

        def fallback(reason):
            self.ui_call(self.log, "CUDA 13.4 %s，尝试回退 CUDA 12.4…"
                         % reason, "warn")
            try:
                srv.stop()
            except Exception:
                pass
            exe12 = self.find_exe("cuda")
            if not exe12:
                self.ui_call(self.log,
                             "未安装 CUDA 12.4 运行库，无法回退；"
                             "请下载运行库或改用 Vulkan/CPU", "err")
                return None
            srv12 = build("cuda", exe12)
            self.server = srv12
            self._push_prompt_extra()
            srv12.start()
            self.ui_call(self.log, "已回退到 CUDA 12.4", "ok")
            return srv12

        srv = build(variant, exe)
        self.server = srv
        self._push_prompt_extra()
        try:
            srv.start()
        except Exception:
            if variant != "cuda13":
                raise
            fb = fallback("启动失败")
            if fb is not None:
                return fb
            raise
        if variant == "cuda13" and not getattr(srv, "gpu_verified", False):
            fb = fallback("未能在 GPU 上初始化")
            if fb is not None:
                return fb
        return srv

    def _engine_ui(self, on):
        on = bool(on)
        self.ui.set_enabled(self.btn_start, not on)
        self.ui.set_enabled(self.btn_stop, on)
        self.refresh_local_status()

    def on_stop(self):
        if self.server:
            def work():
                self.server.stop()
                self.ui_call(self._engine_ui, False)
            threading.Thread(target=work, daemon=True).start()

    def on_run(self):
        self._persist_cfg()
        if self.tr_cancel is not None:
            self.log("已有翻译任务在进行", "warn")
            return
        if self._starting:
            self.log("引擎正在启动，请稍候再开始翻译", "warn")
            return
        in_dir = self.cfg.get("in_dir", "")
        if not in_dir or not os.path.isdir(in_dir):
            self.log("请先选择有效的待翻译目录", "warn")
            return
        if self.parser is None:
            self.log("XML 解析模块未就绪", "err")
            return
        out_dir = self.cfg.get("out_dir", "") or \
            os.path.join(in_dir, "translated_hy_mt2")
        _entry_set(self.out_box, out_dir)
        self.cfg["out_dir"] = out_dir
        save_config(self.cfg)
        glossary = dict(self.gloss_user)
        # 选项已由 _persist_cfg 从控件读进 self.flags（UI 线程），这里只读缓存
        force_glossary = self.flags["glossary_force"]
        # 只给模型/强制路径提供专名类硬术语；对话/百科类普通词不注入。
        hard_glossary = load_hard_glossary_keys() | set(self._gloss_set)
        hard_glossary = hard_glossary or None
        ref_mode = self.flags["reference_layer"]
        copy_ref = self.flags["copy_reference"]
        only_ref = self.flags["only_referenced"]
        protect_proper = self.flags["protect_proper_v2"]
        preview_ref = self.flags["preview_reference"]
        translate_literals = self.flags["translate_literals"]
        session_glossary = self.flags["session_glossary"]
        tm_lite = self.flags["tm_lite"]

        self.tr_cancel = threading.Event()
        self._last_pct = -1
        self.ui.set_enabled(self.btn_run, False)
        self.ui.set_enabled(self.btn_run_cancel, True)
        self._set_progress(0)
        self._set_count(0, 0)
        self._set_stage("阶段：翻译中（按文本表推进）")

        mode = self.cfg["mode"]
        src_lang = self.cfg["src_lang"]
        if ref_mode and mode not in ("auto", "text"):
            self.log("引用层模式只在「自动」或「文本XML」类型下生效；"
                     "当前类型按原流程执行", "warn")
        cap_tier = self.current_tier()
        cap_variant = self.current_variant()

        def work():
            try:
                plan = self._plan_engine(
                    allow_scan=True, tier=cap_tier,
                    variant=cap_variant, in_dir=in_dir)
                if not plan:
                    if not self.find_model(cap_tier):
                        prof = TIER_PROFILES[cap_tier]
                        want = self.cfg.get("model_paths", {}).get(cap_tier)
                        if not want:
                            fname = TIER_FILE.get(cap_tier, "*.gguf")
                            want = os.path.join(
                                self.cfg.get("model_dir", _MODELS_DIR), fname)
                        self.ui_call(self.log,
                            "「%s」档模型未就绪：%s。请下载该档模型或点「选择已有模型」"
                            "指定文件；不要直接用其他档位的引擎代替。"
                            % (prof["combo"][:2], want), "err")
                    if not self.find_exe(cap_variant):
                        self.ui_call(self.log,
                            "%s运行库未就绪，请在「下载管理」安装"
                            % VARIANT_LABELS.get(cap_variant, cap_variant),
                            "err")
                    self.ui_call(self._run_done)
                    return
                model, exe, tier, ctx = plan
                running = bool(self.server and self.server.is_running())
                reusable = running and self.server.model == model and (
                    self.server.variant == cap_variant
                    or (cap_variant == "cuda13"
                        and self.server.variant == "cuda"))
                if running and not reusable:
                    self.ui_call(self.log,
                        "正在切换到 %s 档（停止旧引擎并以新模型重启）…"
                        % TIER_PROFILES[tier]["combo"][:2], "warn")
                    self.server.stop()
                    self.server = None
                if not reusable:
                    self._spawn_server(exe, model, cap_variant, ctx, tier)
                    if getattr(self.server, "health_warn", ""):
                        self.ui_call(self.log,
                                     "注意：" + self.server.health_warn, "warn")
                    self.ui_call(self._engine_ui, True)
                self._push_prompt_extra()
                pending_files = 0
                if self.scan_info and self.scan_dir == in_dir:
                    info = self.scan_info
                    pending_files = int(info.get("files_with_text")
                                        or info.get("files") or 0)
                    self.ui_call(self.log,
                                 "上下文 %d token × %d 路；最长文本 %d 字符，超长自动分句"
                                 % (self.server.ctx_size,
                                    getattr(self.server, "parallel", 1),
                                    info["max_str"]))
                    if pending_files:
                        self.ui_call(self.log,
                                     "待翻译文件：%d 个（已自动识别整个 MOD 的"
                                     "可译文本文件）" % pending_files)
                        self.ui_call(self._set_count, 0, pending_files, "个文件")

                job = TranslationJob(
                    self.parser, self.server, in_dir, mode, src_lang, "chs",
                    glossary=glossary,
                    force_glossary=force_glossary,
                    output_anyway=False,
                    ctx_size=self.server.ctx_size,
                    out_dir=out_dir,
                    log=lambda m, tag="info": self.ui_call(self.log, m, tag),
                    # 2.x 没有 log_detail：明细只落盘（translate_run.log）。
                    # 条目级进度同样只落盘：实测 6122 个文件时会达到 470 行/秒，
                    # 全推给界面日志会压死主线程（日志风暴是卡死主因）。
                    detail=lambda m, tag="info": _append_run_log(m),
                    progress=self._job_progress,
                    cancel_event=self.tr_cancel,
                    reference_mode=ref_mode,
                    copy_reference=copy_ref,
                    only_referenced=only_ref,
                    protect_proper=protect_proper,
                    preview_reference=preview_ref,
                    hard_glossary_keys=hard_glossary,
                    translate_literals=translate_literals,
                    auto_session_terms=session_glossary,
                    tm_lite=tm_lite,
                    freq_names=bool(self.flags.get("freq_names", True)),
                    parallel=getattr(self.server, "parallel", 1),
                    pending_files=pending_files)
                job.run()
            except Exception as e:
                self.ui_call(self.log, "翻译失败：%s" % e, "err")
            finally:
                self.ui_call(self._run_done)
        threading.Thread(target=work, daemon=True).start()

    def _job_progress(self, d):
        """条目级进度：明细落盘，进度条按文件数推进（UI 投递、按百分点节流）。"""
        total = int(d.get("files", 0) or 0)
        idx = int(d.get("file_idx", 0) or 0)
        # 计数行用「自动识别的整个 MOD 待翻译文件总数」做分母；
        # 进度条仍按本轮处理的文件数推进（原有百分比语义不变）。
        pending = int(d.get("pending") or 0) or total
        _append_run_log("  进度：[%d/%d] %s（%d/%d 条，累计 %d 段）"
                        % (idx, total, d.get("file", ""),
                           d.get("ids", 0), d.get("total_ids", 0),
                           d.get("reqs", 0)))
        if total > 0:
            pct = int(100.0 * min(idx, total) / total)
            if pct != getattr(self, "_last_pct", -1):
                # 进度条本身语义不变（百分比，按已处理文件数推进）；下面两行
                # 文字给出「实际要翻译的文件总数 / 已翻译数量」的直观计数。
                self._last_pct = pct
                self.ui_call(self._set_stage,
                             "阶段：翻译中 %d/%d · %s"
                             % (min(idx, total), total, d.get("file", "")))
                extra = ""
                if d.get("ids"):
                    extra = "，条目 %d 条" % int(d.get("ids") or 0)
                done_count = d.get("tabs")
                if done_count is None:
                    done_count = min(idx, total)
                self.ui_call(self._set_count, int(done_count), pending,
                             d.get("unit", ""), extra)
                self.ui_call(self._set_progress, pct)

    def _run_done(self):
        self.tr_cancel = None
        self._last_pct = -1
        self._set_progress(0)
        self._set_stage("阶段：完成")
        self.ui.set_enabled(self.btn_run, True)
        self.ui.set_enabled(self.btn_run_cancel, False)

    def on_cancel_run(self):
        if self.tr_cancel:
            self.tr_cancel.set()
            self.log("正在取消（当前段落翻译完后停止）…", "warn")

    def on_show_log(self):
        """底部「查看日志」：弹独立对话框，不占用主页面高度。"""
        lines = list(self.log_lines)[-300:]
        text = "\n".join(lines) if lines else "(暂无日志；详细运行记录见 logs\\translate_run.log)"
        try:
            self.ui.ask_yes_no("AI 翻译日志（最近 300 行）", text)
        except Exception as exc:
            try:
                self.api.log("日志弹窗失败：%s: %s" % (type(exc).__name__, exc))
            except Exception:
                pass



def register(api):
    """2.x 入口：注入 `api.ui` 后注册一个工具页，页内按步骤切换显示。

    `name` 是插件内唯一 id（ASCII），显示名由 `title` 给；`subtitle` 出现在
    页面标题下方。2.x 的页面外壳（header/card/滚动区）由宿主提供，
    这里只负责往 `parent` 里按顺序放控件。
    """
    global _UI, _HOST, _API
    _UI = api.ui
    # ★ B249+ 宿主服务门面（host.http_get/http_post）：小文本请求跟随宿主，
    #   不再自建 urllib/WinHTTP；老宿主无此门面时 _HOST=None 走兼容路径。
    #   B250+ 增加 host.http_download：中小产物（运行库 zip 等）跟随宿主。
    _HOST = getattr(api, "host", None)
    _API = api
    # ★ B249+ 宿主服务门面（host.http_get/http_post）：小文本请求跟随宿主，
    #   不再自建 urllib/WinHTTP；老宿主无此门面时 _HOST=None 走兼容路径。
    _HOST = getattr(api, "host", None)
    # ★ 真机 BETA.246 实证：宿主 `wrap_python_builder()` 调的是 `kept()`（零参），
    #   `parent` 由 `UiPanelScope(parent)` 隐式设为“当前面板根”；Python builder
    #   多收一个位置参数会直接 TypeError ⇒ 页面变占位页（日志：builder 抛异常）。
    #   控件照官方插件惯例传 `parent=None`，宿主 `resolve_parent()` 会回落到
    #   当前面板根（QTimer 也据此绑定到页面，随页销毁）。
    ok = api.register_tool(
        "ai_translate", lambda: AiTranslateTab(api, None),
        order=6, subtitle="Hy-MT2 本地离线翻译", title="AI 翻译")
    api.log("AI 翻译插件 %s 已注册（宿主 UI 门面%s）"
            % (PLUGIN_INFO.get("version", "?"),
               "" if ok else "，注册被宿主拒绝"))
    return ok
