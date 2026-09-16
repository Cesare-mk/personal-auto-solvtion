#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""中继的常驻进程管理：起停「微信网关」和「截图端」。

为什么逻辑放在 Python 而不是 .bat：

1. **cmd 会按 GBK 解析 UTF-8 的 .bat**。中文在 UTF-8 里占 3 字节、GBK 里算 2 字节，
   cmd 的逐行读取位置会错位，把后面的文本从中间截断——实测 `wait_events.py`
   被吞成 `ait_events.py`，报「不是内部或外部命令」。所以 .bat 只保留纯 ASCII 外壳。
2. .bat 里的 `^` 续行 + 嵌套引号 + 正则转义叠在一起，写对了也很难读。
3. Python 这里能正常输出中文（自己管编码），也能跨平台。

用法::

    python scripts/launcher.py start     # 起两个常驻（各开一个新窗口）
    python scripts/launcher.py stop      # 停掉它们
    python scripts/launcher.py status    # 看看现在什么在跑
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")

sys.path.insert(0, SRC)
import paths  # noqa: E402

PID_FILE = paths.runtime_path("state", "launcher_pids.json")
# 启动常驻进程时要有的第三方包（缺一个，对应的脚本就会起不来）
REQUIRED_MODULES = ("mss", "qrcode", "cryptography", "PIL", "pygments")

DAEMONS = (
    # (名字, 脚本, 参数)
    ("微信网关", "wechat_gateway.py", ["serve"]),
    ("截图端", "start_capture.py", []),
)


def _console_safe() -> None:
    """Windows 控制台是 GBK：打印 ✓ ⚠ 这类符号会 UnicodeEncodeError 直接崩。

    这里比其它脚本多做一件事——把编码也尽量切到 UTF-8，因为本脚本要打中文。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError, OSError):
                pass


_console_safe()


# ----------------------------- 找解释器 -----------------------------

def _has_deps(python: str) -> bool:
    """这个解释器装了项目依赖吗（缺依赖的跑起来也是白跑）。"""
    code = "import " + ", ".join(REQUIRED_MODULES)
    try:
        proc = subprocess.run([python, "-c", code], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _candidates() -> list:
    """按优先级列出候选解释器。"""
    out = []
    env = os.environ.get("ONE_AGENT_PYTHON", "").strip()
    if env:
        out.append(env)
    out += [
        os.path.join(ROOT, ".venv", "Scripts", "python.exe"),   # Windows venv
        os.path.join(ROOT, ".venv", "bin", "python"),           # POSIX venv
        # 本机现成的环境。换机器时改这一行，或设环境变量 ONE_AGENT_PYTHON。
        r"D:\condaEnv\env\Scripts\python.exe",
        sys.executable,
        shutil.which("python3") or "",
        shutil.which("python") or "",
    ]
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def find_interpreter(verbose: bool = True) -> str:
    tried = []
    for cand in _candidates():
        if not os.path.exists(cand) and not shutil.which(cand):
            tried.append((cand, "不存在"))
            continue
        if _has_deps(cand):
            if verbose:
                print("[解释器] %s" % cand)
                if tried:
                    print("         （跳过了缺依赖的: %s）"
                          % ", ".join("%s(%s)" % (p, why) for p, why in tried))
            return cand
        tried.append((cand, "缺依赖"))
    lines = ["找不到装了项目依赖的 Python 解释器。试过："]
    lines += ["  · %s —— %s" % (p, why) for p, why in tried]
    lines += ["", "装依赖：", '  "<解释器>" -m pip install -r requirements.txt',
              "或指定解释器：set ONE_AGENT_PYTHON=<路径>"]
    raise SystemExit("\n".join(lines))


# ----------------------------- 进程记录 -----------------------------

def load_pids() -> dict:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_pids(data: dict) -> None:
    paths.ensure_parent(PID_FILE)
    tmp = PID_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PID_FILE)


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # tasklist 比 os.kill(pid, 0) 可靠：后者在 Windows 上会真的去发信号
        try:
            proc = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                                  capture_output=True, text=True, errors="replace", timeout=20)
        except (OSError, subprocess.SubprocessError):
            return False
        return ("\"%d\"" % pid) in (proc.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def kill_pid(pid: int) -> bool:
    try:
        if os.name == "nt":
            proc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                  capture_output=True, text=True, errors="replace", timeout=20)
            return proc.returncode == 0
        os.kill(pid, 15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _scan_stragglers() -> list:
    """按命令行匹配找漏网的常驻进程。

    只在 PID 文件对不上时兜底（比如上次是手工起的、或 PID 文件被删了）。
    故意**不匹配 wait_events.py**——那是 Agent 侧的主循环，由 Agent 自己管。
    """
    if os.name != "nt":
        try:
            proc = subprocess.run(["pgrep", "-af", "wechat_gateway|start_capture|screenpeer"],
                                  capture_output=True, text=True, timeout=20)
            out = []
            for line in (proc.stdout or "").splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    out.append((int(parts[0]), parts[1].strip()))
            return out
        except (OSError, subprocess.SubprocessError):
            return []

    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'wechat_gateway\\.py|start_capture\\.py|screenpeer\\.py' } | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-Command", ps],
                              capture_output=True, text=True, errors="replace", timeout=40)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].strip().isdigit():
            out.append((int(parts[0].strip()), parts[1].strip()))
    return out


# ----------------------------- 健康检查 -----------------------------

def gateway_health(timeout: float = 3.0) -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8799/health", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def capture_port() -> int:
    """截图端会用哪个端口（读 config/wait_events.json 的 capture 段）。"""
    cfg_path = paths.config_path("wait_events.json")
    try:
        sys.path.insert(0, SRC)
        from wait_events import strip_jsonc
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.loads(strip_jsonc(f.read()) or "{}")
        return int((cfg.get("capture") or {}).get("port") or 5091)
    except (OSError, ValueError, ImportError):
        return 5091


def port_listening(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ----------------------------- 子命令 -----------------------------

def cmd_start(_args) -> int:
    python = find_interpreter()
    pids = load_pids()

    gw = gateway_health()
    if gw.get("ok"):
        print("[跳过] 微信网关已在跑（account=%s，收件箱 %s 条）"
              % (gw.get("account_id") or "-", (gw.get("inbox") or {}).get("count", "?")))
    else:
        pids["微信网关"] = _spawn(python, "wechat_gateway.py", ["serve"], "微信网关")
        print("[启动] 微信网关 -> 新窗口（长轮询 + HTTP API 127.0.0.1:8799）")

    cport = capture_port()
    if port_listening(cport):
        print("[跳过] 截图端已在跑（端口 %d 在监听）" % cport)
    else:
        pids["截图端"] = _spawn(python, "start_capture.py", [], "截图端")
        print("[启动] 截图端 -> 新窗口（Ctrl+` 截图落进 runtime/shots/）")

    save_pids(pids)

    print()
    print("等一下让网关就绪…")
    for _ in range(15):
        time.sleep(1)
        if gateway_health(timeout=2).get("ok"):
            print("[就绪] 网关 /health 正常")
            break
    else:
        print("[警告] 网关 15 秒内没就绪，去它那个窗口看看报什么错")

    print()
    print("两个常驻已拉起。接下来在 Claude Code 会话里说一句「开始中继」，")
    print("Agent 会自己跑 wait_events.py 阻塞等待；然后按 Ctrl+` 出题即可。")
    print("停止：python scripts/launcher.py stop")
    return 0


def _spawn(python: str, script: str, extra: list, title: str) -> int:
    cmd = [python, os.path.join(SRC, script)] + extra
    kwargs = {"cwd": ROOT}
    if os.name == "nt":
        # 各开一个新控制台窗口：关窗口 = 停进程，日志也看得见
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    return proc.pid


def cmd_stop(_args) -> int:
    pids = load_pids()
    killed, missing = [], []

    for name, pid in list(pids.items()):
        pid = int(pid or 0)
        if pid_alive(pid):
            if kill_pid(pid):
                killed.append("%s (PID %d)" % (name, pid))
            else:
                missing.append("%s (PID %d，杀不掉，可能权限不足)" % (name, pid))
        else:
            missing.append("%s (PID %d，已经不在了)" % (name, pid))

    # 兜底：PID 文件对不上时按命令行扫一遍
    stragglers = [p for p in _scan_stragglers() if p[0] not in {int(v or 0) for v in pids.values()}]
    for pid, cmdline in stragglers:
        if kill_pid(pid):
            killed.append("漏网的 PID %d" % pid)

    if killed:
        print("已停止：")
        for k in killed:
            print("  · %s" % k)
    else:
        print("没有在跑的常驻进程。")
    if missing:
        print("注意：")
        for m in missing:
            print("  · %s" % m)

    save_pids({})
    print()
    print("wait_events.py（Agent 侧的主循环）没被停——那个在 Claude Code 那边中断。")
    return 0


def cmd_status(_args) -> int:
    print("=== 中继常驻进程状态 ===")
    gw = gateway_health()
    if gw.get("ok"):
        print("微信网关  运行中  account=%s  收件箱 %s 条"
              % (gw.get("account_id") or "-", (gw.get("inbox") or {}).get("count", "?")))
    else:
        print("微信网关  未运行（/health 连不上）")

    cport = capture_port()
    print("截图端    %s（端口 %d）" % ("运行中" if port_listening(cport) else "未运行", cport))

    pids = load_pids()
    print()
    if pids:
        print("PID 记录（%s）：" % PID_FILE)
        for name, pid in pids.items():
            print("  %s  PID %s  %s" % (name, pid, "活着" if pid_alive(int(pid or 0)) else "已退出"))
    else:
        print("没有 PID 记录。")

    shots = paths.runtime_path("shots")
    try:
        n = len([f for f in os.listdir(shots) if f.lower().endswith(".png")])
        print("\n截图目录 %s（%d 张图）" % (shots, n))
    except OSError:
        print("\n截图目录 %s（还没建）" % shots)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="中继常驻进程的起停管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="起微信网关 + 截图端").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="停掉它们").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="看看现在什么在跑").set_defaults(func=cmd_status)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
