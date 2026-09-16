# -*- coding: utf-8 -*-
"""常驻进程的文件日志 + 崩溃捕获。

为什么需要
----------
2026-09-16 实际踩过：微信网关进程静默死掉，查的时候发现

* ``runtime/`` 下**没有任何日志文件**；
* 唯一的 ``traceback.print_exc()`` 打在它自己那个独立控制台窗口里，而
  ``launcher.py`` 的设计注释写着「关窗口 = 停进程」——窗口一关，日志永久消失；
* Windows 事件日志里也没有记录（**Python 未捕获异常不写事件日志**）。

结论：死因不可考。这个模块就是补这个洞——让「进程为什么没了」事后能查。

做了三件事
----------
1. **文件日志**：``runtime/logs/<name>.log``，带滚动，UTF-8。
2. **未捕获异常落盘**：``sys.excepthook`` / ``threading.excepthook``。
   后者尤其重要——收件轮询跑在后台线程里，线程被异常掀掉时主进程还活着，
   现象是「进程在、但再也不收消息」，比直接崩掉更难查。
3. **原生崩溃落盘**：``faulthandler``，段错误一类也能留痕。

行为约定
--------
* **只加文件，不动控制台。** 现有的 ``print()`` 照旧打在窗口里，不重复、不变样；
  未捕获异常在写日志之后仍会走原来的 traceback 打印。
* **失败静默降级**：日志起不来（目录没权限等）绝不能让常驻进程起不来。
* 重复调用同一 ``name`` 只生效一次。
"""

import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from typing import Optional

import paths

#: 单个日志文件上限（字节）与保留份数
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3

_configured: set = set()
#: 持有 faulthandler 的文件对象引用，防止被 GC 掉后崩溃转储写不进去
_fault_files: list = []
#: 日志文件路径，供调用方打印
_log_paths: dict = {}


def log_path(name: str) -> str:
    """该进程的日志文件路径（不保证已存在）。"""
    return _log_paths.get(name) or paths.runtime_path("logs", name + ".log")


def setup(name: str, *, level: int = logging.INFO,
          max_bytes: int = _MAX_BYTES, backups: int = _BACKUPS) -> logging.Logger:
    """给当前进程装文件日志与崩溃捕获，返回该进程的 logger。

    :param name: 日志名，同时决定文件名 ``runtime/logs/<name>.log``
    """
    logger = logging.getLogger(name)
    if name in _configured:
        return logger

    try:
        log_dir = paths.runtime_path("logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, name + ".log")

        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))

        logger.setLevel(level)
        logger.addHandler(handler)
        logger.propagate = False          # 不往 root 冒泡，免得别的库把噪声带进来
    except OSError as exc:                # 日志起不来绝不能拖垮常驻进程
        print("[日志] 文件日志启用失败（继续运行，仅控制台可见）: %r" % exc,
              file=sys.stderr, flush=True)
        _configured.add(name)
        return logger

    _configured.add(name)
    _log_paths[name] = path
    _install_excepthooks(name, logger)
    _enable_faulthandler(path)

    logger.info("=" * 68)
    logger.info("启动 pid=%d  python=%s", os.getpid(), sys.executable)
    logger.info("argv=%s", " ".join(sys.argv))
    logger.info("cwd=%s", os.getcwd())
    logger.info("日志文件=%s", path)
    return logger


def banner(logger: logging.Logger, role: str) -> None:
    """记一行「我是谁」，方便多进程日志对照。"""
    logger.info("角色=%s 父进程=%s", role, os.getppid())


def shutdown(name: str) -> None:
    """退出前刷盘。放在 ``finally`` 里调用。"""
    logging.getLogger(name).info("退出 pid=%d", os.getpid())
    logging.shutdown()


# --------------------------------------------------------------------------
# 崩溃捕获
# --------------------------------------------------------------------------

def _install_excepthooks(name: str, logger: logging.Logger) -> None:
    original = sys.excepthook

    def _hook(exc_type, exc, tb) -> None:
        # Ctrl+C 是有意为之，不算崩溃，别污染日志
        if issubclass(exc_type, KeyboardInterrupt):
            original(exc_type, exc, tb)
            return
        logger.critical("未捕获异常，进程即将退出\n%s",
                        "".join(traceback.format_exception(exc_type, exc, tb)).rstrip())
        logging.shutdown()
        original(exc_type, exc, tb)       # 保留原来的控制台 traceback

    sys.excepthook = _hook

    def _thread_hook(args) -> None:       # threading.excepthook 需要 3.8+
        if issubclass(args.exc_type, SystemExit):
            return
        thread_name = args.thread.name if args.thread is not None else "?"
        logger.critical("后台线程 %s 未捕获异常（主进程仍在运行，该线程已死）\n%s",
                        thread_name,
                        "".join(traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback)).rstrip())
        logging.shutdown()

    try:
        threading.excepthook = _thread_hook
    except AttributeError:                # 老解释器没有这个钩子，忽略
        pass


def _enable_faulthandler(path: str) -> None:
    """段错误 / 栈溢出这类原生崩溃不会走 Python 的异常钩子，只能靠 faulthandler。"""
    try:
        import faulthandler
        fh = open(path, "a", encoding="utf-8", buffering=1)
        fh.write("\n---- faulthandler 已挂载 %s ----\n"
                 % time.strftime("%Y-%m-%d %H:%M:%S"))
        fh.flush()
        faulthandler.enable(file=fh, all_threads=True)
        _fault_files.append(fh)
    except (ImportError, OSError, ValueError):
        pass


def attach_to(path: str, logger: logging.Logger, *, level: int = logging.INFO) -> Optional[logging.Handler]:
    """给**第三方库**（如 http.server）也接一个文件 handler。

    ``logging.getLogger(name)`` 只覆盖我们自己写的日志；标准库内部走的是 root
    logger。需要把它们的输出也收进同一个文件时调用这个。
    """
    try:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        root = logging.getLogger()
        root.setLevel(level)
        root.addHandler(handler)
        return handler
    except OSError:
        return None
