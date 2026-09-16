# -*- coding: utf-8 -*-
"""单实例锁 + 文件日志的离线自测（不依赖网关、不依赖微信）。

跑：scripts/py runtime/work/smoke/test_lock_log.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_root(start):
    """往上找带 src/paths.py 的那一级，避免写死层数。"""
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, "src", "paths.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise SystemExit("找不到项目根（src/paths.py）")
        cur = parent


ROOT = _find_root(HERE)
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

# 把运行产物隔离到临时目录，别污染真实 runtime/
TMP = tempfile.mkdtemp(prefix="one_agent_lock_test_")
os.environ["ONE_AGENT_ROOT"] = TMP
for mod in ("paths", "single_instance", "logsetup"):
    sys.modules.pop(mod, None)

import paths            # noqa: E402
import single_instance  # noqa: E402
import logsetup         # noqa: E402

PASS, FAIL = [], []


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(("[PASS] " if cond else "[FAIL] ") + msg)


print("=== 单实例锁 ===")
guard = single_instance.SingleInstance("demo")
holder = guard.acquire()
check(holder is None, "首次取锁成功")
check(os.path.exists(guard.path), "锁文件已写出")

info = json.load(open(guard.path, encoding="utf-8"))
check(info["pid"] == os.getpid(), "锁文件记录的 PID == 当前进程")
check("started_iso" in info and "argv" in info, "锁文件含排查所需字段（时间/命令行）")

bis = single_instance.SingleInstance("demo")
holder2 = bis.acquire()
check(holder2 is not None, "同进程内第二个守卫被拒绝")
check(holder2 and holder2["pid"] == os.getpid(), "拒绝时能报出占用者 PID")
msg = bis.explain(holder2)
check("--takeover" in msg and "taskkill" in msg, "报错文案给出了两条处置办法")

print()
print("=== 陈旧锁自动接管 ===")
guard.release()
check(not os.path.exists(guard.path), "释放后锁文件被删除")

# 伪造一个「已经死掉的 PID」占着锁
with open(guard.path, "w", encoding="utf-8") as fh:
    json.dump({"pid": 999999, "name": "demo", "started_iso": "-"}, fh)
stale = single_instance.SingleInstance("demo")
check(stale.acquire() is None, "持有者已死 → 自动接管，不需要人工清理")

print()
print("=== 锁文件损坏时不崩 ===")
with open(guard.path, "w", encoding="utf-8") as fh:
    fh.write("{ 这不是 json")
broken = single_instance.SingleInstance("demo")
check(broken.acquire() is None, "锁文件读坏 → 视为无主，照常取锁")

print()
print("=== 跨进程互斥（真起第二个解释器）===")
# 上一段是「锁文件损坏 → 本进程接管」，先把本进程的锁放掉，
# 否则下面子进程看到的是我们自己的锁，测的就不是跨进程了。
broken.release()
holder_py = os.path.join(TMP, "hold.py")
with open(holder_py, "w", encoding="utf-8") as fh:
    fh.write(
        "import sys, time\n"
        "sys.path.insert(0, r'%s')\n"
        "import single_instance\n"
        "g = single_instance.SingleInstance('demo')\n"
        "assert g.acquire() is None, 'holder failed'\n"
        "print('HELD', flush=True)\n"
        "time.sleep(6)\n" % SRC)
proc = subprocess.Popen([sys.executable, holder_py], stdout=subprocess.PIPE,
                        text=True, env=dict(os.environ))
line = proc.stdout.readline().strip()
check(line == "HELD", "子进程取到锁")

other = single_instance.SingleInstance("demo")
holder3 = other.acquire()
check(holder3 is not None, "另一个**真实进程**持锁时，本进程被拒绝")
check(holder3 and holder3["pid"] != os.getpid(), "报出的 PID 是别的进程，不是本进程")
check(holder3 and single_instance._pid_alive(holder3["pid"]), "报出的 PID 是个活进程")

# 这里刻意不比对 proc.pid：Windows 上 Scripts\python.exe 是个 shim，
# 会再拉起真正的解释器干活。Popen 给的是 **shim** 的 PID，而锁文件里记的是
# 跑脚本的那个真身（os.getpid()）。launcher 的 PID 记录和网关心跳对不上，
# 就是同一个原因。下面把这层关系打印出来，别再让人踩第二次。
if holder3 and holder3["pid"] != proc.pid:
    print("       · 注：Popen.pid=%d 是 shim，锁里记的是真身 pid=%d（预期行为）"
          % (proc.pid, holder3["pid"]))

print()
print("=== --takeover 真的能接管 ===")
took = other.acquire(takeover=True)
check(took is None, "--takeover 接管成功")
time.sleep(0.5)
check(proc.poll() is not None, "原来的持有者进程已被杀掉")
proc.wait(timeout=5)
other.release()

print()
print("=== 文件日志 + 崩溃捕获 ===")
lg = logsetup.setup("smoke")
check(os.path.isdir(paths.runtime_path("logs")), "runtime/logs 已创建")

try:
    raise ValueError("故意炸一个，验证 excepthook 落盘")
except ValueError:
    sys.excepthook(*sys.exc_info())

logfile = logsetup.log_path("smoke")
check(os.path.exists(logfile), "日志文件已生成")
body = open(logfile, encoding="utf-8").read()
check("故意炸一个" in body, "未捕获异常的 traceback 落到了文件里")
check("启动 pid=" in body, "记录了启动时的 pid/python/argv/cwd")

# 后台线程异常（网关轮询线程就属于这一类：主进程活着，线程悄悄死了）
import threading as _th
ev = _th.Event()


def _boom():
    ev.set()
    raise RuntimeError("线程里炸的")


_th.Thread(target=_boom, name="poller").start()
ev.wait(2)
time.sleep(0.4)
logsetup.shutdown("smoke")
body = open(logfile, encoding="utf-8").read()
check("线程里炸的" in body, "后台线程异常也落盘（并标明线程名）")
check("poller" in body, "线程异常记录了线程名")

print()
print("---- PASS %d / FAIL %d ----" % (len(PASS), len(FAIL)))
shutil.rmtree(TMP, ignore_errors=True)
if FAIL:
    for m in FAIL:
        print("  失败: " + m)
    sys.exit(1)
