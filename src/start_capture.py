#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""start_capture.py — 一键启动截图端（screenpeer.py），把截图落到 wait_events 监听的目录

端口、对端、节流、落盘目录全部可以在 ``wait_events.json`` 的 ``capture`` 段里配置
（和轮询脚本共用同一份配置文件，保证「截图落盘目录」永远等于「轮询监听的目录」），
命令行参数优先级更高，可临时覆盖。

    wait_events.json:
      "shot_dirs": ["shots"],
      "capture": {
        "peer": "self",        // "self"=本机自收（截图直接落盘）；或对方 IP（局域网互传）
        "port": 5077,          // 端口；局域网互传时两端必须一致
        "cooldown_sec": 2.0,   // 两次截图最小间隔，防连按刷屏
        "auto_port": true,     // 端口被占：true=自动改用下一个空闲端口；false=直接报错退出
        "save_dir": ""         // 留空 = 用 shot_dirs[0]
      }

用法::

    python start_capture.py                       # 全部走配置
    python start_capture.py --port 6000           # 临时换端口
    python start_capture.py --peer 192.168.1.23   # 局域网互传（记得两端 --port 一致）
    python start_capture.py --no-auto-port        # 端口被占就直接报错，不偷偷换
    python start_capture.py --print-cmd           # 只打印将要执行的命令

macOS 首次运行需要在「系统设置 → 隐私与安全性」里给运行本脚本的终端授权：
屏幕录制（截屏）+ 辅助功能（Ctrl+` 全局热键拦截），授权后重启终端。Windows 无需授权。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402  （同目录模块，须先补 sys.path）
import logsetup  # noqa: E402

#: 文件日志 logger，名字须与 logsetup.setup("capture") 一致
log = logging.getLogger("capture")


def _console_safe() -> None:
    """Windows 控制台默认是 GBK(cp936)：打印 ✓ ✗ ⚠ 这类符号会直接 UnicodeEncodeError 崩掉。
    这里只把错误策略放宽成 replace —— 保留控制台原生编码（中文照常显示），个别符号降级成 ?。
    想全 UTF-8：set PYTHONUTF8=1 或 chcp 65001。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_console_safe()


CONFIG_NAME = "wait_events.json"          # 与轮询脚本共用，避免目录/端口两处配置打架
SCREENPEER = os.path.join(paths.SRC_DIR, "screenpeer.py")
DEFAULT_PORT = 5077
DEFAULT_CAPTURE: Dict[str, Any] = {
    "peer": "self",
    "port": DEFAULT_PORT,
    "cooldown_sec": 2.0,
    "auto_port": True,
    "save_dir": "",
}


# ----------------------------- 配置 -----------------------------

def strip_jsonc(text: str) -> str:
    """允许 // 与 /* */ 注释（JSONC-lite）"""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_jsonc(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.loads(strip_jsonc(f.read()) or "{}")
    except FileNotFoundError:
        print("[提示] 没找到 %s，改用内置默认值（端口 %d）" % (path, DEFAULT_PORT))
    except ValueError as exc:
        print("[提示] %s 解析失败(%s)，改用内置默认值" % (path, exc))
    return {}


def resolve_path(value: str) -> str:
    """相对路径基于项目根解析（不再是脚本目录），并展开 ~。"""
    return paths.resolve(value)


def resolve_settings(cfg: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """命令行 > 配置文件 capture 段 > 内置默认值"""
    cap = {**DEFAULT_CAPTURE, **(cfg.get("capture") or {})}
    peer = (args.peer or cap.get("peer") or "self").strip()
    peer = "127.0.0.1" if peer.lower() in ("self", "local", "localhost", "127.0.0.1") else peer
    port = int(args.port if args.port is not None else (cap.get("port") or DEFAULT_PORT))
    cooldown = float(args.cooldown if args.cooldown is not None else (cap.get("cooldown_sec") or 2.0))
    auto_port = bool(args.auto_port) if args.auto_port is not None else bool(cap.get("auto_port", True))
    if args.dir:
        save_dir = resolve_path(args.dir)
    elif cap.get("save_dir"):
        save_dir = resolve_path(str(cap["save_dir"]))
    else:
        dirs = cfg.get("shot_dirs") or []
        if len(dirs) > 1 and not args.dir:
            print("[提示] wait_events.json 配了多个截图目录，默认用第一个 %s（可用 --dir 指定）" % dirs[0])
        save_dir = resolve_path(str(dirs[0])) if dirs else paths.runtime_path("shots")
    return {"peer": peer, "port": port, "cooldown_sec": cooldown, "auto_port": auto_port,
            "save_dir": save_dir}


# ----------------------------- 端口 -----------------------------

def port_in_use(port: int) -> bool:
    """端口是否被监听。

    先试「连接」再试「绑定」：连接探测在两个平台都准，是主判据。

    兜底的 bind 探测**故意不设 SO_REUSEADDR**：Windows 的语义是「允许绑定已被占用的地址」，
    设了它 bind 永远成功 → 把已占用的端口判成空闲。不设时两平台都会以
    WSAEADDRINUSE / EADDRINUSE 失败，正是探测想要的。代价是 TIME_WAIT 中的端口也算「被占」，
    对监听端口来说这个保守结论是对的。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        try:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


def pick_free_port(start: int, tries: int = 50) -> int:
    for port in range(start + 1, start + 1 + tries):
        if not port_in_use(port):
            return port
    return start


def port_holder(port: int) -> str:
    """谁占着这个端口：macOS/Linux 用 lsof，Windows 用 netstat + tasklist；拿不到就返回空"""
    if sys.platform.startswith("win"):
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                                 timeout=5, errors="replace").stdout
        except (OSError, subprocess.SubprocessError, TypeError):
            return ""
        pids = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(":%d" % port) and parts[3].upper() == "LISTENING":
                if parts[4] not in pids:
                    pids.append(parts[4])
        names = []
        for pid in pids[:2]:
            name = ""
            try:
                t = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"],
                                   capture_output=True, text=True, timeout=5, errors="replace").stdout
                name = t.strip().strip('"').split('","')[0] if t.strip() else ""
            except (OSError, subprocess.SubprocessError, TypeError):
                pass
            names.append("%s (PID %s)" % (name or "?", pid))
        return ", ".join(names)
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-F", "pc"],
                             capture_output=True, text=True, timeout=5, errors="replace").stdout
    except (OSError, subprocess.SubprocessError, TypeError):
        return ""
    pid = cmd = ""
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c"):
            cmd = line[1:]
    return ("%s (PID %s)" % (cmd, pid)) if pid or cmd else ""


def decide_port(settings: Dict[str, Any]) -> Tuple[Optional[int], int]:
    """返回 (最终端口, 退出码)；退出码非 0 表示不该继续"""
    port = int(settings["port"])
    lan = settings["peer"] != "127.0.0.1"
    if not port_in_use(port):
        return port, 0
    holder = port_holder(port)
    hint = "占用者: %s" % holder if holder else ""
    if lan:
        # 局域网互传时端口必须两端一致，绝不能自动换
        print("[警告] 端口 %d 已被占用（%s）—— 局域网互传模式下不改端口，本机将无法接收对方发来的图，"
              "但发送不受影响。要恢复双向请先停掉占用者。" % (port, hint))
        return port, 0
    if not settings["auto_port"]:
        print("[错误] 端口 %d 已被占用（%s），且配置里 auto_port=false。" % (port, hint))
        print("       处理：1) 停掉占用进程；2) 或在 wait_events.json 的 capture.port 换一个端口；")
        print("             3) 或本次用 --port <新端口> 临时指定。")
        return None, 2
    new_port = pick_free_port(port)
    print("[提示] 端口 %d 已被占用（%s），本机自收模式自动改用 %d。" % (port, hint, new_port))
    print("       想固定用 %d：先停掉占用者，或在 wait_events.json 里把 capture.auto_port 设为 false 让它直接报错。"
          % port)
    return new_port, 0


# ----------------------------- 入口 -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="启动 screenpeer 截图端（端口/对端/目录均可在 wait_events.json 配置）")
    ap.add_argument("--config", default=paths.config_path(CONFIG_NAME), help="配置文件（默认 wait_events.json）")
    ap.add_argument("--peer", default=None, help="self=本机自收（默认）；或对方 IP（局域网互传）")
    ap.add_argument("--port", type=int, default=None, help="端口（覆盖配置里的 capture.port，默认 %d）" % DEFAULT_PORT)
    ap.add_argument("--cooldown", type=float, default=None, help="两次截图最小间隔秒（覆盖 capture.cooldown_sec）")
    ap.add_argument("--dir", default="", help="截图落盘目录（覆盖 shot_dirs[0]）")
    ap.add_argument("--auto-port", dest="auto_port", action="store_true", default=None, help="端口被占时自动换端口")
    ap.add_argument("--no-auto-port", dest="auto_port", action="store_false", help="端口被占时直接报错退出")
    ap.add_argument("--print-cmd", action="store_true", help="只打印命令，不执行")
    args = ap.parse_args()

    logsetup.setup("capture")
    logsetup.banner(log, "start_capture")

    if not os.path.exists(SCREENPEER):
        print("[错误] 找不到 screenpeer.py: %s" % SCREENPEER)
        log.error("找不到 screenpeer.py: %s", SCREENPEER)
        return 2

    config_path = os.path.abspath(args.config)
    cfg = load_jsonc(config_path)
    settings = resolve_settings(cfg, args)
    os.makedirs(settings["save_dir"], exist_ok=True)

    port, rc = decide_port(settings)
    if rc:
        return rc

    cmd = [sys.executable, SCREENPEER, settings["peer"], "--port", str(port),
           "--save-dir", settings["save_dir"], "--cooldown", str(settings["cooldown_sec"])]
    print("=== 截图端 ===")
    print("配置文件: %s" % config_path)
    print("模式: %s" % ("本机自收（截图直接落盘，无需第二台机器）" if settings["peer"] == "127.0.0.1"
                        else "局域网发送 → %s（对方也要跑 screenpeer，端口必须同为 %d）" % (settings["peer"], port)))
    print("落盘目录: %s" % settings["save_dir"])
    print("端口: %d%s" % (port, "" if port == settings["port"] else "（配置是 %d，被占后避让）" % settings["port"]))
    print("节流: 两次截图最小间隔 %.1fs" % settings["cooldown_sec"])
    print("热键: Ctrl+`（全局拦截，前台程序收不到这个按键）")
    print("命令: %s" % " ".join(cmd))
    if args.print_cmd:
        return 0
    if sys.platform == "darwin":
        print("[提醒] macOS 首次运行需给本终端授权：屏幕录制 + 辅助功能，授权后重启终端。")
    print("[提示] 保持本进程运行即可；Ctrl+C 停止。")

    # screenpeer 主循环会读 stdin（回车 = 手动截一张），后台运行时 stdin 为空会立刻 EOF 退出。
    # 给它接一根「永不写入」的管道，让它只等热键；父进程退出时管道关闭，它自然收工。
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    except OSError as exc:
        print("[错误] 启动失败: %s" % exc)
        log.error("screenpeer 启动失败: %r", exc)
        return 1
    log.info("screenpeer 已拉起 pid=%d 端口=%d 落盘=%s", proc.pid, port, settings["save_dir"])
    try:
        rc = proc.wait()
        log.warning("screenpeer 退出 rc=%s（截图端主循环结束）", rc)
        return rc
    except KeyboardInterrupt:
        print("\n[截图端] 正在停止…")
        log.info("收到中断，停止 screenpeer pid=%d", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 0


if __name__ == "__main__":
    sys.exit(main())
