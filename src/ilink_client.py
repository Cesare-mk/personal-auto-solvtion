#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ilink_client.py — 微信 iLink Bot API 客户端（纯标准库 + cryptography）

腾讯 iLink Bot API 是微信官方的开放协议（hermes-agent 的微信适配器同样走它）：
个人微信扫码登录后拿到一个机器人身份（形如 ``xxxxxxxx@im.bot``），之后就能通过
HTTPS 主动收发消息——文本、图片、文件、语音都有。因为是公网 HTTPS 长轮询，所以
「跑脚本的电脑」和「手机上的微信」在不在同一个局域网都无所谓。

协议要点（逆向自 hermes-agent/gateway/platforms/weixin.py，本文件独立实现）::

    base      https://ilinkai.weixin.qq.com
    cdn       https://novac2c.cdn.weixin.qq.com/c2c
    POST      ilink/bot/sendmessage   发消息      {"msg": {...}}
    POST      ilink/bot/getupdates    长轮询收信  {"get_updates_buf": "..."}
    POST      ilink/bot/getconfig     取 typing ticket / 会话配置
    POST      ilink/bot/getuploadurl  取媒体上传地址
    POST      ilink/bot/sendtyping    正在输入
    GET       ilink/bot/get_bot_qrcode?bot_type=3
    GET       ilink/bot/get_qrcode_status?qrcode=...

所有 POST 都会带上 ``base_info.channel_version``；请求头必须含
``AuthorizationType: ilink_bot_token`` / ``iLink-App-Id: bot`` /
``iLink-App-ClientVersion`` / ``X-WECHAT-UIN``（base64 的随机 4 字节整数）。

媒体走 AES-128-ECB(PKCS7) 加密的 CDN：先 getuploadurl 拿 upload_param，
再把密文 POST 到 ``{cdn}/upload?encrypted_query_param=..&filekey=..``，从响应头
``x-encrypted-param`` 取回凭据，最后作为 item 的 media.encrypt_query_param 发出去。
注意 ``aes_key`` 字段要填 **base64(十六进制字符串)**，不是 base64(原始字节)，
否则图片在微信里是一片灰块。

本模块不做任何 IO 决策：网络调用同步阻塞、异常统一抛 ``IlinkError``，交给
``wechat_gateway.py`` 组织。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "缺少依赖 cryptography（媒体加解密需要）:\n"
        "  %s -m pip install cryptography\n" % sys.executable
    )
    raise SystemExit(1)

# ----------------------------- 常量 -----------------------------

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0        # 131584

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

# item_list 里的条目类型
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
# getuploadurl 的 media_type
MEDIA_IMAGE, MEDIA_VIDEO, MEDIA_FILE, MEDIA_VOICE = 1, 2, 3, 4

MSG_TYPE_USER, MSG_TYPE_BOT = 1, 2
MSG_STATE_FINISH = 2
TYPING_START, TYPING_STOP = 1, 2

SESSION_EXPIRED_ERRCODE = -14     # 会话过期：去掉 context_token 重试即可
RATE_LIMIT_ERRCODE = -2           # 频率限制：退避后重试

MAX_MESSAGE_LENGTH = 2000         # iLink 单条文本上限（约 2048，留点余量）
SPLIT_THRESHOLD = 1800            # 超过就先切片，避免被服务端硬切
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

_SSL_CTX: Optional[ssl.SSLContext] = None


class IlinkError(RuntimeError):
    """iLink 调用失败（网络、HTTP 或业务 ret/errcode 非 0）"""


# ----------------------------- 通用工具 -----------------------------

def _ssl_context() -> ssl.SSLContext:
    """优先用 certifi 的根证书（部分 Homebrew/自编译 Python 的系统证书链不全会握手失败）"""
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            import certifi
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad = block_size - (len(data) % block_size)
    return data + bytes([pad]) * pad


def aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).encryptor()
    return enc.update(pkcs7_pad(plaintext)) + enc.finalize()


def aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """解密并去掉 PKCS#7 填充（填充不合法时原样返回，容错优先）"""
    dec = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).decryptor()
    out = dec.update(ciphertext) + dec.finalize()
    pad = out[-1] if out else 0
    if 1 <= pad <= 16 and out.endswith(bytes([pad]) * pad):
        return out[:-pad]
    return out


def parse_aes_key(aes_key_b64: str) -> bytes:
    """iLink 的 aes_key 有两种历史形态：base64(16 字节) 或 base64(32 位十六进制串)"""
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    text = decoded.decode("ascii", errors="ignore") if len(decoded) == 32 else ""
    if text and all(c in "0123456789abcdefABCDEF" for c in text):
        return bytes.fromhex(text)
    raise IlinkError("无法解析 aes_key（长度 %d）" % len(decoded))


def _x_wechat_uin() -> str:
    n = int.from_bytes(secrets.token_bytes(4), "big")
    return base64.b64encode(str(n).encode("utf-8")).decode("ascii")


def guess_media_type(path: str) -> Tuple[int, str]:
    """按扩展名判断上传类型 -> (media_type, item_key)"""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return MEDIA_IMAGE, "image_item"
    if ext in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
        return MEDIA_VIDEO, "video_item"
    if ext in {".silk", ".amr"}:
        return MEDIA_VOICE, "voice_item"
    return MEDIA_FILE, "file_item"


def _mime_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp", ".md": "text/markdown", ".txt": "text/plain",
        ".py": "text/x-python", ".json": "application/json", ".pdf": "application/pdf",
    }.get(ext, "application/octet-stream")


# ----------------------------- 文本排版（对齐微信客户端） -----------------------------
# 微信客户端会把超过一屏宽度的行折成很难复制的样子，所以长行主动折行（代码块/表格不动）。

COPY_LINE_WIDTH = 60          # 中文按 2 列算，60 列≈30 个汉字


def _display_width(text: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _wrap_line(line: str, width: int = COPY_LINE_WIDTH) -> List[str]:
    """按显示宽度折行，尽量在空格/标点处断开"""
    if _display_width(line) <= width:
        return [line]
    out, buf, buf_w = [], "", 0
    for ch in line:
        w = 2 if __import__("unicodedata").east_asian_width(ch) in ("W", "F") else 1
        if buf_w + w > width and buf:
            cut = max(buf.rfind(" "), buf.rfind("，"), buf.rfind("、"), buf.rfind(","))
            if cut > width // 2:
                out.append(buf[:cut + 1].rstrip())
                buf = buf[cut + 1:]
            else:
                out.append(buf)
                buf = ""
            buf_w = _display_width(buf)
        buf += ch
        buf_w += w
    if buf:
        out.append(buf)
    return out


def format_text(content: str, wrap: bool = True) -> str:
    """规整即将发出的文本：去行尾空格、压掉连续空行、折超长行（代码块内不动）"""
    lines_out: List[str] = []
    in_code = False
    blank_run = 0
    for raw in (content or "").splitlines():
        line = raw.rstrip()
        is_fence = line.strip().startswith("```")
        if not is_fence and not in_code and not line.strip():
            blank_run += 1
            if blank_run <= 1:
                lines_out.append("")
            continue
        if is_fence or not in_code:
            blank_run = 0
        if wrap and not in_code and not is_fence and line.strip() and not line.lstrip().startswith("|"):
            lines_out.extend(_wrap_line(line))
        else:
            lines_out.append(line)
        if is_fence:
            in_code = not in_code
    return "\n".join(lines_out).strip()


def split_text(content: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """按代码块/段落边界切片，保证代码块不被腰斩"""
    if not content:
        return []
    if len(content) <= max_length:
        return [content]
    blocks: List[str] = []
    cur: List[str] = []
    in_code = False
    for line in content.splitlines():
        is_fence = line.strip().startswith("```")
        if is_fence and not in_code:
            if cur:
                blocks.append("\n".join(cur).strip())
                cur = []
        if is_fence or in_code or line.strip():
            cur.append(line)
        else:
            if cur:
                blocks.append("\n".join(cur).strip())
                cur = []
        if is_fence and in_code:
            blocks.append("\n".join(cur).strip())
            cur = []
        if is_fence:
            in_code = not in_code
    if cur:
        blocks.append("\n".join(cur).strip())
    blocks = [b for b in blocks if b]

    chunks: List[str] = []
    for block in blocks:
        if len(block) <= max_length:
            chunks.append(block)
            continue
        lines = block.splitlines()
        fence = ""
        if lines and lines[0].strip().startswith("```"):
            # 代码块被切开时，每一片都要自带成对围栏，否则微信里渲染不出来
            fence = lines[0].strip()
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
        reserve = (len(fence) + 5) if fence else 0
        part = ""
        for line in lines:
            if part and len(part) + len(line) + 1 + reserve > max_length:
                chunks.append(("%s\n%s\n```" % (fence, part.rstrip())) if fence else part.rstrip())
                part = ""
            part += line + "\n"
        if part.strip():
            chunks.append(("%s\n%s\n```" % (fence, part.rstrip())) if fence else part.rstrip())
    # 合并过短的相邻块（避免刷屏），但仍不越过上限
    merged: List[str] = []
    for chunk in chunks:
        if merged and len(merged[-1]) + len(chunk) + 2 <= max_length and not merged[-1].rstrip().endswith("```"):
            merged[-1] = merged[-1] + "\n\n" + chunk
        else:
            merged.append(chunk)
    return merged or [content[:max_length]]


# ----------------------------- 客户端 -----------------------------

class IlinkClient:
    """iLink Bot API 同步客户端。所有方法失败抛 IlinkError。"""

    def __init__(
        self,
        token: str = "",
        base_url: str = ILINK_BASE_URL,
        cdn_base_url: str = WEIXIN_CDN_BASE_URL,
        timeout: float = 20.0,
        retries: int = 4,
        retry_delay: float = 1.0,
        verbose: bool = False,
    ) -> None:
        self.token = (token or "").strip()
        self.base_url = (base_url or ILINK_BASE_URL).rstrip("/")
        self.cdn_base_url = (cdn_base_url or WEIXIN_CDN_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.verbose = verbose

    # ---------- 底层 HTTP ----------

    def _log(self, msg: str) -> None:
        if self.verbose:
            sys.stderr.write("[ilink] %s\n" % msg)
            sys.stderr.flush()

    def _headers(self, body: str = "") -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _x_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if body:
            headers["Content-Length"] = str(len(body.encode("utf-8")))
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        body: Optional[str] = None,
        timeout: Optional[float] = None,
        base_url: Optional[str] = None,
        raw_response: bool = False,
    ) -> Any:
        url = "%s/%s" % ((base_url or self.base_url).rstrip("/"), endpoint)
        headers = self._headers(body or "")
        if raw_response:
            headers.pop("Content-Type", None)
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=_ssl_context()) as resp:
                payload = resp.read()
                if raw_response:
                    return payload, dict(resp.headers)
                return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read()[:200].decode("utf-8", "replace")
            except Exception:
                pass
            raise IlinkError("HTTP %s %s -> %s %s" % (method, endpoint, exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise IlinkError("网络错误 %s %s -> %s" % (method, endpoint, exc.reason)) from exc
        except json.JSONDecodeError as exc:
            raise IlinkError("响应不是合法 JSON: %s" % exc) from exc

    def _post(self, endpoint: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        body = json.dumps(
            {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
            ensure_ascii=False, separators=(",", ":"),
        )
        return self._request("POST", endpoint, body=body, timeout=timeout)

    def _get(self, endpoint: str, timeout: Optional[float] = None, base_url: Optional[str] = None) -> Dict[str, Any]:
        # QR 相关接口是 GET，只带 App 头，不带 token
        url = "%s/%s" % ((base_url or self.base_url).rstrip("/"), endpoint)
        headers = {"iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)}
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=_ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise IlinkError("HTTP GET %s -> %s" % (endpoint, exc.code)) from exc
        except urllib.error.URLError as exc:
            raise IlinkError("网络错误 GET %s -> %s" % (endpoint, exc.reason)) from exc

    @staticmethod
    def _check(resp: Dict[str, Any], what: str) -> Dict[str, Any]:
        ret = resp.get("ret")
        errcode = resp.get("errcode")
        if (ret not in (None, 0)) or (errcode not in (None, 0)):
            errmsg = resp.get("errmsg") or resp.get("msg") or "unknown error"
            raise IlinkError("%s 失败: ret=%s errcode=%s errmsg=%s" % (what, ret, errcode, errmsg))
        return resp

    @staticmethod
    def _is_session_expired(resp: Dict[str, Any]) -> bool:
        ret, errcode = resp.get("ret"), resp.get("errcode")
        if SESSION_EXPIRED_ERRCODE in (ret, errcode):
            return True
        # ret=-2 且 errmsg="unknown error" 是「会话失效」的另一种表现，不是真的限流
        return (ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE) and \
            str(resp.get("errmsg") or "").lower() == "unknown error"

    @staticmethod
    def _explain_ret2(resp: Dict[str, Any]) -> str:
        """ret=-2 的两种含义：真限流 vs 会话没建立（prepare failed）"""
        errmsg = str(resp.get("errmsg") or resp.get("msg") or "").lower()
        if "prepare" in errmsg or "not found" in errmsg or "no session" in errmsg:
            return ("iLink 拒绝发送（ret=-2 errmsg=%s）：与收件人的会话尚未建立。"
                    "请让接收方先在微信里给机器人发一条消息（机器人才能回话），然后重试。"
                    % (resp.get("errmsg") or resp.get("msg")))
        return "iLink 频率限制（ret=-2 errmsg=%s）：稍后重试" % (resp.get("errmsg") or resp.get("msg"))

    # ---------- 账号 ----------

    def get_config(self, user_id: str, context_token: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"ilink_user_id": user_id}
        if context_token:
            payload["context_token"] = context_token
        return self._check(self._post(EP_GET_CONFIG, payload), "getconfig")

    def typing_ticket(self, user_id: str, context_token: Optional[str] = None) -> str:
        try:
            return str(self.get_config(user_id, context_token).get("typing_ticket") or "")
        except IlinkError:
            return ""

    def send_typing(self, user_id: str, ticket: str, status: int = TYPING_START) -> bool:
        if not ticket:
            return False
        try:
            self._post(EP_SEND_TYPING, {"ilink_user_id": user_id, "typing_ticket": ticket, "status": status},
                       timeout=10.0)
            return True
        except IlinkError:
            return False

    # ---------- 收信 ----------

    def get_updates(self, sync_buf: str = "", timeout_ms: int = 35000) -> Dict[str, Any]:
        """长轮询拉取新消息。超时不是错误，返回空列表即可。"""
        try:
            resp = self._post(EP_GET_UPDATES, {"get_updates_buf": sync_buf}, timeout=timeout_ms / 1000.0 + 5)
        except IlinkError as exc:
            if "timed out" in str(exc).lower():
                return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}
            raise
        if self._is_session_expired(resp):
            raise IlinkError("会话已失效（errcode -14），需要重新扫码登录")
        return self._check(resp, "getupdates")

    def download_media(self, item: Dict[str, Any], timeout: float = 60.0) -> bytes:
        """下载并解密一条入站媒体（图片/文件/语音/视频）"""
        key, payload_key = None, None
        for candidate_key in ("image_item", "file_item", "voice_item", "video_item"):
            if item.get(candidate_key):
                payload_key = candidate_key
                break
        if payload_key is None:
            raise IlinkError("该 item 不含媒体内容")
        payload = item[payload_key] or {}
        media = payload.get("media") or {}
        enc = media.get("encrypt_query_param")
        aes_key_b64 = media.get("aes_key")
        if payload_key == "image_item" and payload.get("aeskey"):
            aes_key_b64 = base64.b64encode(bytes.fromhex(str(payload["aeskey"]))).decode("ascii") or aes_key_b64
        full_url = media.get("full_url")
        if enc:
            url = "%s/download?encrypted_query_param=%s" % (self.cdn_base_url, urllib.parse.quote(str(enc), safe=""))
        elif full_url:
            host = urllib.parse.urlparse(str(full_url)).hostname or ""
            if not host.endswith(".weixin.qq.com"):
                raise IlinkError("拒绝下载非微信 CDN 的媒体地址: %s" % host)
            url = str(full_url)
        else:
            raise IlinkError("媒体项缺少 encrypt_query_param / full_url")
        raw, _headers = self._request("GET", url, timeout=timeout, raw_response=True)
        key = parse_aes_key(str(aes_key_b64)) if aes_key_b64 else None
        return aes128_ecb_decrypt(raw, key) if key else raw

    # ---------- 发信 ----------

    def send_items(
        self,
        to_user_id: str,
        item_list: List[Dict[str, Any]],
        context_token: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        msg: Dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id or ("dpagent-%s" % uuid.uuid4().hex),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": item_list,
        }
        if context_token:
            msg["context_token"] = context_token
        return self._post(EP_SEND_MESSAGE, {"msg": msg})

    def send_text(
        self,
        to_user_id: str,
        text: str,
        context_token: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """发一条文本，内建重试：限流退避、会话过期去掉 context_token 重试"""
        if not (text or "").strip():
            raise IlinkError("文本为空，拒绝发送")
        item_list = [{"type": ITEM_TEXT, "text_item": {"text": text}}]
        last_error: Optional[Exception] = None
        token = context_token
        dropped_token = False
        for attempt in range(self.retries):
            try:
                resp = self.send_items(to_user_id, item_list, token, client_id)
                ret, errcode = resp.get("ret"), resp.get("errcode")
                if (ret not in (None, 0)) or (errcode not in (None, 0)):
                    if self._is_session_expired(resp) and token and not dropped_token:
                        dropped_token, token = True, None
                        self._log("会话过期，去掉 context_token 重试")
                        continue
                    if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE:
                        reason = self._explain_ret2(resp)
                        if "会话尚未建立" in reason:
                            raise IlinkError(reason)          # 重试也没用，直接给可执行的提示
                        wait = self.retry_delay * 3 * (attempt + 1)
                        self._log("%s，%.1fs 后重试" % (reason, wait))
                        time.sleep(wait)
                        last_error = IlinkError(reason)
                        continue
                    raise IlinkError("sendmessage 失败: ret=%s errcode=%s errmsg=%s"
                                     % (ret, errcode, resp.get("errmsg") or resp.get("msg")))
                return resp
            except IlinkError as exc:
                last_error = exc
                if attempt >= self.retries - 1:
                    break
                wait = self.retry_delay * (attempt + 1)
                self._log("发送失败(%s)，%.1fs 后重试 %d/%d" % (exc, wait, attempt + 1, self.retries))
                time.sleep(wait)
        raise IlinkError("发送文本最终失败: %s" % last_error)

    def upload_media(self, path: str, to_user_id: str) -> Tuple[Dict[str, Any], int]:
        """上传一个本地文件到微信 CDN，返回 (可直接放进 item_list 的 item, media_type)"""
        if not os.path.isfile(path):
            raise IlinkError("文件不存在: %s" % path)
        plaintext = open(path, "rb").read()
        if not plaintext:
            raise IlinkError("文件为空: %s" % path)
        media_type, item_key = guess_media_type(path)
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        rawsize = len(plaintext)
        filesize = ((rawsize + 16) // 16) * 16          # PKCS7 一定补齐
        raw_md5 = hashlib.md5(plaintext).hexdigest()

        resp = self._check(self._post(EP_GET_UPLOAD_URL, {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": raw_md5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
        }), "getuploadurl")

        upload_param = str(resp.get("upload_param") or "")
        upload_url = str(resp.get("upload_full_url") or "")
        if not upload_url and upload_param:
            upload_url = "%s/upload?encrypted_query_param=%s&filekey=%s" % (
                self.cdn_base_url, urllib.parse.quote(upload_param, safe=""), urllib.parse.quote(filekey, safe=""))
        if not upload_url:
            raise IlinkError("getuploadurl 既没返回 upload_param 也没返回 upload_full_url: %s" % resp)

        ciphertext = aes128_ecb_encrypt(plaintext, aes_key)
        req = urllib.request.Request(
            upload_url, data=ciphertext, method="POST",
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(ciphertext))})
        try:
            with urllib.request.urlopen(req, timeout=120, context=_ssl_context()) as r:
                encrypted_param = r.headers.get("x-encrypted-param")
                r.read()
        except urllib.error.HTTPError as exc:
            raise IlinkError("CDN 上传失败 HTTP %s" % exc.code) from exc
        except urllib.error.URLError as exc:
            raise IlinkError("CDN 上传网络错误: %s" % exc.reason) from exc
        if not encrypted_param:
            raise IlinkError("CDN 上传响应缺少 x-encrypted-param 头")

        media = {
            "encrypt_query_param": encrypted_param,
            # 关键：aes_key 必须是 base64(hex 字符串)，否则微信里显示灰块
            "aes_key": base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii"),
            "encrypt_type": 1,
        }
        if item_key == "image_item":
            item = {"type": ITEM_IMAGE, "image_item": {"media": media, "mid_size": len(ciphertext)}}
        elif item_key == "file_item":
            item = {"type": ITEM_FILE, "file_item": {
                "media": media, "file_name": os.path.basename(path), "len": str(rawsize)}}
        elif item_key == "video_item":
            item = {"type": ITEM_VIDEO, "video_item": {
                "media": media, "video_size": len(ciphertext), "play_length": 0, "video_md5": raw_md5}}
        else:
            item = {"type": ITEM_VOICE, "voice_item": {"media": media, "playtime": 0}}
        return item, media_type

    def send_media(
        self,
        to_user_id: str,
        path: str,
        context_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        item, _mt = self.upload_media(path, to_user_id)
        resp = self.send_items(to_user_id, [item], context_token)
        ret, errcode = resp.get("ret"), resp.get("errcode")
        if (ret not in (None, 0)) or (errcode not in (None, 0)):
            if self._is_session_expired(resp) and context_token:
                resp = self.send_items(to_user_id, [item], None)
                ret, errcode = resp.get("ret"), resp.get("errcode")
        if (ret not in (None, 0)) or (errcode not in (None, 0)):
            raise IlinkError("发送媒体失败: ret=%s errcode=%s errmsg=%s"
                             % (ret, errcode, resp.get("errmsg") or resp.get("msg")))
        return resp


# ----------------------------- 扫码登录 -----------------------------

def fetch_qr(client: IlinkClient, bot_type: str = "3") -> Tuple[str, str]:
    resp = client._get("%s?bot_type=%s" % (EP_GET_BOT_QR, bot_type), timeout=35.0)
    qrcode_value = str(resp.get("qrcode") or "")
    qrcode_url = str(resp.get("qrcode_img_content") or "")
    if not qrcode_value:
        raise IlinkError("获取二维码失败: %s" % resp)
    return qrcode_value, qrcode_url


def render_qr(qrcode_url: str, png_path: Optional[str] = None) -> None:
    """终端打印字符二维码 + 落一张 PNG（GUI 里看字符画容易错位，PNG 更稳）"""
    if png_path:
        try:
            import qrcode
            img = qrcode.make(qrcode_url)
            img.save(png_path)
            print("二维码已保存: %s" % png_path)
        except Exception as exc:
            print("（二维码 PNG 生成失败: %s）" % exc)
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as exc:
        print("（终端二维码渲染失败: %s，请直接扫上面那张 PNG）" % exc)


def qr_login(
    client: IlinkClient,
    bot_type: str = "3",
    timeout_seconds: int = 480,
    on_qr: Optional[Callable[[str, str], None]] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """扫码登录：返回 {account_id, token, base_url, user_id}

    流程：GET get_bot_qrcode -> 显示二维码 -> 轮询 get_qrcode_status
    状态：wait(待扫) / scaned(已扫待确认) / scaned_but_redirect(换域名) / expired(过期) / confirmed(成功)
    """
    qrcode_value, qrcode_url = fetch_qr(client, bot_type)
    if on_qr:
        on_qr(qrcode_value, qrcode_url)
    else:
        render_qr(qrcode_url)
    deadline = time.monotonic() + timeout_seconds
    base_url, refresh_count, last_status = client.base_url, 0, ""
    while time.monotonic() < deadline:
        try:
            status_resp = client._get("%s?qrcode=%s" % (EP_GET_QR_STATUS, qrcode_value), timeout=35.0, base_url=base_url)
        except IlinkError as exc:
            if on_status:
                on_status("轮询异常: %s" % exc)
            time.sleep(1)
            continue
        status = str(status_resp.get("status") or "wait")
        if status != last_status:
            last_status = status
            if on_status and status in ("scaned", "scaned_but_redirect", "expired"):
                on_status({"scaned": "已扫码，请在手机上确认",
                           "scaned_but_redirect": "已扫码，正在切换接入点…",
                           "expired": "二维码已过期，正在刷新…"}.get(status, status))
        if status == "scaned_but_redirect" and status_resp.get("redirect_host"):
            base_url = "https://%s" % status_resp["redirect_host"]
        elif status == "expired":
            refresh_count += 1
            if refresh_count > 3:
                raise IlinkError("二维码多次过期，请重新执行登录")
            qrcode_value, qrcode_url = fetch_qr(client, bot_type)
            (on_qr or (lambda v, u: render_qr(u)))(qrcode_value, qrcode_url)
        elif status == "confirmed":
            creds = {
                "account_id": str(status_resp.get("ilink_bot_id") or ""),
                "token": str(status_resp.get("bot_token") or ""),
                "base_url": str(status_resp.get("baseurl") or ILINK_BASE_URL),
                "user_id": str(status_resp.get("ilink_user_id") or ""),
            }
            if not creds["account_id"] or not creds["token"]:
                raise IlinkError("扫码已确认，但凭据不完整: %s" % status_resp)
            return creds
        time.sleep(1)
    raise IlinkError("登录超时（%ds 内未确认）" % timeout_seconds)


# ----------------------------- 自测 -----------------------------

if __name__ == "__main__":
    # 手工调试：python ilink_client.py <token> <chat_id> "hello"
    if len(sys.argv) >= 4:
        c = IlinkClient(token=sys.argv[1], verbose=True)
        print(c.send_text(sys.argv[2], sys.argv[3]))
    else:
        c = IlinkClient()
        print("QR:", fetch_qr(c))
