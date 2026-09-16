#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""screenpeer.py — 局域网截图互传（Windows / macOS）

两台电脑各运行一份，互相填对方 IP，就能互发整屏截图：

    python screenpeer.py                    # 交互式：提示输入对方 IP（回车=上次记录）
    python screenpeer.py 192.168.1.23       # 启动即指定对方 IP
    python screenpeer.py 192.168.1.23 --port 5077
    python screenpeer.py --save-dir ~/pics  # 自定义接收保存目录（两端通用）

操作（脚本窗口可最小化，热键全局生效）：
    Ctrl + `        全局热键：任意程序前台时按下，截取本机整屏发给对方
                    （按键在系统层被吃掉，前台程序/浏览器收不到 Ctrl+`）
    回车            等价于热键（在脚本自己的控制台里按）
    ip <新IP>       运行中更换对方
    q / Ctrl+C      退出

依赖：
    pip install mss                        # 截屏，两端都要装
    pip install pyobjc-framework-Quartz    # 仅 macOS：事件拦截（装过 pynput 就已带）
    pip install pynput                     # 仅其它平台的降级监听才需要

权限（macOS 首次需要，授给运行脚本的终端，如 iTerm）：
    系统设置 → 隐私与安全性 → 屏幕录制（截屏用）+ 辅助功能（热键拦截用）
    改完重启终端再运行；Windows 无需额外权限。

行为说明：
    * 双方端口必须一致（默认 5077）；本机同时监听该端口，收到的截图默认保存到
      项目内的 runtime/shots/（可用 -d 改），文件名为纯时间戳（无任何标识性命名）。
    * 截屏静默：无快门声、无闪光、无水印、不落临时文件；热键先拦截再动作
      （Windows WH_KEYBOARD_LL / macOS Quartz Event Tap 命中即消费事件，
      不向前台窗口派发，浏览器既收不到按键也感知不到截屏动作）。
    * 明文 TCP，建议仅在可信内网、且只在你拥有或获授权的机器之间使用。

调试：
    --file PATH          不截屏，直接发送本地 PNG 文件
    --save-dir PATH      自定义接收保存目录（支持 ~ 和相对路径）
    环境变量 SCREENPEER_SAVE_DIR 也可覆盖保存目录（优先级低于 --save-dir）
"""

import argparse
import json
import logging
import os
import socket
import struct
import sys
import threading
import time

try:
    import mss
    import mss.tools
except ImportError as exc:
    sys.stderr.write("缺少依赖: %s\n请先安装:  pip install mss\n" % exc)
    sys.exit(1)

# pynput 只在 Windows/macOS 之外的平台做降级监听；两个主流平台都走系统级
# 可拦截 Hook，不需要 pynput（缺了也不影响）。
try:
    from pynput import keyboard as pynput_keyboard
except ImportError:
    pynput_keyboard = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402  （同目录模块，须先补 sys.path）
import logsetup  # noqa: E402

#: 文件日志 logger，名字须与 logsetup.setup("screenpeer") 一致。
#: 这里记的是「热键有没有命中 / 图有没有落盘」——用户按了 Ctrl+` 却没出图时，
#: 唯一的排查依据就是这两行。
log = logging.getLogger("screenpeer")

DEFAULT_PORT = 5077
MAGIC = b"DSNAP01\x00"
PNG_SIG = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 64 * 1024 * 1024
FIRE_COOLDOWN = 2.0          # 两次发送最小间隔（防按键自动重复连发）

_peer_file = paths.runtime_path("peer.json")
_last_fire = [0.0]
_save_dir_override = None        # --save-dir 指定后，优先级最高
_save_lock = threading.Lock()    # 并发保存保护


# ----------------------------- 截屏 -----------------------------

def capture_screen():
    """mss 抓取整屏 → PNG 字节（纯内存，无声音/闪光/水印/临时文件）"""
    mss_impl = getattr(mss, "MSS", mss.mss)
    with mss_impl() as sct:
        shot = sct.grab(sct.monitors[0])        # monitors[0] = 所有显示器并集
        return mss.tools.to_png(shot.rgb, shot.size)


# ----------------------------- 传输协议 -----------------------------
# 包格式: MAGIC(8) + meta长度(2) + meta(JSON) + 数据长度(4) + PNG数据

def build_packet(png):
    meta = {"v": 1, "host": socket.gethostname(),
            "os": sys.platform, "ts": round(time.time(), 3)}
    mb = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return MAGIC + struct.pack("!H", len(mb)) + mb + struct.pack("!I", len(png)) + png


def recv_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接中途断开")
        buf += chunk
    return bytes(buf)


def save_dir():
    """落盘目录优先级：--save-dir > SCREENPEER_SAVE_DIR > runtime/shots。

    默认值由「桌面」改成了项目内的 runtime/shots —— 截图本来就属于运行产物，
    不该散落到桌面。走 start_capture.py 启动时会显式传 --save-dir，不受影响。
    """
    if _save_dir_override:
        return _save_dir_override
    d = os.environ.get("SCREENPEER_SAVE_DIR")
    if d:
        return d
    return paths.runtime_path("shots")


def save_png(data, meta):
    d = save_dir()
    os.makedirs(d, exist_ok=True)
    ts = meta.get("ts")
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = time.time()
    with _save_lock:                             # 多线程同时收图时避免抢同名文件
        base = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
        ms = int(round(ts % 1 * 1000)) % 1000
        path = os.path.join(d, "%s_%03d.png" % (base, ms))
        i = 1
        while os.path.exists(path):
            path = os.path.join(d, "%s_%03d_%d.png" % (base, ms, i))
            i += 1
        tmp = "%s.%d.part" % (path, threading.get_ident())   # 临时名唯一，防互踩
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)                    # 原子写入，避免半截文件
    log.info("已落盘 %s (%d KB)", os.path.basename(path), len(data) // 1024)
    return path


def handle_conn(conn, addr):
    try:
        try:
            magic = recv_exact(conn, 8)
        except (ConnectionError, OSError):
            return                                # 空连接/端口探测：静默忽略，不刷错误
        if magic != MAGIC:
            print("\n[收到] %s: 协议头不符，已丢弃" % addr[0])
            return
        (mlen,) = struct.unpack("!H", recv_exact(conn, 2))
        meta = json.loads(recv_exact(conn, mlen).decode("utf-8")) if mlen else {}
        (dlen,) = struct.unpack("!I", recv_exact(conn, 4))
        if dlen <= 0 or dlen > MAX_BYTES:
            print("\n[收到] %s: 数据大小异常(%d)" % (addr[0], dlen))
            return
        data = recv_exact(conn, dlen)
        if not data.startswith(PNG_SIG):
            print("\n[收到] %s: 非 PNG 内容，已丢弃" % addr[0])
            return
        path = save_png(data, meta)
        print("\n[收到] %s  %d KB  ->  %s" % (addr[0], dlen // 1024, path))
    except Exception as exc:
        print("\n[收到] %s: 传输异常 %r" % (addr[0], exc))
    finally:
        try:
            conn.close()
        except OSError:
            pass


def server_loop(port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", port))
    except OSError as exc:
        print("[监听] 端口 %d 绑定失败: %s" % (port, exc))
        return
    srv.listen(8)
    while True:
        try:
            conn, addr = srv.accept()
        except OSError as exc:
            if srv.fileno() < 0:                 # 监听套接字已关闭 → 正常退出
                return
            # Windows 上客户端在握手期中断（端口探测、安全软件扫描等）会抛
            # ECONNABORTED，不能因此让接收线程退出、导致再也收不到图
            print("[监听] accept 瞬时异常，已忽略继续: %r" % exc)
            time.sleep(0.2)
            continue
        threading.Thread(target=handle_conn, args=(conn, addr), daemon=True).start()


def send_to(peer, port, data):
    with socket.create_connection((peer, port), timeout=8) as s:
        s.settimeout(30)      # 大图在慢 Wi-Fi 下给传输更多时间（连接阶段仍 8 秒）
        s.sendall(build_packet(data))


# ----------------------------- 全局热键 Ctrl+` -----------------------------

# 思路：先消费按键，再执行截图。命中 Ctrl+` 时在系统层把事件吃掉，前台程序
# （浏览器、终端）根本收不到它，然后才 fire() → 截屏 → 发送。
#   Windows : WH_KEYBOARD_LL 低级键盘钩子，回调 return 1     = 拦截
#   macOS   : CGEventTap（可拦截模式），回调 return None     = 拦截
#  其它系统: pynput 只能监听、拦不住（降级，按键仍会漏给前台）

# ` 键标识按平台区分：0x32 在 Windows 上是数字键 2、0x29 在 macOS 上是分号键，
# 混用会误吞 Ctrl+2 / Ctrl+;。
#   macOS  : 0x32 = kVK_ANSI_Grave
#   Windows: 0xC0 = VK_OEM_3（主键盘 `）；0x29 = 该键的物理扫描码（非 US 布局兜底）
_GRAVE_VK_MAC = 0x32
_GRAVE_VKS_OTHER = (0xC0, 0x29)


def is_grave_key(key, plat=None):
    """pynput 降级监听用：判断按键是否为 ` 键（plat 可注入，便于跨平台测试）"""
    plat = sys.platform if plat is None else plat
    vks = (_GRAVE_VK_MAC,) if plat == "darwin" else _GRAVE_VKS_OTHER
    vk = getattr(key, "vk", None)
    if isinstance(vk, int) and (vk & 0xFF) in vks:
        return True
    # 兜底：macOS 部分版本把 ` 键上报为 backspace；无 vk 时回退到字符，
    # 按住 Ctrl 时 pynput 在 Windows 上不给 char，故 char 只能作兜底
    if pynput_keyboard is not None and key is pynput_keyboard.Key.backspace:
        return True
    return getattr(key, "char", None) in ("`", "~")


def _safe_trigger(on_trigger):
    """在系统事件回调里跑外层逻辑：异常不能冒泡回系统回调，否则钩子会被拆掉"""
    try:
        on_trigger()
    except Exception as exc:
        print("[热键] 触发处理失败: %r" % exc)
        log.error("热键触发处理失败", exc_info=True)


def start_windows_keyboard_hook(on_trigger):
    """Win32 低级键盘钩子（WH_KEYBOARD_LL）：

    命中 Ctrl+` 时 return 1，事件被消费、不再传给前台窗口；其余按键一律
    CallNextHookEx 放行。返回 True 表示钩子已装好。
    """
    import ctypes
    from ctypes import wintypes

    WH_KEYBOARD_LL = 13
    HC_ACTION = 0
    WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
    WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
    VK_CONTROL = 0x11
    VK_GRAVE = 0xC0
    SCAN_GRAVE = 0x29
    LRESULT = ctypes.c_ssize_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD),
                    ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p)]

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        print("[热键] Win32 API 加载失败: %s" % exc)
        return False

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         ctypes.c_void_p, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    # armed：一次按住只发一张（挡掉自动重复）；swallow_up：已吃掉的 keyDown
    # 对应的 keyUp 也要一起吃掉，避免前台收到半截按键事件。
    state = {"ctrl": False, "armed": True, "swallow_up": False}

    def low_level_proc(n_code, w_param, l_param):
        if n_code != HC_ACTION:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        msg = int(w_param)
        # GetAsyncKeyState 兜底：钩子是中途装上的话，state 可能漏掉 Ctrl 按下
        ctrl_down = state["ctrl"] or bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
        if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            if info.vkCode == VK_CONTROL:
                state["ctrl"] = True
            elif info.vkCode == VK_GRAVE or info.scanCode == SCAN_GRAVE:
                if ctrl_down:
                    state["swallow_up"] = True
                    if state["armed"]:
                        state["armed"] = False
                        _safe_trigger(on_trigger)
                    return 1                     # ← 关键：吃掉 keyDown，浏览器收不到
        elif msg in (WM_KEYUP, WM_SYSKEYUP):
            if info.vkCode == VK_CONTROL:
                state["ctrl"] = False
                state["armed"] = True
            elif info.vkCode == VK_GRAVE or info.scanCode == SCAN_GRAVE:
                if state["swallow_up"]:
                    state["swallow_up"] = False
                    state["armed"] = True
                    return 1                     # keyDown 已吃，keyUp 一并吃掉
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    # 回调必须先包成 HOOKPROC 实例才能传给 SetWindowsHookExW：argtypes 里声明的是
    # 函数指针类型，直接传普通函数会被 ctypes 拒绝（ArgumentError: argument 2:
    # expected WinFunctionType instance instead of function）。
    # 且这里必须持有强引用：hook_proc 若被 GC 回收，系统回调会跳进已释放的跳板，
    # 直接搞崩进程（ctypes 回调的经典坑）。放在闭包里，活到钩子卸载为止。
    hook_proc = HOOKPROC(low_level_proc)

    ready = threading.Event()
    result = {"ok": False, "err": ""}

    def run():
        # 低级钩子必须在装它的线程里跑消息循环，所以整套都放进这个线程
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_proc,
                                        kernel32.GetModuleHandleW(None), 0)
        if not hook:
            result["err"] = "SetWindowsHookExW 失败: %s" % \
                ctypes.WinError(ctypes.get_last_error())
            ready.set()
            return
        result["ok"] = True
        ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnhookWindowsHookEx(hook)

    threading.Thread(target=run, daemon=True, name="screenpeer-hotkey").start()
    ready.wait(3.0)                      # 等钩子装好再返回，失败好走降级分支
    if not result["ok"]:
        print("[热键] %s" % (result["err"] or "键盘钩子安装超时"))
    return result["ok"]


def start_macos_keyboard_hook(on_trigger):
    """Quartz Event Tap：命中 Ctrl+` 时 return None，事件被消费、不再向前台派发。

    返回 True 表示 Event Tap 已建好。要求本进程有“辅助功能”权限，否则
    CGEventTapCreate 返回 NULL（没有权限就只能监听、拦不住按键）。
    """
    try:
        import Quartz
    except ImportError as exc:
        print("[热键] 缺少 Quartz（pyobjc）: %s" % exc)
        print("       安装:  pip install pyobjc-framework-Quartz")
        return False

    state = {"armed": True, "grave_down": False}
    holder = {}
    mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))

    def callback(proxy, etype, event, refcon):
        try:
            if etype in (Quartz.kCGEventTapDisabledByTimeout,
                         Quartz.kCGEventTapDisabledByUserInput):
                tap = holder.get("tap")
                if tap is not None:
                    Quartz.CGEventTapEnable(tap, True)    # 被系统临时关掉后自动救回
                return event
            ctrl_down = bool(Quartz.CGEventGetFlags(event)
                             & Quartz.kCGEventFlagMaskControl)
            if etype == Quartz.kCGEventFlagsChanged:
                if not ctrl_down and not state["grave_down"]:
                    state["armed"] = True                 # Ctrl 松手也复位，防卡死
                return event
            if etype not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                return event
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            if keycode != _GRAVE_VK_MAC:
                return event
            if etype == Quartz.kCGEventKeyDown:
                if not ctrl_down:
                    return event                          # 单独按 ` 照常输入
                state["grave_down"] = True
                if state["armed"]:
                    state["armed"] = False
                    _safe_trigger(on_trigger)
                return None                               # ← 关键：吃掉 keyDown
            if state["grave_down"]:
                state["grave_down"] = False
                state["armed"] = True
                return None                               # keyDown 已吃，keyUp 一并吃掉
            return event
        except Exception as exc:                          # 回调里绝不能抛异常
            print("[热键] 事件回调异常: %r" % exc)
            return event

    ready = threading.Event()
    result = {"ok": False, "err": ""}

    def run():
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,       # 必须是 Default；ListenOnly 拦不住
            mask, callback, None)
        if tap is None:
            result["err"] = ("事件拦截被系统拒绝（CGEventTapCreate 返回 NULL）："
                             "本进程没有“辅助功能”权限")
            ready.set()
            return
        holder["tap"] = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source,
                                  Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        result["ok"] = True
        ready.set()
        Quartz.CFRunLoopRun()

    threading.Thread(target=run, daemon=True, name="screenpeer-hotkey").start()
    if not ready.wait(3.0):
        print("[热键] 事件拦截启动超时")
        return False
    if not result["ok"]:
        print("[热键] %s" % result["err"])
        print("       处理: 系统设置 → 隐私与安全性 → 辅助功能 → 勾选运行本脚本的"
              "终端（如 iTerm/终端），然后重启终端再运行本脚本")
        return False
    return True


def _start_pynput_listener(on_trigger):
    """其它平台的降级方案：只能监听、拦不住按键。Ctrl+` 按下发一张，松开复位。"""
    if pynput_keyboard is None:
        print("[热键] 缺少依赖 pynput:  pip install pynput")
        return False
    keyboard = pynput_keyboard
    st = {"ctrl": False, "armed": True, "last_down": 0.0}
    lock = threading.Lock()

    def is_ctrl(key):
        return key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)

    def on_press(key):
        with lock:
            if is_ctrl(key):
                st["ctrl"] = True
            elif is_grave_key(key) and st["ctrl"]:
                now = time.time()
                # 自动重复的 keydown 间隔约 30ms；间隔 >0.5s 说明是新的一次按下。
                # 即使 ` 的松开事件丢失（armed 卡在 False），下次真实按下也能恢复，
                # 而按住不放不会被当成新按下而重复发送。
                fresh = (now - st["last_down"]) > 0.5
                st["last_down"] = now
                if st["armed"] or fresh:
                    st["armed"] = False
                    _safe_trigger(on_trigger)

    def on_release(key):
        with lock:
            if is_ctrl(key):
                st["ctrl"] = False
                st["armed"] = True               # 兜底复位
            elif is_grave_key(key):
                st["armed"] = True

    try:
        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()              # pynput 自带守护线程，非阻塞
    except Exception as exc:
        print("[热键] pynput 监听启动失败: %r" % exc)
        return False
    return True


def start_hotkey(on_trigger):
    """装好全局热键层。

    返回 True  = 命中时按键已在系统层被消费，前台程序收不到 Ctrl+`
    返回 False = 拦不住（降级为只监听）或热键启动失败
    """
    if sys.platform.startswith("win"):
        if start_windows_keyboard_hook(on_trigger):
            return True
        print("[热键] Windows 键盘钩子不可用，降级为只监听：按键会传给前台程序")
    elif sys.platform == "darwin":
        if start_macos_keyboard_hook(on_trigger):
            return True
        print("[热键] macOS 事件拦截不可用，降级为只监听：按键会传给前台程序")
    if not _start_pynput_listener(on_trigger):
        print("[热键] 热键不可用：请在控制台按回车发送")
    return False


def _send_work(state, source):
    try:
        data = capture_screen()
    except Exception as exc:
        print("\n[截屏] 失败: %s（%s）" % (exc, source))
        log.error("截屏失败（%s）", source, exc_info=True)
        return
    try:
        send_to(state["peer"], state["send_port"], data)
        print("\n[发送] %d KB -> %s:%d OK（%s）"
              % (len(data) // 1024, state["peer"], state["send_port"], source))
        log.info("已发送 %d KB -> %s:%d", len(data) // 1024, state["peer"], state["send_port"])
    except Exception as exc:
        print("\n[发送] 失败(%s:%d): %r（%s）"
              % (state["peer"], state["send_port"], exc, source))
        log.error("发送失败 -> %s:%d（%s）", state["peer"], state["send_port"], source, exc_info=True)


# ----------------------------- 对端记录 -----------------------------

def load_last_peer():
    try:
        with open(_peer_file, "r", encoding="utf-8") as f:
            return str(json.load(f).get("peer", "")).strip()
    except (OSError, ValueError):
        return ""


def save_last_peer(peer):
    try:
        with open(_peer_file, "w", encoding="utf-8") as f:
            json.dump({"peer": peer}, f, ensure_ascii=False)
    except OSError:
        pass


def local_ip_toward(peer, port):
    """本机发往对方时用的源地址（提示用户把该地址填到对端）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, port))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_ip(peer_hint="", port=DEFAULT_PORT):
    """报出本机在局域网里的地址：对端已知时最准，否则用通用兜底（UDP connect 不发包）"""
    if peer_hint:
        ip = local_ip_toward(peer_hint, port)
        if ip != "127.0.0.1":
            return ip
    for probe in ("8.8.8.8", "1.1.1.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "未知"


# ----------------------------- 主流程 -----------------------------

def main():
    ap = argparse.ArgumentParser(
        description="局域网截图互传：双方各跑一份，Ctrl+` 发整屏给对方，收到自动存 runtime/shots/")
    ap.add_argument("peer", nargs="?", help="对方 IP（省略则提示输入；直接回车用上次记录）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="监听/发送端口（默认 %d，两端必须一致）" % DEFAULT_PORT)
    ap.add_argument("--send-port", type=int, default=None,
                    help="发送目标端口（默认同 --port；仅特殊网络/测试时需要）")
    ap.add_argument("-d", "--save-dir", metavar="PATH", default=None,
                    help="自定义接收保存目录（两端通用；支持 ~ 与相对路径；默认为 runtime/shots/）")
    ap.add_argument("--cooldown", type=float, default=FIRE_COOLDOWN, metavar="SEC",
                    help="两次发送的最小间隔秒数，防按住/连按刷屏（默认 2，0=不节流）")
    ap.add_argument("--file", metavar="PATH",
                    help="调试：不截屏，直接发送本地 PNG 文件")
    args = ap.parse_args()

    logsetup.setup("screenpeer")
    logsetup.banner(log, "screenpeer")

    send_port = args.send_port or args.port
    cooldown = max(0.0, args.cooldown)

    global _save_dir_override
    if args.save_dir:
        _save_dir_override = os.path.abspath(os.path.expanduser(args.save_dir))

    # 先起监听并立刻显示本机地址：脚本一跑起来对方就能发来，不必等输完对方 IP
    threading.Thread(target=server_loop, args=(args.port,), daemon=True).start()
    time.sleep(0.15)

    saved = load_last_peer()
    my_ip = local_ip((args.peer or "").strip() or saved, args.port)
    print("=== screenpeer ===")
    print("本机地址: %s:%d   ← 请把这个告诉对方" % (my_ip, args.port))
    try:
        os.makedirs(save_dir(), exist_ok=True)
        print("接收保存目录: %s" % save_dir())
    except OSError as exc:
        print("[警告] 保存目录不可用: %s（%s）——收到文件会保存失败" % (save_dir(), exc))

    peer = (args.peer or "").strip()
    if not peer:
        hint = "（回车 = %s）" % saved if saved else ""
        while not peer:
            peer = input("对方 IP%s: " % hint).strip()
            if not peer and saved:
                peer = saved
    save_last_peer(peer)

    state = {"peer": peer, "send_port": send_port}
    print("当前对端: %s:%d" % (peer, send_port))
    real_ip = local_ip_toward(peer, send_port)
    if real_ip != my_ip and not real_ip.startswith("127."):
        print("提示: 到对端的实际本机地址是 %s:%d，请告诉对方这个" % (real_ip, args.port))

    def fire(source):
        now = time.time()
        gap = now - _last_fire[0]
        if gap < cooldown:                           # 冷却节流（--cooldown 可调）
            # 静默丢弃会让人以为「按了没反应」，落一行日志留证据
            log.warning("触发被节流跳过（%s，距上次 %.2fs < %.2fs）", source, gap, cooldown)
            print("[节流] 距离上次截图仅 %.2fs（< %.2fs），本次忽略" % (gap, cooldown))
            return
        _last_fire[0] = now
        log.info("触发截屏（%s）", source)
        threading.Thread(target=_send_work, args=(state, source), daemon=True).start()

    intercepted = start_hotkey(lambda: fire("Ctrl+`"))
    if intercepted:
        print("[热键] Ctrl+` 已启用：按键在系统层被拦截（前台程序收不到），按下即截屏发送")
    else:
        print("[注意] Ctrl+` 未被拦截：该按键仍会传到前台程序（原因见上方提示）")
    print("操作: Ctrl+`/回车=截屏发送   ip <IP>=更换对端   q=退出")

    if args.file:                                    # 调试模式：发送即退出
        try:
            with open(args.file, "rb") as f:
                data = f.read()
        except OSError as exc:
            print("[调试] 读文件失败: %s" % exc)
            return 1
        try:
            send_to(peer, send_port, data)
            print("[发送] %d KB -> %s:%d OK" % (len(data) // 1024, peer, send_port))
        except Exception as exc:
            print("[发送] 失败(%s:%d): %r" % (peer, send_port, exc))
        time.sleep(2)      # 自环调试时本进程就是接收端，给监听线程留落盘时间
        return 0

    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not cmd:
            fire("回车")
        elif cmd.lower() in ("q", "quit", "exit"):
            return 0
        elif cmd.lower().startswith("ip "):
            new = cmd[3:].strip()
            if new:
                state["peer"] = new
                save_last_peer(new)
                print("[对端] -> %s:%d" % (state["peer"], state["send_port"]))
        else:
            print("操作: Ctrl+`/回车=截屏发送   ip <IP>=更换对端   q=退出")


if __name__ == "__main__":
    sys.exit(main())
