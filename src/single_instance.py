# -*- coding: utf-8 -*-
"""单实例锁：防止同一个「单消费者」脚本被重复拉起。

为什么需要
----------
``wait_events.py`` 是「单消费者 + 账本推进」模型：每轮把没打印过的事件打印出来
并推进账本。两个实例并存时，后启动的那个会把事件消费掉、输出却打进一个已经
消失的控制台——事件看起来凭空消失，事后连 ``REPLAY`` 都补不回来。

2026-09-16 实际踩过：上一个 Claude Code 会话（19:45）遗留的 ``wait_events.py``
实例一直活到 73 分钟后才发现，期间它一直在和 Agent 抢事件。

设计取舍
--------
* **锁文件里写 PID，而不是用系统文件锁。** Windows 上 ``msvcrt.locking`` 的语义
  和 POSIX 不一致；而且进程被 ``taskkill /F`` 时文件锁不会留下任何有用信息。
  PID 能直接告诉使用者「谁占着、什么时候起的、命令行是什么」。
* **持有者已死就自动接管。** 崩溃/强杀留下的陈旧锁不该需要人工清理。
* **只读模式不取锁**（``--status`` / ``--reset`` / ``--once``）：它们是诊断入口，
  被锁挡住反而会让「进程到底在不在」这件事更难查。

注意 Windows 上的一个坑：``D:\\condaEnv\\env\\Scripts\\python.exe`` 是个 shim，
会再拉起真正的解释器干活（``taskkill`` 的输出能直接看到这层父子关系）。
本模块记录的是 ``os.getpid()``，也就是**真正在跑脚本的那个进程**。
"""

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple

import paths

#: 判定「上一个持有者已经死了」时，最多等多久（秒）
_TAKEOVER_WAIT = 5.0


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。**不判断它是不是同一个脚本**——PID 会被系统复用，
    但对一个「人看着的常驻锁」来说，宁可误报占用也不要误判为空。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        return ('"%d"' % pid) in (proc.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在但不属于我们
    except OSError:
        return False
    return True


def _kill(pid: int) -> bool:
    """杀进程树。Windows 上用 ``/T`` 连 shim 的子进程一起带走。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            import signal as _signal
            os.kill(pid, _signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class SingleInstance:
    """基于 PID 锁文件的单实例守卫。

    典型用法::

        guard = SingleInstance("wait_events")
        holder = guard.acquire(takeover=args.takeover)
        if holder is not None:
            print(guard.explain(holder), file=sys.stderr)
            return 3
        try:
            ...主循环...
        finally:
            guard.release()
    """

    def __init__(self, name: str, path: Optional[str] = None) -> None:
        self.name = name
        self.path = path or paths.runtime_path("state", name + ".lock")
        self.pid = os.getpid()
        self._held = False

    # ----------------------------- 读 -----------------------------

    def holder(self) -> Optional[Dict[str, Any]]:
        """读锁文件。文件不存在/读坏/不是 dict 都返回 None。"""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def live_holder(self) -> Optional[Dict[str, Any]]:
        """返回**仍然活着**的持有者记录；没有则 None。

        注意「自己」分两种情况，别混：

        * ``_held`` 为真 —— 同一个守卫对象重复 ``acquire()``，幂等放行；
        * ``_held`` 为假 —— 同一进程里又 new 了一个守卫。这也是重复消费者，
          **必须拒绝**：放行会让「已经有实例在跑」这个判断形同虚设。
        """
        info = self.holder()
        if not info:
            return None
        pid = int(info.get("pid") or 0)
        if pid == self.pid and self._held:
            return None                      # 自己已持有：幂等
        return info if _pid_alive(pid) else None

    # ----------------------------- 取 -----------------------------

    def acquire(self, takeover: bool = False) -> Optional[Dict[str, Any]]:
        """尝试取锁。

        返回 ``None`` 表示取到了；返回一个 dict 表示**没取到**，里面是占用者的
        信息（``pid`` / ``started`` / ``argv`` / ``cwd``），交给调用方决定怎么报。
        """
        holder = self.live_holder()
        if holder is not None:
            target = int(holder.get("pid") or 0)
            # --takeover 也不能对自己开枪：同进程重复取锁一律拒绝。
            if not takeover or target == self.pid:
                return holder
            _kill(target)
            deadline = time.time() + _TAKEOVER_WAIT
            while time.time() < deadline and _pid_alive(target):
                time.sleep(0.2)

        self._write()
        # 竞态兜底：两个进程同时发现「没人占」并同时写入时，后写的赢。
        # 再读一次确认赢的是自己，否则老实认输。
        info = self.holder()
        if info and int(info.get("pid") or 0) != self.pid:
            return info
        self._held = True
        return None

    def _write(self) -> None:
        payload = {
            "pid": self.pid,
            "name": self.name,
            "started": time.time(),
            "started_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            "argv": sys.argv,
            "cwd": os.getcwd(),
        }
        paths.ensure_parent(self.path)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def release(self) -> None:
        """释放锁。**只删自己写的那个**——否则会把接管者的锁误删。"""
        if not self._held:
            return
        info = self.holder()
        if info and int(info.get("pid") or 0) == self.pid:
            try:
                os.remove(self.path)
            except OSError:
                pass
        self._held = False

    # --------------------------- 报错文案 ---------------------------

    def explain(self, holder: Dict[str, Any]) -> str:
        pid = int(holder.get("pid") or 0)
        started = holder.get("started_iso") or "-"
        argv = " ".join(str(x) for x in (holder.get("argv") or [])) or "-"
        cwd = holder.get("cwd") or "-"
        return (
            "[已有一个 %s 在跑，拒绝启动第二个]\n"
            "  PID      : %d\n"
            "  启动时间 : %s\n"
            "  命令行   : %s\n"
            "  工作目录 : %s\n"
            "  锁文件   : %s\n"
            "\n"
            "为什么必须拒绝：%s 是单消费者 + 账本推进。两个实例并存时，\n"
            "旧实例会把事件消费掉、输出却打进一个已经消失的控制台，\n"
            "事件看起来凭空消失，事后连 REPLAY 都补不回来。\n"
            "\n"
            "怎么办：\n"
            "  · 想接管（杀掉旧的）：加 --takeover\n"
            "  · 只想看看     ：taskkill /PID %d /T /F\n"
            % (self.name, pid, started, argv, cwd, self.path, self.name, pid)
        )


def acquire_or_none(name: str, takeover: bool = False
                    ) -> Tuple[Optional[SingleInstance], Optional[str]]:
    """一步到位的便捷封装，返回 ``(guard, 报错文案)``。

    成功时 ``guard`` 非 None、文案为 None；失败时相反。
    """
    guard = SingleInstance(name)
    holder = guard.acquire(takeover=takeover)
    if holder is None:
        return guard, None
    return None, guard.explain(holder)
