#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wait_events.py — 阻塞式「等新截图 / 等新微信消息」轮询脚本

学习场景的中枢：用户在前台按下截图热键（screenpeer.py 把整屏存进指定文件夹），
或用户在微信里给机器人发消息（wechat_gateway.py 把消息写进收件箱），本脚本都会
**阻塞**在那里等，一旦有新东西就把名字打印出来；等到「空闲超过 N 秒」再安全退出。

设计要点（都是硬要求，别改语义）::

    1. 打印过的名字永不重复打印 —— 状态文件持久化，跨进程、跨重启、跨会话都算数，
       这样 Agent 的上下文不会被同一张图/同一条消息污染。
    2. 双源一次阻塞 —— 截图目录 + 微信收件箱一起等，谁先来打印谁，
       空闲计时器在任意一路有新事件时重置。Agent 只调一个脚本就能同时监听两路。
    3. 干净退出才「确认」输出；被 kill 掉的那一轮，下次运行会以 REPLAY 重放，
       既不丢事件，也不会把正常情况搞乱。
    4. 没有超时 —— 没内容就永远等（可用 --max-wait 设一个硬上限，默认 0=不限）。

输出格式（stdout，逐行，立即 flush）::

    IMG /abs/path/shots/20260916_120001_123.png
    IMG /abs/path/shots/20260916_115900_777.png REPLAY
    WX {"seq":3,"ts":...,"chat_id":"...","chat_type":"dm","sender":"...","text":"...","media":[...]}

用法::

    python wait_events.py                       # 阻塞等待（默认配置）
    python wait_events.py --once                # 只扫一遍就退出（调试/自测）
    python wait_events.py --interval 2 --idle-timeout 10
    python wait_events.py --sources shots       # 只等截图，不等微信
    python wait_events.py --status              # 看状态，不阻塞
    python wait_events.py --reset               # 清空状态（下次会重放未确认项）
    python wait_events.py --json                # 输出 JSON 行（机器解析）

退出码：0=正常（空闲超时安全退出，或 --once 完成）；2=配置/用法错误；130=被中断。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402  （同目录模块，须先补 sys.path）
import single_instance  # noqa: E402


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


VERSION = "1.0.0"
CONFIG_NAME = "wait_events.json"
STATE_VERSION = 1

DEFAULT_CONFIG: Dict[str, Any] = {
    "_readme": "wait_events.py 的配置；相对路径都相对于项目根目录；支持 // 注释",
    "sources": ["shots", "wechat"],
    "shot_dirs": ["runtime/shots"],
    "globs": ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp"],
    "poll_interval_sec": 2.0,
    "idle_timeout_sec": 10.0,
    "max_wait_sec": 0,
    "stability_check": True,
    "ignore_suffixes": [".part", ".tmp", ".crdownload", ".download", ".partial", ".!ut"],
    "ignore_prefixes": [".", "~$", "._"],
    "state_file": "runtime/state/wait_events_state.json",
    "replay_unacked": True,
    "max_seen": 4000,
    "shots_cold_start_lookback_sec": 0,
    "wechat": {
        "inbox_file": "runtime/wechat_inbox.jsonl",
        "gateway_heartbeat": "runtime/state/wechat/heartbeat.json",
        "require_gateway_fresh_sec": 0,
        "cold_start_lookback_sec": 300,
        "max_batch": 20,
    },
    "output": {"format": "lines", "wechat_show_meta": False, "banner": True},
}


# ----------------------------- 配置 -----------------------------

def strip_jsonc(text: str) -> str:
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


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str, create: bool = True) -> Dict[str, Any]:
    if not os.path.exists(path):
        if not create:
            raise SystemExit("配置不存在: %s" % path)
        paths.ensure_parent(path)   # config/ 可能还不存在，直接 open 会 FileNotFoundError
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        print("[配置] 已生成默认配置: %s" % path, file=sys.stderr)
    with open(path, "r", encoding="utf-8") as f:
        return deep_merge(DEFAULT_CONFIG, json.loads(strip_jsonc(f.read()) or "{}"))


def abspath(value: str) -> str:
    """相对路径基于项目根解析（不再是脚本目录），并展开 ~。"""
    return paths.resolve(value)


# ----------------------------- 状态 -----------------------------

class State:
    """跨运行持久化的「已打印/已确认」账本

    shots:  seen      {文件名: mtime}         —— 见过就不再打印
            unacked   [文件名]                —— 打印过但那一轮没干净退出
    wechat: acked_offset / printed_offset / read_offset —— 字节游标
            acked   = 已确认送达 Agent；printed = 至少打印过一次；
            kill 掉的那一轮里 acked < printed，于是重启后 [acked, printed) 这段以 REPLAY 补打。
            seen_ids  []                      —— 去重（iLink 会重复投递同一条）
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: Dict[str, Any] = {
            "version": STATE_VERSION,
            "seen": {},
            "unacked_shots": [],
            "wechat_acked_offset": 0,      # 已确认送达 Agent 的位置
            "wechat_printed_offset": 0,    # 至少打印过一次的位置（被 kill 时用它判定 REPLAY）
            "wechat_seen_ids": [],
            "runs": 0,
            "last_run": {},
        }
        self.fresh = True
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and int(data.get("version", 0)) == STATE_VERSION:
                self.data.update(data)
                self.fresh = False
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)          # 原子替换：kill 也不会写坏账本

    # -- shots --
    def is_seen(self, name: str) -> bool:
        return name in self.data["seen"]

    def mark_seen(self, name: str, mtime: float) -> None:
        self.data["seen"][name] = round(float(mtime), 3)
        limit = int(self.data.get("_max_seen") or 4000)
        seen = self.data["seen"]
        if len(seen) > limit:                # 按时间淘汰最老的，防止无限膨胀
            for key, _ in sorted(seen.items(), key=lambda kv: kv[1])[:len(seen) - limit]:
                seen.pop(key, None)

    def add_unacked(self, name: str) -> None:
        if name not in self.data["unacked_shots"]:
            self.data["unacked_shots"].append(name)

    def clear_unacked(self) -> None:
        self.data["unacked_shots"] = []

    # -- wechat --
    def seen_id(self, msg_id: str) -> bool:
        return bool(msg_id) and msg_id in self.data["wechat_seen_ids"]

    def mark_id(self, msg_id: str) -> None:
        if msg_id and msg_id not in self.data["wechat_seen_ids"]:
            self.data["wechat_seen_ids"].append(msg_id)
            if len(self.data["wechat_seen_ids"]) > 2000:
                self.data["wechat_seen_ids"] = self.data["wechat_seen_ids"][-2000:]


# ----------------------------- 事件 -----------------------------

class Event:
    __slots__ = ("kind", "path", "name", "record", "replay", "hint")

    def __init__(self, kind: str, path: str = "", name: str = "", record: Optional[Dict[str, Any]] = None,
                 replay: bool = False, hint: str = "") -> None:
        self.kind = kind          # "IMG" | "WX"
        self.path = path
        self.name = name
        self.record = record
        self.replay = replay
        self.hint = hint          # 仅非默认会话时填 "[chat=…]"，其余情况为空

    def to_line(self, wechat_meta: bool = False) -> str:
        suffix = " REPLAY" if self.replay else ""
        if self.kind == "IMG":
            return "IMG %s%s" % (self.path, suffix)
        rec = self.record or {}
        if wechat_meta:                        # 调试用：完整 JSON
            return "WX %s%s" % (json.dumps(rec, ensure_ascii=False, separators=(",", ":")), suffix)
        # 默认：只给「用户说了什么」+ 附件本地路径；不带时间/msg_id/sender 等噪音
        line = " ".join(str(rec.get("text") or "").split())
        atts = []
        for media in rec.get("media") or []:
            kind = {"image": "图片", "file": "文件", "voice": "语音", "video": "视频"}.get(media.get("kind"), "附件")
            loc = media.get("path") or media.get("error") or ""
            atts.append("[%s] %s" % (kind, loc))
        if atts:
            line = (line + " " if line else "") + " ".join(atts)
        if self.hint:
            line = (line + " " if line else "") + self.hint
        return "WX %s%s" % (line.strip(), suffix)

    def to_json(self) -> str:
        obj: Dict[str, Any] = {"kind": self.kind, "replay": self.replay}
        if self.kind == "IMG":
            obj.update({"path": self.path, "name": self.name})
        else:
            obj.update(self.record or {})
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ----------------------------- 截图源 -----------------------------

class ShotSource:
    def __init__(self, cfg: Dict[str, Any], state: State) -> None:
        self.cfg = cfg
        self.state = state
        self.dirs = [abspath(d) for d in cfg.get("shot_dirs") or []]
        self.globs = list(cfg.get("globs") or ["*.png"])
        self.ignore_suffixes = tuple(str(s).lower() for s in (cfg.get("ignore_suffixes") or []))
        self.ignore_prefixes = tuple(str(s) for s in (cfg.get("ignore_prefixes") or []))
        self.stability = bool(cfg.get("stability_check", True))
        self.lookback = float(cfg.get("shots_cold_start_lookback_sec", 0) or 0)
        self._prev: Dict[str, Tuple[int, int]] = {}
        for directory in self.dirs:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                print("[警告] 截图目录不可用: %s (%s)" % (directory, exc), file=sys.stderr)

    def baseline(self) -> int:
        """冷启动：目录里已有的文件记为「已见过」，避免把历史截图灌进 Agent 上下文。

        lookback > 0 时，最近 lookback 秒内落盘的图不算旧图（留给本轮上报），
        用于「先按了截图、再让 Agent 起脚本」的场景。
        """
        if not self.state.fresh:
            return 0
        cutoff = time.time() - self.lookback
        kept = 0
        for directory in self.dirs:
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not self._match(name):
                    continue
                path = os.path.join(directory, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path):
                    continue
                if self.lookback > 0 and st.st_mtime >= cutoff:
                    kept += 1                      # 新鲜：交给主循环上报
                    continue
                self.state.mark_seen(name, st.st_mtime)
        return kept

    def _match(self, name: str) -> bool:
        if name.startswith(self.ignore_prefixes):
            return False
        low = name.lower()
        if low.endswith(self.ignore_suffixes):
            return False
        return any(fnmatch.fnmatch(low, g.lower()) for g in self.globs)

    def scan(self) -> List[Tuple[str, str, float]]:
        """返回本次可上报的 (目录, 文件名, mtime)；顺带维护稳定性检查的上一轮快照"""
        ready: List[Tuple[str, str, float]] = []
        current: Dict[str, Tuple[int, int]] = {}
        for directory in self.dirs:
            try:
                entries = os.listdir(directory)
            except OSError:
                continue
            for name in entries:
                path = os.path.join(directory, name)
                if not self._match(name) or not os.path.isfile(path):
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                key = path
                sig = (st.st_size, st.st_mtime_ns)
                current[key] = sig
                if st.st_size <= 0 or self.state.is_seen(name):
                    continue
                if self.stability and self._prev.get(key) != sig:
                    continue                     # 下一轮再看：避免读到半截文件
                ready.append((directory, name, st.st_mtime))
        self._prev = current
        ready.sort(key=lambda item: item[2])     # 按修改时间，先来的先处理
        return ready


# ----------------------------- 微信源 -----------------------------

class WechatSource:
    def __init__(self, cfg: Dict[str, Any], state: State) -> None:
        wcfg = cfg.get("wechat") or {}
        self.cfg = wcfg                       # 注意：self.cfg 只是 wechat 子配置
        self.config_dir = str(cfg.get("_config_dir") or paths.CONFIG_DIR)
        self.state = state
        self.inbox = abspath(str(wcfg.get("inbox_file") or "runtime/wechat_inbox.jsonl"))
        self.heartbeat_path = abspath(str(wcfg.get("gateway_heartbeat") or "runtime/state/wechat/heartbeat.json"))
        self.max_batch = int(wcfg.get("max_batch", 20))
        self.default_chat = self._default_chat()
        self.require_fresh = float(wcfg.get("require_gateway_fresh_sec", 0) or 0)
        self.read_offset = 0
        self.printed_offset = 0
        self.available = os.path.exists(self.inbox)
        self.warning = ""
        self._prepare()

    def _default_chat(self) -> str:
        """读 wechat_gateway.json 的默认收件人：只有「非默认会话」的消息才需要提示 chat_id"""
        try:
            gw = os.path.join(self.config_dir, "wechat_gateway.json")
            with open(gw, "r", encoding="utf-8") as f:
                data = json.loads(strip_jsonc(f.read()) or "{}")
            return str(data.get("default_chat_id") or data.get("user_id") or "")
        except (OSError, ValueError):
            return ""

    # -- 启动时定位游标 --
    def _prepare(self) -> None:
        acked = int(self.state.data.get("wechat_acked_offset", 0) or 0)
        try:
            size = os.path.getsize(self.inbox) if self.available else 0
        except OSError:
            size = 0
        printed = int(self.state.data.get("wechat_printed_offset", acked) or 0)
        if size < acked:                          # 收件箱被轮转/清空过
            acked = 0
        if size < printed:
            printed = 0
        if self.state.fresh:
            lookback = float(self.cfg.get("cold_start_lookback_sec", 300) or 0)
            acked = printed = self._offset_for_lookback(size, lookback)
        self.read_offset = acked                   # 从「上次确认的位置」开始
        self.printed_offset = max(printed, acked)  # 到「至少打印过」的位置为止都算重放
        self.state.data["wechat_acked_offset"] = acked
        self.state.data["wechat_printed_offset"] = self.printed_offset
        self.state.data["wechat_read_offset"] = self.read_offset
        age = self.heartbeat_age()
        if self.available and age is not None:
            self.available = True
            if age > 90 and self.require_fresh:
                self.available = False
                self.warning = "网关心跳已 %.0fs 没更新，微信源暂停（换个 --sources shots 或重启 serve）" % age
        elif not self.available:
            self.warning = "收件箱文件还不存在（wechat_gateway.py serve 跑起来后才有）"

    def _offset_for_lookback(self, size: int, lookback_sec: float) -> int:
        """冷启动：只有最近 lookback 秒内的消息才算「新」，更早的历史消息静默跳过"""
        if lookback_sec <= 0 or size <= 0:
            return size
        cutoff = time.time() - lookback_sec
        offset = size
        try:
            with open(self.inbox, "r", encoding="utf-8") as f:
                pos = 0
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        pos += len(line.encode("utf-8"))
                        continue
                    if float(rec.get("ts") or 0) >= cutoff:
                        offset = pos
                        break
                    pos += len(line.encode("utf-8"))
        except OSError:
            return size
        return offset

    def heartbeat_age(self) -> Optional[float]:
        try:
            with open(self.heartbeat_path, "r", encoding="utf-8") as f:
                hb = json.load(f)
            return max(0.0, time.time() - float(hb.get("ts") or 0))
        except (OSError, ValueError):
            return None

    def gateway_running(self) -> bool:
        age = self.heartbeat_age()
        return age is not None and age <= 90

    # -- 读取新消息 --
    def poll(self) -> List[Tuple[Dict[str, Any], bool]]:
        """返回 [(记录, 是否重放)]；同时把 read_offset 推进到已打印的位置（内存）"""
        if not self.available:
            return []
        try:
            size = os.path.getsize(self.inbox)
        except OSError:
            return []
        if size < self.read_offset:
            self.read_offset = 0
        if size == self.read_offset:
            return []
        out: List[Tuple[Dict[str, Any], bool]] = []
        try:
            with open(self.inbox, "r", encoding="utf-8") as f:
                f.seek(self.read_offset)
                pos = self.read_offset
                for line in f:
                    pos += len(line.encode("utf-8"))
                    raw = line.strip()
                    if not raw:
                        self.read_offset = pos
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        self.read_offset = pos
                        continue
                    # 这一行在「至少打印过一次」的位置之前 = 上一轮打印了但没确认 → 重放
                    replay = pos <= self.printed_offset
                    msg_id = str(rec.get("msg_id") or "")
                    if not replay and self.state.seen_id(msg_id):
                        self.read_offset = pos
                        continue
                    out.append((rec, replay))
                    self.read_offset = pos
                    if pos > self.printed_offset:            # 打印过就记账（被 kill 也不丢）
                        self.printed_offset = pos
                        self.state.data["wechat_printed_offset"] = pos
                    if len(out) >= self.max_batch:
                        break
        except OSError:
            return []
        return out


# ----------------------------- 主循环 -----------------------------

def render(cfg: Dict[str, Any], shots: ShotSource, wechat: WechatSource, state: State,
           interval: float, idle: float, max_wait: float, sources: List[str]) -> None:
    if not (cfg.get("output") or {}).get("banner", True):
        return
    print("=== wait_events v%s ===" % VERSION)
    if "shots" in sources:
        print("截图源: %s  (%s)" % (", ".join(shots.dirs), ", ".join(shots.globs)))
    if "wechat" in sources:
        age = wechat.heartbeat_age()
        status = "网关未运行" if age is None else ("网关心跳 %.0fs 前 ✓" % age if age <= 90 else "网关心跳 %.0fs 前 ⚠" % age)
        print("微信源: %s  (%s)" % (wechat.inbox, status))
        if wechat.warning:
            print("        ⚠ %s" % wechat.warning)
    print("轮询 %.1fs  空闲退出 %.1fs（打印过第一个事件后才开始计时；零事件时一直等下去）  最长等待 %s  状态文件 %s"
          % (interval, idle, ("%.0fs" % max_wait) if max_wait > 0 else "不限", state.path))
    print("等待新事件…（Agent 请保持阻塞，不要设超时）", flush=True)


def run(args: argparse.Namespace) -> int:
    cfg = load_config(os.path.abspath(args.config))
    cfg["_config_dir"] = os.path.dirname(os.path.abspath(args.config))
    for key, value in (("poll_interval_sec", args.interval), ("idle_timeout_sec", args.idle_timeout),
                       ("max_wait_sec", args.max_wait), ("stability_check", args.stability)):
        if value is not None:
            cfg[key] = value
    if args.dir:
        cfg["shot_dirs"] = list(args.dir)
    if args.replay is not None:
        cfg["replay_unacked"] = args.replay
    if args.format:
        cfg.setdefault("output", {})["format"] = args.format
    if args.wechat_meta is not None:
        cfg.setdefault("output", {})["wechat_show_meta"] = args.wechat_meta
    sources = [s for s in (args.sources.split(",") if args.sources else cfg.get("sources") or []) if s]
    if not sources:
        print("[错误] 没有可用的监听源（sources 为空）", file=sys.stderr)
        return 2

    state = State(abspath(str(cfg.get("state_file") or "runtime/state/wait_events_state.json")))
    state.data["_max_seen"] = int(cfg.get("max_seen", 4000))

    if args.reset:
        state.data["seen"] = {}
        state.data["unacked_shots"] = []
        state.data["wechat_seen_ids"] = []
        state.save()
        print("[状态] 已清空 %s（已打印过的名字会被重新视为新事件）" % state.path)
        return 0

    shots = ShotSource(cfg, state) if "shots" in sources else None
    wechat = WechatSource(cfg, state) if "wechat" in sources else None
    if shots is not None and not args.status:
        kept = shots.baseline()                    # 冷启动基线（旧图静默忽略）
        if kept and not args.quiet:
            print("[基线] 目录里有 %d 个最近落盘的图，算新事件上报" % kept, file=sys.stderr)

    if args.status:
        print("=== wait_events 状态 ===")
        print("状态文件: %s（%s）" % (state.path, "首次运行前" if state.fresh else "已存在"))
        print("记录过 %d 个文件名，未确认 %d 个" % (len(state.data["seen"]), len(state.data["unacked_shots"])))
        if shots:
            pending = [n for n in sorted(os.listdir(shots.dirs[0]))] if os.path.isdir(shots.dirs[0]) else []
            print("截图目录: %s（现有 %d 个文件）" % (", ".join(shots.dirs), len(pending)))
        if wechat:
            print("微信收件箱: %s（游标 acked=%s read=%s）"
                  % (wechat.inbox, state.data.get("wechat_acked_offset"), state.data.get("wechat_read_offset")))
        print("最近一次运行: %s" % json.dumps(state.data.get("last_run") or {}, ensure_ascii=False))
        return 0

    interval = max(0.2, float(cfg["poll_interval_sec"]))
    idle = float(cfg["idle_timeout_sec"])
    max_wait = float(cfg["max_wait_sec"])
    started_at = time.monotonic()
    last_event = started_at
    # 关键语义：**一个事件都没打印过时，永不因空闲退出**（一直阻塞等第一个事件）；
    # 打印过之后，才开始「空闲 N 秒 → 安全退出」的计时。
    armed = False
    printed_this_run: List[Event] = []
    stop = {"flag": False, "reason": "signal"}

    def on_signal(signum, _frame):
        stop["flag"] = True
        stop["reason"] = "signal"

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError, AttributeError):
            pass

    if not args.once:
        render(cfg, shots, wechat, state, interval, idle, max_wait, sources)

    # ---- 启动时重放：上一轮打印了但没干净退出的事件 ----
    # 截图源在这里补打；微信源不在这里轮询（否则会把「新消息」误当已消费吞掉），
    # 它的未确认项由主循环第一轮的 poll() 自动带上 replay=True 打印出来。
    if shots and cfg.get("replay_unacked"):
        for name in list(state.data["unacked_shots"]):
            path = next((os.path.join(d, name) for d in shots.dirs if os.path.exists(os.path.join(d, name))), "")
            event = Event("IMG", path=path or name, name=name, replay=True)
            print(event.to_line(), flush=True)
            printed_this_run.append(event)
    if printed_this_run:
        last_event = time.monotonic()
        armed = True                           # 重放也是「打印过」，同样启动空闲计时

    fmt = (cfg.get("output") or {}).get("format", "lines")
    wmeta = bool((cfg.get("output") or {}).get("wechat_show_meta", False))
    emit = (lambda ev: print(ev.to_json() if fmt == "json" else ev.to_line(wmeta), flush=True))

    # ---- 主循环 ----
    while True:
        if stop["flag"]:
            print("--- 收到中断信号：本轮输出未确认，下次运行会以 REPLAY 重放 ---", flush=True)
            return 130
        new_count = 0
        if shots:
            for _directory, name, mtime in shots.scan():
                event = Event("IMG", path=os.path.join(_directory, name), name=name)
                emit(event)
                printed_this_run.append(event)
                state.mark_seen(name, mtime)
                state.add_unacked(name)
                new_count += 1
        if wechat:
            for rec, replay in wechat.poll():
                cid = str(rec.get("chat_id") or "")
                hint = "[chat=%s]" % cid if (wechat.default_chat and cid and cid != wechat.default_chat) else ""
                event = Event("WX", record=rec, replay=replay, hint=hint)
                emit(event)
                printed_this_run.append(event)
                state.mark_id(str(rec.get("msg_id") or ""))
                new_count += 1
        if new_count:
            last_event = time.monotonic()
            armed = True                       # 有输出 → 从此开始算空闲
            state.save()                       # 打印即落盘：被 kill 也不会重复打印
            if args.once:
                break
        elif args.once:
            break

        if max_wait > 0 and (time.monotonic() - started_at) >= max_wait:
            print("--- 到达最长等待 %.0fs（硬上限），退出 ---" % max_wait, flush=True)
            break
        # armed 之前不判空闲：没等到任何事件就一直等下去（这是设计，不是卡死）
        if armed and idle > 0 and (time.monotonic() - last_event) >= idle:
            break
        slept = 0.0
        while slept < interval and not stop["flag"]:
            step = min(0.2, interval - slept)
            time.sleep(step)
            slept += step

    # ---- 干净退出：确认本轮所有输出 ----
    state.clear_unacked()
    if wechat:
        state.data["wechat_acked_offset"] = wechat.read_offset
    state.data["runs"] = int(state.data.get("runs", 0)) + 1
    state.data["last_run"] = {
        "ts": round(time.time(), 3),
        "printed": len(printed_this_run),
        "img": sum(1 for e in printed_this_run if e.kind == "IMG"),
        "wx": sum(1 for e in printed_this_run if e.kind == "WX"),
        "replay": sum(1 for e in printed_this_run if e.replay),
        "idle_timeout": idle,
    }
    state.save()
    if not args.quiet:
        if printed_this_run:
            print("--- 空闲 %.1fs 无新事件，安全退出；本轮输出 %d 条（IMG %d / WX %d，重放 %d）---"
                  % (idle, len(printed_this_run),
                     sum(1 for e in printed_this_run if e.kind == "IMG"),
                     sum(1 for e in printed_this_run if e.kind == "WX"),
                     sum(1 for e in printed_this_run if e.replay)), flush=True)
        else:
            print("--- 本轮 0 条输出（零事件时不会因空闲退出，只可能是 --once / --max-wait / 信号 结束）---",
                  flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="阻塞等待新截图 / 微信新消息，把名字打印出来")
    ap.add_argument("--config", default=paths.config_path(CONFIG_NAME), help="配置文件路径")
    ap.add_argument("--interval", type=float, default=None, help="轮询间隔秒（默认取配置 2.0）")
    ap.add_argument("--idle-timeout", type=float, default=None, help="空闲多久算结束（默认取配置 10.0，0=永不退出）")
    ap.add_argument("--max-wait", type=float, default=None, help="最长等待秒（0=不限，默认取配置）")
    ap.add_argument("--dir", action="append", default=None, help="截图目录，可多次指定（覆盖配置）")
    ap.add_argument("--sources", default=None, help="监听源，逗号分隔: shots,wechat")
    ap.add_argument("--stability", dest="stability", action="store_true", default=None,
                    help="强制开启文件稳定性检查（连续两轮大小/mtime 一致才算就绪）")
    ap.add_argument("--no-stability", dest="stability", action="store_false", help="关闭稳定性检查（快一轮）")
    ap.add_argument("--replay", dest="replay", action="store_true", default=None, help="重放上一轮未确认的输出")
    ap.add_argument("--no-replay", dest="replay", action="store_false", help="不重放未确认输出")
    ap.add_argument("--format", choices=["lines", "json"], default=None, help="输出格式")
    ap.add_argument("--wechat-meta", dest="wechat_meta", action="store_true", default=None,
                    help="WX 行打印完整 JSON（默认只打印消息正文+附件路径）")
    ap.add_argument("--no-wechat-meta", dest="wechat_meta", action="store_false",
                    help="WX 行只打印消息正文（默认行为）")
    ap.add_argument("--once", action="store_true", help="只扫一遍就退出（调试用）")
    ap.add_argument("--status", action="store_true", help="打印状态后退出")
    ap.add_argument("--reset", action="store_true", help="清空状态文件后退出")
    ap.add_argument("--quiet", action="store_true", help="不打印横幅与收尾说明")
    ap.add_argument("--takeover", action="store_true",
                    help="已经有实例在跑时，杀掉它并接管（默认拒绝启动）")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    # --status / --reset / --once 是诊断入口，不取锁：
    # 它们正是用来回答「现在到底有没有实例在跑」的，被锁挡住就查不下去了。
    readonly = bool(args.status or args.reset or args.once)
    guard = None
    if not readonly:
        guard, conflict = single_instance.acquire_or_none("wait_events",
                                                          takeover=args.takeover)
        if conflict:
            print(conflict, file=sys.stderr, flush=True)
            return 3

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n--- 被 Ctrl+C 中断 ---", flush=True)
        return 130
    except Exception as exc:                       # 配置/环境问题：给出可读错误
        print("[错误] %r" % exc, file=sys.stderr)
        return 2
    finally:
        if guard is not None:
            guard.release()


if __name__ == "__main__":
    sys.exit(main())
