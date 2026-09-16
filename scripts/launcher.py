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
import logsetup  # noqa: E402

PID_FILE = paths.runtime_path("state", "launcher_pids.json")
# 启动常驻进程时要有的第三方包（缺一个，对应的脚本就会起不来）
REQUIRED_MODULES = ("mss", "qrcode", "cryptography", "PIL", "pygments")

DAEMONS = (
    # (名字, 脚本, 参数)
    ("微信网关", "wechat_gateway.py", ["serve"]),
    ("截图端", "start_capture.py", []),
)

#: 按命令行找常驻进程用的正则（pgrep / PowerShell 都用它）。
#: 分窄一点是必要的：清网关残留时不能顺手把截图端也杀了。
GW_PATTERN = r"wechat_gateway\.py"
CAPTURE_PATTERN = r"start_capture\.py|screenpeer\.py"
ALL_RESIDENTS_PATTERN = r"wechat_gateway\.py|start_capture\.py|screenpeer\.py"


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


def _scan_stragglers(pattern: str = ALL_RESIDENTS_PATTERN) -> list:
    """按命令行匹配找常驻进程（返回 ``[(pid, cmdline), ...]``）。

    :param pattern: 命令行正则。默认匹配两个常驻；传更窄的表达式可以只找其中一个。

    故意**不匹配 wait_events.py**——那是 Agent 侧的主循环，由 Agent 自己管，
    它有自己的单实例锁（见 ``src/single_instance.py``）。
    """
    if os.name != "nt":
        try:
            proc = subprocess.run(["pgrep", "-af", pattern],
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
        "Where-Object { $_.CommandLine -match '%s' } | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }" % pattern
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


def _wait_ready(check, seconds: float) -> bool:
    """轮询 ``check()`` 直到为真或超时。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.5)
    return bool(check())


def _resolve_conflict(label: str, pattern: str, check, wait_sec: float = 6.0) -> bool:
    """拉起前先腾地方。返回 True = 已有可用实例，别拉起；False = 可以安全拉起了。

    为什么必须做这件事：两个网关同时长轮询会**重复消费同一条消息**，
    两个截图端抢同一个端口会有一个持续报错。之前 `start` 只做「健康就不动」，
    遇到「进程还在、但健康检查不通过」就会直接再拉一个 —— 正好踩这个坑。

    顺序上有讲究：**先等、再杀**。健康检查失败最常见的原因是
    「另一次 start 刚把它拉起来，还在就绪中」，这时候杀它是误伤。
    真等不到，才认定是残留进程清掉。
    """
    if check():
        return True
    if _wait_ready(check, wait_sec):
        print("[等待] %s 正在就绪中，已可用" % label)
        return True

    killed = 0
    for pid, _cmd in _scan_stragglers(pattern):
        if pid_alive(pid) and kill_pid(pid):
            killed += 1
    if killed:
        print("[清理] 清掉 %d 个残留的 %s 进程（健康检查不通过；不清会和新的抢数据）"
              % (killed, label))
        time.sleep(0.8)
    return False


# ----------------------------- 子命令 -----------------------------

def _ensure_gateway(python: str, pids: dict, quiet: bool = False) -> str:
    """确保网关在跑。返回 ``'already' | 'starting' | 'spawned'``。"""
    gw = gateway_health()
    if gw.get("ok"):
        if not quiet:
            print("[跳过] 微信网关已在跑（account=%s，收件箱 %s 条）"
                  % (gw.get("account_id") or "-", (gw.get("inbox") or {}).get("count", "?")))
        return "already"
    if _resolve_conflict("微信网关", GW_PATTERN, gateway_health):
        if not quiet:
            print("[跳过] 微信网关刚启动，已就绪")
        return "starting"
    pids["微信网关"] = _spawn(python, "wechat_gateway.py", ["serve"], "微信网关")
    if not quiet:
        print("[启动] 微信网关 -> 新窗口（长轮询 + HTTP API 127.0.0.1:8799）")
    return "spawned"


def _ensure_capture(python: str, pids: dict, quiet: bool = False) -> str:
    """确保截图端在跑。返回 ``'already' | 'starting' | 'spawned'``。"""
    cport = capture_port()
    check = lambda: port_listening(cport)               # noqa: E731
    if check():
        if not quiet:
            print("[跳过] 截图端已在跑（端口 %d 在监听）" % cport)
        return "already"
    if _resolve_conflict("截图端", CAPTURE_PATTERN, check):
        if not quiet:
            print("[跳过] 截图端刚启动，端口 %d 已就绪" % cport)
        return "starting"
    pids["截图端"] = _spawn(python, "start_capture.py", [], "截图端")
    if not quiet:
        print("[启动] 截图端 -> 新窗口（Ctrl+` 截图落进 runtime/shots/）")
    return "spawned"


def cmd_start(_args) -> int:
    python = find_interpreter()
    pids = load_pids()

    _ensure_gateway(python, pids)
    _ensure_capture(python, pids)

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
    print("想让它俩掉了自动重起：python scripts/launcher.py watch")
    return 0


#: 看门狗对同一个组件的重启次数上限（窗口内），超了就停手喊人
WATCH_MAX_RESTARTS = 5
WATCH_WINDOW_SEC = 600.0


def cmd_watch(args) -> int:
    """看门狗：周期性确认两个常驻还活着，掉了就补起来。

    为什么需要：常驻进程可能在任何时刻静默死掉（见 docs/ADAPTATION.md 里
    2026-09-16 那次），而唯一症状是「按了 Ctrl+` 没反应」——等人发现时
    考试已经过去几分钟了。

    为什么是前台进程而不是后台线程：看门狗必须活在**被监控进程之外**才有意义。
    它不自己开新窗口，由调用方决定怎么跑（会话里挂后台任务、或用户自己开一个窗口）。
    """
    log = logsetup.setup("watchdog")
    python = find_interpreter()
    interval = max(5.0, float(args.interval))

    log.info("看门狗启动 interval=%.0fs python=%s", interval, python)
    print("=== 看门狗 ===")
    print("每 %.0f 秒检查一次两个常驻，掉了自动补起来。" % interval)
    print("日志: %s" % logsetup.log_path("watchdog"))
    print("Ctrl+C 停止。")

    history: dict = {}          # 组件名 -> [重启时间戳]
    first = True
    try:
        while True:
            pids = load_pids()
            changed = False
            for name, ensure in (("微信网关", _ensure_gateway), ("截图端", _ensure_capture)):
                try:
                    status = ensure(python, pids, quiet=True)
                except Exception:                      # 看门狗自己不能死
                    log.error("%s 检查异常", name, exc_info=True)
                    continue
                if status == "already":
                    continue
                changed = True
                if status == "starting":
                    log.info("%s 正在启动中", name)
                    continue

                # 真要重启了 —— 先看看是不是在反复崩，别把窗口刷爆
                now = time.time()
                hits = [t for t in history.get(name, []) if now - t < WATCH_WINDOW_SEC]
                hits.append(now)
                history[name] = hits
                if len(hits) > WATCH_MAX_RESTARTS:
                    log.critical("%s 在 %.0f 秒内已重启 %d 次，停止自动拉起（去看日志找根因）",
                                 name, WATCH_WINDOW_SEC, len(hits) - 1)
                    print("[看门狗] %s 反复崩溃，已停止自动拉起 —— 看 %s"
                          % (name, logsetup.log_path(name if name == "微信网关" else "capture")))
                    continue
                log.warning("%s 不在了，已重新拉起（窗口内第 %d 次）", name, len(hits))
                print("[%s] %s 掉了 -> 已重新拉起" % (_now_hms(), name))

            if changed:
                try:
                    save_pids(pids)
                except OSError as exc:
                    log.error("PID 记录写入失败: %r", exc)

            if args.once:
                if not changed:
                    print("[%s] 两个常驻都正常" % _now_hms())
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[看门狗] 停止。")
        log.info("看门狗收到中断，退出")
    finally:
        logsetup.shutdown("watchdog")
    return 0


def _now_hms() -> str:
    return time.strftime("%H:%M:%S")


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

    p = sub.add_parser("watch", help="看门狗：常驻掉了自动补起来（前台运行）")
    p.add_argument("--interval", type=float, default=30.0, help="检查间隔秒（默认 30，最小 5）")
    p.add_argument("--once", action="store_true", help="只检查一次就退出")
    p.set_defaults(func=cmd_watch)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
