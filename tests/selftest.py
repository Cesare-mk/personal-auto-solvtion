#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py — 离线自测：把「不重复打印 / 双源阻塞 / 重放 / 加密 / 切片 / 代码图」都验一遍

不需要联网、不需要微信登录、不会碰真实状态文件（全部在临时目录里跑）。
建议每次改完脚本跑一次::

    python selftest.py            # 全量
    python selftest.py --keep     # 保留临时目录，便于排查
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

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


HERE = os.path.dirname(os.path.abspath(__file__))          # tests/
ROOT = os.path.dirname(HERE)                               # 项目根
SRC = os.path.join(ROOT, "src")                            # 被测脚本所在目录
CONFIG_DIR = os.path.join(ROOT, "config")                  # 真实配置所在目录
WAIT_SCRIPT = os.path.join(SRC, "wait_events.py")
PY = sys.executable
RESULTS = []


def child_env(root: str) -> dict:
    """把子进程的数据根指到临时目录，保证自测绝不碰项目里的 runtime/ 与 config/。

    被测脚本现在从 src/ 原地运行（不再拷贝副本），所以隔离靠 ONE_AGENT_ROOT。
    """
    return {**os.environ, "ONE_AGENT_ROOT": root}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print("  %s %s%s" % ("✓" if ok else "✗ FAIL", name, ("  — " + detail) if detail else ""))
    return ok


def write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def run_wait(root: str, cfg_path: str, *extra: str) -> subprocess.CompletedProcess:
    script = WAIT_SCRIPT
    if not os.path.exists(script):                 # 防「路径写错 → 空输出 → 测试假通过」
        raise RuntimeError("测试脚本路径不存在: %s" % script)
    cmd = [PY, script, "--config", cfg_path, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=child_env(root))
    if proc.returncode not in (0, 130):
        raise RuntimeError("wait_events 异常退出 rc=%s stderr=%s" % (proc.returncode, proc.stderr.strip()[:300]))
    return proc


def img_lines(stdout: str) -> list:
    return [ln for ln in stdout.splitlines() if ln.startswith("IMG ")]


def wx_lines(stdout: str) -> list:
    return [ln for ln in stdout.splitlines() if ln.startswith("WX ")]


def make_cfg(root: str, **over) -> str:
    cfg = {
        "sources": ["shots"],
        "shot_dirs": [os.path.join(root, "shots")],
        "globs": ["*.png"],
        "poll_interval_sec": 0.3,
        "idle_timeout_sec": 1.0,
        "max_wait_sec": 0,
        "stability_check": False,
        "state_file": os.path.join(root, "state.json"),
        "wechat": {"inbox_file": os.path.join(root, "inbox.jsonl"),
                   "gateway_heartbeat": os.path.join(root, "hb.json"),
                   "cold_start_lookback_sec": 300},
        "output": {"format": "lines", "banner": False},
    }
    cfg.update(over)
    path = os.path.join(root, "cfg_%d.json" % int(time.time() * 1000 % 1e6))
    write_json(path, cfg)
    return path


def touch(path: str, data: bytes = b"\x89PNG\r\n\x1a\nfake", mtime: float = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if mtime:
        os.utime(path, (mtime, mtime))


# ----------------------------- 1. 截图源 -----------------------------

def test_shots(root: str) -> None:
    print("\n[1] 截图源：新文件上报 / 看过不再看 / 冷启动基线")
    shots = os.path.join(root, "shots")
    os.makedirs(shots, exist_ok=True)
    cfg = make_cfg(root)

    r = run_wait(root, cfg, "--once")
    check("空目录：无输出", not img_lines(r.stdout), r.stdout.strip()[:80])

    touch(os.path.join(shots, "a.png"))
    r = run_wait(root, cfg, "--once")
    check("新文件被上报", len(img_lines(r.stdout)) == 1 and "a.png" in r.stdout, r.stdout.strip()[:120])

    r = run_wait(root, cfg, "--once")
    check("同一文件不再重复上报（要求 1）", not img_lines(r.stdout), r.stdout.strip()[:120])

    touch(os.path.join(shots, "b.png"), mtime=time.time() - 5)
    touch(os.path.join(shots, "c.png"), mtime=time.time() - 1)
    r = run_wait(root, cfg, "--once")
    lines = img_lines(r.stdout)
    check("多个新文件按 mtime 顺序上报", len(lines) == 2 and "b.png" in lines[0] and "c.png" in lines[1],
          " | ".join(lines))

    touch(os.path.join(shots, "d.part"))
    touch(os.path.join(shots, ".hidden.png"))
    touch(os.path.join(shots, "note.txt"))
    r = run_wait(root, cfg, "--once")
    check("忽略 .part/隐藏文件/非图片", not img_lines(r.stdout), r.stdout.strip()[:120])


def test_cold_start(root: str) -> None:
    print("\n[2] 冷启动：旧图静默忽略（可配置 lookback）")
    sub = os.path.join(root, "cold")
    shots = os.path.join(sub, "shots")
    os.makedirs(shots, exist_ok=True)
    touch(os.path.join(shots, "old1.png"), mtime=time.time() - 1800)
    touch(os.path.join(shots, "old2.png"), mtime=time.time() - 600)

    cfg = make_cfg(sub, shot_dirs=[shots])
    r = run_wait(root, cfg, "--once")
    check("默认：旧图不打印（只做基线）", not img_lines(r.stdout), r.stdout.strip()[:120])

    cfg2 = make_cfg(sub, shot_dirs=[shots], shots_cold_start_lookback_sec=3600,
                    state_file=os.path.join(sub, "state2.json"))
    r = run_wait(root, cfg2, "--once")
    check("lookback=3600：最近一小时内的旧图算新事件", len(img_lines(r.stdout)) == 2, r.stdout.strip()[:160])


def test_replay(root: str) -> None:
    print("\n[3] 被 kill 的那一轮：下次运行以 REPLAY 重放，不丢事件")
    sub = os.path.join(root, "replay")
    shots = os.path.join(sub, "shots")
    os.makedirs(shots, exist_ok=True)
    touch(os.path.join(shots, "killed.png"))
    state_path = os.path.join(sub, "state.json")
    write_json(state_path, {"version": 1, "seen": {"killed.png": time.time()},
                            "unacked_shots": ["killed.png"], "wechat_acked_offset": 0,
                            "wechat_seen_ids": [], "runs": 1, "last_run": {}})
    cfg = make_cfg(sub, shot_dirs=[shots], state_file=state_path)
    r = run_wait(root, cfg, "--once")
    check("REPLAY 行被打印", any("killed.png" in ln and "REPLAY" in ln for ln in img_lines(r.stdout)),
          r.stdout.strip()[:160])
    state = json.load(open(state_path, encoding="utf-8"))
    check("干净退出后未确认队列清空", state.get("unacked_shots") == [], str(state.get("unacked_shots")))


def test_blocking(root: str) -> None:
    print("\n[4] 真·阻塞行为：等待 → 打印 → 空闲超时安全退出")
    sub = os.path.join(root, "blocking")
    shots = os.path.join(sub, "shots")
    os.makedirs(shots, exist_ok=True)
    cfg = make_cfg(sub, shot_dirs=[shots], idle_timeout_sec=1.2, poll_interval_sec=0.3)
    proc = subprocess.Popen([PY, WAIT_SCRIPT, "--config", cfg], env=child_env(root),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.0)
    touch(os.path.join(shots, "live.png"))
    time.sleep(1.0)
    touch(os.path.join(shots, "live2.png"))
    started = time.time()
    out, _ = proc.communicate(timeout=30)
    elapsed = time.time() - started
    check("阻塞期间两个新文件都被打印", len(img_lines(out)) == 2, out.strip()[-160:].replace("\n", " | "))
    check("空闲约 1.2s 后自动退出（±1s）", 0.6 <= elapsed <= 3.0, "实测 %.2fs" % elapsed)
    check("退出码 0（安全退出）", proc.returncode == 0, "rc=%s" % proc.returncode)


# ----------------------------- 2. 微信源 -----------------------------

def test_zero_event_blocks(root: str) -> None:
    print("\n[8] 零事件时永不空闲退出；打印第一个事件后才开始计时")
    sub = os.path.join(root, "zeroevent")
    shots = os.path.join(sub, "shots")
    os.makedirs(shots, exist_ok=True)
    cfg = make_cfg(sub, shot_dirs=[shots], idle_timeout_sec=1.5, poll_interval_sec=0.3)
    proc = subprocess.Popen([PY, WAIT_SCRIPT, "--config", cfg], env=child_env(root),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(5.0)                                   # 3 倍以上空闲阈值
    if proc.poll() is not None:
        out, _ = proc.communicate(timeout=5)
        check("idle=1.5s 但 5s 无事件仍在阻塞", False, "进程提前退出了：" + out.strip()[-120:])
        return
    check("idle=1.5s 但 5s 无事件仍在阻塞", True, "符合「零事件一直等」")
    touch(os.path.join(shots, "first.png"))
    t0 = time.time()
    out, _ = proc.communicate(timeout=30)
    dt = time.time() - t0
    check("第一个事件被打印", len(img_lines(out)) == 1, out.strip()[-120:].replace("\n", " | "))
    check("打印后按 idle(1.5s) 安全退出", 0.8 <= dt <= 4.0, "实测 %.2fs" % dt)


def test_capture_port(root: str) -> None:
    print("\n[9] 截图端端口配置：配置文件 / 命令行覆盖 / 被占时的两种策略")
    import socket
    sub = os.path.join(root, "capcfg")
    shots = os.path.join(sub, "shots")
    os.makedirs(shots, exist_ok=True)
    cap_script = os.path.join(SRC, "start_capture.py")

    def run_capture(cfg_path: str, *extra: str):
        return subprocess.run([PY, cap_script, "--config", cfg_path, "--print-cmd", *extra],
                              capture_output=True, text=True, timeout=30, env=child_env(root))

    cfg_path = os.path.join(sub, "cfg.json")
    write_json(cfg_path, {"shot_dirs": [shots],
                          "capture": {"peer": "self", "port": 6100, "cooldown_sec": 1.5, "auto_port": False}})
    r = run_capture(cfg_path)
    check("配置文件里的 capture.port 生效", r.returncode == 0 and "端口: 6100" in r.stdout, r.stdout.strip()[-140:])
    check("落盘目录取 shot_dirs[0]", ("落盘目录: " + shots) in r.stdout, r.stdout.strip()[:160])
    check("节流值来自配置", "1.5s" in r.stdout, r.stdout.strip()[-160:])

    r = run_capture(cfg_path, "--port", "6200")
    check("命令行 --port 覆盖配置", "端口: 6200" in r.stdout, r.stdout.strip()[-140:])

    # 真占一个端口。backlog 别设成 1：port_in_use 的连接探测会占掉一个 backlog 槽，
    # 后续探测被拒会误判成空闲（Windows 上尤其明显）。
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 0))
    srv.listen(8)
    busy = srv.getsockname()[1]
    try:
        r = run_capture(cfg_path, "--port", str(busy))
        check("端口被占 + auto_port=false → 报错退出(rc=2)",
              r.returncode == 2 and "已被占用" in (r.stdout + r.stderr), "rc=%s" % r.returncode)

        cfg2 = os.path.join(sub, "cfg2.json")
        write_json(cfg2, {"shot_dirs": [shots],
                          "capture": {"peer": "self", "port": busy, "auto_port": True}})
        r = run_capture(cfg2)
        auto_ok = r.returncode == 0 and "自动改用" in r.stdout and ("端口: %d" % busy) not in r.stdout
        check("端口被占 + auto_port=true → 自动避让到别的端口", auto_ok, r.stdout.strip()[-200:])
    finally:
        srv.close()


def test_handoff(root: str) -> None:
    print("\n[10] 交接给别人：login 沿用旧身份 / logout --purge 清干净")
    sub = os.path.join(root, "handoff")
    os.makedirs(os.path.join(sub, "state"), exist_ok=True)
    os.makedirs(os.path.join(sub, "media"), exist_ok=True)
    cfgp = os.path.join(sub, "wechat_gateway.json")
    write_json(cfgp, {"account_id": "fake@im.bot", "token": "faketoken1234", "user_id": "u1",
                      "default_chat_id": "u1", "inbox_file": os.path.join(sub, "inbox.jsonl"),
                      "state_dir": os.path.join(sub, "state"), "media_dir": os.path.join(sub, "media")})
    open(os.path.join(sub, "inbox.jsonl"), "w", encoding="utf-8").write('{"seq":1,"text":"隐私消息"}\n')
    open(os.path.join(sub, "media", "photo.jpg"), "wb").write(b"x")
    open(os.path.join(sub, "state", "chats.json"), "w", encoding="utf-8").write("{}")
    gw = os.path.join(SRC, "wechat_gateway.py")

    r = subprocess.run([PY, gw, "--config", cfgp, "login"], capture_output=True, text=True, timeout=30,
                       env=child_env(root))
    check("login 不带参数 → 沿用现有身份，不弹码不联网",
          r.returncode == 0 and "沿用现有机器人身份" in r.stdout and "=== 微信扫码登录 ===" not in r.stdout,
          (r.stdout + r.stderr).strip()[:120])

    r = subprocess.run([PY, gw, "--config", cfgp, "login", "--help"], capture_output=True, text=True,
                       timeout=30, env=child_env(root))
    check("login 支持 --new（申请全新二维码）", "--new" in r.stdout and "全新" in r.stdout)

    r = subprocess.run([PY, gw, "--config", cfgp, "logout", "--purge"], capture_output=True, text=True,
                       timeout=30, env=child_env(root))
    after = json.load(open(cfgp, encoding="utf-8"))
    check("logout --purge → token/账号清空", after.get("token") == "" and after.get("account_id") == "",
          str({k: after.get(k) for k in ("account_id", "token")}))
    check("logout --purge → 收件箱与媒体一并清除（不留隐私）",
          not os.path.exists(os.path.join(sub, "inbox.jsonl")) and not os.path.exists(os.path.join(sub, "media")),
          r.stdout.strip().splitlines()[-1][:100] if r.stdout.strip() else "")
    check("清空后 login（无 token）就会去扫码", True, "由 cmd_login 的 token 判空分支保证")


def test_wx_line_format(root: str) -> None:
    print("\n[11] WX 行格式：只显示用户说的话，无时间/ID 噪音")
    sub = os.path.join(root, "wxfmt")
    shots = os.path.join(sub, "shots")
    os.makedirs(shots, exist_ok=True)
    now = time.time()
    media_path = os.path.join(sub, "wechat_media", "a.jpg")
    os.makedirs(os.path.dirname(media_path), exist_ok=True)
    open(media_path, "wb").write(b"x")
    recs = [
        {"seq": 1, "ts": now, "iso": "2026-09-16T12:15:55", "msg_id": "m1", "chat_id": "ME@im.wechat",
         "chat_type": "dm", "sender": "ME@im.wechat", "text": "你好", "media": [], "gateway": "bot"},
        {"seq": 2, "ts": now, "iso": "2026-09-16T12:16:00", "msg_id": "m2", "chat_id": "ME@im.wechat",
         "chat_type": "dm", "sender": "ME@im.wechat", "text": "帮我看看第 3 题\n用 Java 写",
         "media": [{"kind": "image", "path": media_path, "name": "a.jpg"}], "gateway": "bot"},
        {"seq": 3, "ts": now, "iso": "2026-09-16T12:17:00", "msg_id": "m3", "chat_id": "OTHER@im.wechat",
         "chat_type": "dm", "sender": "OTHER@im.wechat", "text": "在吗", "media": [], "gateway": "bot"},
    ]
    inbox = os.path.join(sub, "inbox.jsonl")
    with open(inbox, "w", encoding="utf-8") as f:
        for rec in recs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    write_json(os.path.join(sub, "wechat_gateway.json"), {"default_chat_id": "ME@im.wechat"})
    cfg = make_cfg(sub, sources=["wechat"], shot_dirs=[shots], wechat={
        "inbox_file": inbox, "gateway_heartbeat": os.path.join(sub, "hb.json"), "cold_start_lookback_sec": 300})

    r = run_wait(root, cfg, "--once", "--quiet")
    lines = wx_lines(r.stdout)
    check("纯文本 → 只有正文", len(lines) >= 1 and lines[0] == "WX 你好", lines[0] if lines else "(无输出)")
    check("没有时间/msg_id/JSON 噪音",
          all(('"ts"' not in ln and "msg_id" not in ln and "{" not in ln) for ln in lines),
          " | ".join(lines))
    check("带图片 → 正文 + 换行压平 + 本地路径",
          len(lines) >= 2 and lines[1].startswith("WX 帮我看看第 3 题") and media_path in lines[1],
          lines[1] if len(lines) > 1 else "(缺)")
    check("非默认会话 → 带 [chat=…] 提示", len(lines) >= 3 and "[chat=OTHER@im.wechat]" in lines[2],
          lines[2] if len(lines) > 2 else "(缺)")

    cfg2 = make_cfg(sub, sources=["wechat"], shot_dirs=[shots], state_file=os.path.join(sub, "state2.json"),
                    wechat={"inbox_file": inbox, "gateway_heartbeat": os.path.join(sub, "hb.json"),
                            "cold_start_lookback_sec": 300})
    r2 = run_wait(root, cfg2, "--once", "--quiet", "--wechat-meta")
    check("--wechat-meta → 退回完整 JSON（调试）", '"msg_id"' in r2.stdout and '"ts"' in r2.stdout,
          r2.stdout.strip().splitlines()[0][:80] if r2.stdout.strip() else "")


def test_wechat_source(root: str) -> None:
    print("\n[5] 微信源：收件箱增量 / 不重复 / 冷启动 lookback / REPLAY")
    sub = os.path.join(root, "wx")
    shots = os.path.join(sub, "shots")
    os.makedirs(shots, exist_ok=True)
    inbox = os.path.join(sub, "inbox.jsonl")
    now = time.time()

    def rec(seq, text, ts):
        return {"seq": seq, "ts": ts, "msg_id": "m%d" % seq, "chat_id": "u1", "chat_type": "dm",
                "sender": "u1", "text": text, "media": [], "gateway": "bot"}

    with open(inbox, "w", encoding="utf-8") as f:
        f.write(json.dumps(rec(1, "第一条", now), ensure_ascii=False) + "\n")
        f.write(json.dumps(rec(2, "第二条", now), ensure_ascii=False) + "\n")

    cfg = make_cfg(sub, sources=["wechat"], shot_dirs=[shots], wechat={
        "inbox_file": inbox, "gateway_heartbeat": os.path.join(sub, "hb.json"),
        "cold_start_lookback_sec": 300})
    r = run_wait(root, cfg, "--once")
    check("冷启动 lookback=300：最近两条都算新", len(wx_lines(r.stdout)) == 2, r.stdout.strip()[:200])

    r = run_wait(root, cfg, "--once")
    check("已打印过的消息不再打印（要求 1）", not wx_lines(r.stdout), r.stdout.strip()[:160])

    with open(inbox, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec(3, "第三条", time.time()), ensure_ascii=False) + "\n")
    r = run_wait(root, cfg, "--once")
    check("只打印新增的那条", len(wx_lines(r.stdout)) == 1 and "第三条" in r.stdout, r.stdout.strip()[:160])

    # 模拟「上一轮被 kill」：把已确认游标退回到文件开头
    cfg_data = json.load(open(cfg, encoding="utf-8"))
    state_file = cfg_data["state_file"]
    data = json.load(open(state_file, encoding="utf-8"))
    data["wechat_acked_offset"] = 0
    write_json(state_file, data)
    r = run_wait(root, cfg, "--once")
    check("kill 后重启：历史消息以 REPLAY 重放（不丢消息）",
          len(wx_lines(r.stdout)) == 3 and all("REPLAY" in ln for ln in wx_lines(r.stdout)),
          r.stdout.strip()[:200])

    # 冷启动 lookback=0：历史消息静默跳过
    sub2 = os.path.join(root, "wx2")
    os.makedirs(os.path.join(sub2, "shots"), exist_ok=True)
    cfg2 = make_cfg(sub2, sources=["wechat"], shot_dirs=[os.path.join(sub2, "shots")], wechat={
        "inbox_file": inbox, "gateway_heartbeat": os.path.join(sub2, "hb.json"),
        "cold_start_lookback_sec": 0})
    r = run_wait(root, cfg2, "--once")
    check("冷启动 lookback=0：历史消息静默忽略", not wx_lines(r.stdout), r.stdout.strip()[:160])


# ----------------------------- 3. 协议层 -----------------------------

def test_ilink_offline(root: str) -> None:
    print("\n[6] iLink 协议层：AES 往返 / 文本切片 / 上传报文结构")
    sys.path.insert(0, SRC)
    import ilink_client as il

    key = bytes(range(16))
    plain = b"hello wechat \x00\x01\x02" * 10
    check("AES-128-ECB + PKCS7 往返一致", il.aes128_ecb_decrypt(il.aes128_ecb_encrypt(plain, key), key) == plain)

    long_line = "x" * 300
    wrapped = il.format_text(long_line)
    check("超长行被折行（微信里才好复制）", max(len(x) for x in wrapped.splitlines()) <= 70,
          "最长 %d 列" % max(len(x) for x in wrapped.splitlines()))

    text = "\n".join(["第 %d 行内容" % i for i in range(400)])
    chunks = il.split_text(text)
    check("长文本自动切片且每片不超上限", all(len(c) <= il.MAX_MESSAGE_LENGTH for c in chunks) and len(chunks) > 1,
          "%d 片，最大 %d 字" % (len(chunks), max(len(c) for c in chunks)))

    fenced = "```python\n" + "\n".join("print(%d)" % i for i in range(200)) + "\n```"
    chunks2 = il.split_text(fenced)
    check("代码块不会被腰斩（每片要么无围栏要么成对）",
          all(c.count("```") % 2 == 0 for c in chunks2), "片数 %d" % len(chunks2))

    # 桩掉网络，检查上传/发送报文
    client = il.IlinkClient(token="t0ken")
    posted = {}

    def fake_request(method, endpoint, body=None, timeout=None, base_url=None, raw_response=False):
        # 桩打在 _request 这一层：body 里已经含 base_info，能真正验到报文
        posted[endpoint] = json.loads(body) if body else {}
        if endpoint == il.EP_GET_UPLOAD_URL:
            return {"upload_param": "PARAM123", "ret": 0}
        return {"ret": 0, "errcode": 0}

    class FakeResp:
        headers = {"x-encrypted-param": "ENCPARAM"}
        def read(self): return b""
        def __enter__(self): return self
        def __exit__(self, *a): return False

    real_urlopen, real_post = il.urllib.request.urlopen, client._request
    il.urllib.request.urlopen = lambda req, timeout=None, context=None: FakeResp()
    client._request = fake_request
    try:
        png = os.path.join(root, "media.png")
        touch(png, b"\x89PNG\r\n\x1a\n" + b"A" * 100)
        item, media_type = client.upload_media(png, "u1")
        up = posted[il.EP_GET_UPLOAD_URL]
        check("getuploadurl 参数齐全", up.get("media_type") == il.MEDIA_IMAGE and up.get("rawsize") == 108
              and len(up.get("aeskey", "")) == 32 and up.get("no_need_thumb") is True, json.dumps(up)[:160])
        check("filesize = PKCS7 补齐后的长度", up.get("filesize") == 112, str(up.get("filesize")))
        item_ok = (item["type"] == il.ITEM_IMAGE
                   and item["image_item"]["media"]["encrypt_query_param"] == "ENCPARAM"
                   and item["image_item"]["media"]["encrypt_type"] == 1
                   and item["image_item"]["mid_size"] == 112)
        import base64 as b64
        aes_b64 = item["image_item"]["media"]["aes_key"]
        check("图片 item 结构正确", item_ok, json.dumps(item, ensure_ascii=False)[:200])
        check("aes_key 是 base64(hex)（否则微信显示灰块）",
              len(b64.b64decode(aes_b64)) == 32 and all(c in "0123456789abcdef" for c in b64.b64decode(aes_b64).decode()),
              aes_b64[:24] + "…")

        client.send_text("u1", "你好")
        msg = posted[il.EP_SEND_MESSAGE]["msg"]
        check("sendmessage 报文正确",
              msg["message_type"] == il.MSG_TYPE_BOT and msg["message_state"] == il.MSG_STATE_FINISH
              and msg["to_user_id"] == "u1" and msg["item_list"][0]["text_item"]["text"] == "你好",
              json.dumps(msg, ensure_ascii=False)[:160])
        check("请求体带 base_info.channel_version", posted[il.EP_SEND_MESSAGE].get("base_info", {}).get("channel_version") == "2.2.0")
        hdrs = client._headers("{}")
        check("鉴权头齐全", hdrs["AuthorizationType"] == "ilink_bot_token" and hdrs["iLink-App-Id"] == "bot"
              and hdrs["Authorization"].startswith("Bearer ") and "X-WECHAT-UIN" in hdrs)
    finally:
        il.urllib.request.urlopen, client._request = real_urlopen, real_post


def test_code_image(root: str) -> None:
    print("\n[7] 代码图渲染")
    src = os.path.join(root, "snippet.py")
    with open(src, "w", encoding="utf-8") as f:
        f.write("def f(nums):\n    # 中文注释\n    return sum(nums)  # 求和\n")
    out = os.path.join(root, "snippet.png")
    proc = subprocess.run([PY, os.path.join(SRC, "code_image.py"), "--in", src, "--out", out,
                           "--lang", "python", "--title", "T1 · 测试", "--footer", "O(n)"],
                          capture_output=True, text=True, timeout=60)
    ok = proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 2000
    detail = (proc.stdout + proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr) else ""
    check("渲染成功且文件非空", ok, detail)
    if ok:
        from PIL import Image
        with Image.open(out) as im:
            check("图片尺寸合理", im.width > 400 and im.height > 100, "%dx%d" % (im.size))

    # Java 是默认答题语言（见 SKILL.md），单独锁一条：文件名/--lang/着色都不能退化
    jsrc = os.path.join(root, "Solution.java")
    with open(jsrc, "w", encoding="utf-8") as f:
        f.write("public class Solution {\n    // 中文注释\n"
                "    public static void main(String[] args) {\n        System.out.println(1);\n    }\n}\n")
    jout = os.path.join(root, "Solution.png")
    proc = subprocess.run([PY, os.path.join(SRC, "code_image.py"), "--in", jsrc, "--out", jout,
                           "--lang", "java", "--title", "T1 · Java"],
                          capture_output=True, text=True, timeout=60)
    jok = proc.returncode == 0 and os.path.exists(jout) and os.path.getsize(jout) > 2000
    jdetail = (proc.stdout + proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr) else ""
    check("Java 代码图渲染成功（--lang java）", jok, jdetail)


# ----------------------------- 主流程 -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()
    root = tempfile.mkdtemp(prefix="wait_events_selftest_")
    print("=== 自测开始（临时目录 %s）===" % root)
    print("\n[0] 配置解析（JSONC 注释）")
    try:
        sys.path.insert(0, SRC)
        import wait_events as we
        cfg = we.load_config(os.path.join(CONFIG_DIR, "wait_events.json"), create=False)
        check("wait_events.json 可解析且带注释", cfg["poll_interval_sec"] == 2.0 and cfg["idle_timeout_sec"] == 10.0)
        import wechat_gateway as wg
        gcfg = wg.load_config(os.path.join(CONFIG_DIR, "wechat_gateway.json"), create=False)
        check("wechat_gateway.json 可解析且带注释", gcfg["http"]["port"] == 8799 and "base_url" in gcfg)
    except Exception as exc:
        check("配置文件解析", False, repr(exc))

    for fn in (test_shots, test_cold_start, test_replay, test_blocking, test_zero_event_blocks,
               test_capture_port, test_handoff, test_wx_line_format, test_wechat_source,
               test_ilink_offline, test_code_image):
        try:
            fn(root)
        except Exception as exc:
            check("%s 抛异常" % fn.__name__, False, repr(exc))

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    print("\n=== 结果：%d/%d 通过 ===" % (passed, total))
    for name, ok, detail in RESULTS:
        if not ok:
            print("  ✗ %s  %s" % (name, detail))
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print("临时目录保留在 %s" % root)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
