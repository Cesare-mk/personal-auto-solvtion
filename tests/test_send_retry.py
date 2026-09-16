# -*- coding: utf-8 -*-
"""send 分型与退避重试的离线自测（不联网）。

跑：scripts/py runtime/work/smoke/test_send_retry.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_root(start):
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, "src", "ilink_client.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise SystemExit("找不到项目根")
        cur = parent


sys.path.insert(0, os.path.join(_find_root(HERE), "src"))
import ilink_client  # noqa: E402
from ilink_client import IlinkClient, IlinkError  # noqa: E402

PASS, FAIL = [], []


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(("[PASS] " if cond else "[FAIL] ") + msg)


# 退避会真的 sleep，测试里全部换成记账
SLEPT = []
ilink_client.time.sleep = lambda s: SLEPT.append(s)
ilink_client.time.time = ilink_client.time.time          # 保持不变


def client(retries=4, retry_delay=0.0):
    c = IlinkClient(base_url="http://example.invalid", retries=retries, retry_delay=retry_delay)
    c._log = lambda *a, **k: None
    return c


print("=== 1. ret=-2 分型 ===")
kind, hint = IlinkClient.classify_ret2({"ret": -2, "errmsg": "prepare failed"})
check(kind == "session_pending", "prepare failed -> session_pending")
check("瞬时" in hint, "hint 不再断言「必须让对方先发消息」")

kind, _ = IlinkClient.classify_ret2({"ret": -2, "errmsg": "too many requests"})
check(kind == "rate_limit", "其他 ret=-2 -> rate_limit")

kind, _ = IlinkClient.classify_ret2({"ret": -2, "errmsg": "no session found"})
check(kind == "session_pending", "no session -> session_pending")

print()
print("=== 2. 退避计划：比原来耐心得多 ===")
base, factor, cap, attempts = ilink_client.backoff_plan("session_pending", 4)
waits = [round(ilink_client.backoff_wait(base, factor, cap, i), 1) for i in range(1, attempts)]
total = sum(waits)
print("     计划等待序列:", waits, "合计 %.0fs" % total)
check(attempts >= 6, "prepare failed 至少重试到第 6 次（原来是 4 次）")
check(total >= 90, "总窗口 >= 90 秒（原实现只有 6 秒）")

base_n, factor_n, cap_n, att_n = ilink_client.backoff_plan("network", 4)
waits_n = [round(ilink_client.backoff_wait(base_n, factor_n, cap_n, i), 1) for i in range(1, att_n)]
print("     网络类:", waits_n)
check(waits_n[0] < waits[0], "网络抖动的首次等待比限流短（快速恢复）")

check(ilink_client.backoff_plan("nonexistent", 2)[3] >= 2, "未知类型有兜底计划")

print()
print("=== 3. prepare failed 会自动重试，且最终成功 ===")
SLEPT.clear()
c = client()
calls = {"n": 0}


def flaky(to_user_id, item_list, token, client_id=None):
    calls["n"] += 1
    if calls["n"] < 3:
        return {"ret": -2, "errmsg": "prepare failed"}
    return {"ret": 0}


c.send_items = flaky
resp = c.send_text("u1", "你好")
check(resp.get("ret") == 0, "前两次拒绝、第三次成功后正常返回")
check(calls["n"] == 3, "确实重试了 2 次")
check(len(SLEPT) == 2, "每次失败都等待了")
check(SLEPT[0] == 10.0, "首次等待 10 秒（不是原来的 1 秒）")

print()
print("=== 4. 一直失败时要放弃，并带上分型与建议 ===")
SLEPT.clear()
c = client()
c.send_items = lambda *a, **k: {"ret": -2, "errmsg": "prepare failed"}
try:
    c.send_text("u1", "你好")
    check(False, "应当抛出 IlinkError")
except IlinkError as exc:
    check(True, "最终抛出 IlinkError")
    check(exc.kind == "session_pending", "kind 传到了最外层（%s）" % exc.kind)
    check(exc.retryable is False, "最终异常标记为不可再重试")
    check(bool(exc.hint), "带着可执行的 hint")
    check(len(SLEPT) == 5, "共尝试 6 次、等待 5 次（实际 %d）" % len(SLEPT))

print()
print("=== 5. 服务端明确拒绝不重试 ===")
SLEPT.clear()
c = client()
calls = {"n": 0}


def bad(*a, **k):
    calls["n"] += 1
    return {"ret": 40001, "errmsg": "invalid parameter"}


c.send_items = bad
try:
    c.send_text("u1", "你好")
    check(False, "应当抛出 IlinkError")
except IlinkError as exc:
    check(exc.kind == "server", "kind=server")
    check(calls["n"] == 1, "只试了一次，没有白白退避（实际 %d 次）" % calls["n"])
    check(SLEPT == [], "一次都没 sleep")

print()
print("=== 6. 会话过期：摘掉 context_token 重试 ===")
SLEPT.clear()
c = client()
seen = []


def expires_once(to_user_id, item_list, token, client_id=None):
    seen.append(token)
    if token is not None:
        return {"ret": -14}
    return {"ret": 0}


c.send_items = expires_once
resp = c.send_text("u1", "你好", context_token="CTX")
check(resp.get("ret") == 0, "摘掉 token 后成功")
check(seen == ["CTX", None], "第一次带 token、第二次不带（%r）" % (seen,))
check(SLEPT == [], "换 token 的重试不消耗退避（没 sleep）")

print()
print("=== 7. 网络抖动可重试、协议错误不可重试 ===")
check(IlinkError("x", kind="network", retryable=True).retryable is True, "network 可重试")
check(IlinkError("x", kind="protocol").retryable is False, "protocol 默认不可重试")
check(IlinkError("x").kind == "unknown", "不带参数时 kind=unknown")

print()
print("---- PASS %d / FAIL %d ----" % (len(PASS), len(FAIL)))
if FAIL:
    for m in FAIL:
        print("  失败: " + m)
    sys.exit(1)
