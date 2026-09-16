# -*- coding: utf-8 -*-
"""统一路径基准：把「代码在哪」和「数据/配置在哪」拆开。

重构前每个脚本都用 ``HERE = os.path.dirname(os.path.abspath(__file__))``
同时表示这两件事。脚本一旦从仓库根目录移进 ``src/``，``shots/``、``.state/``、
``wechat_media/`` 这些相对路径就会**静默**跟着平移到 ``src/`` 下——不报错，
但状态账本被重置、历史截图与消息会被当成新事件重放。

本模块把两个语义分开：

    SRC_DIR       代码在哪 —— 用来找同目录的 ilink_client / screenpeer
    PROJECT_ROOT  数据在哪 —— 用来找 config/ 与 runtime/

布局::

    one-person-agent/          <- PROJECT_ROOT
    ├─ src/                    <- SRC_DIR
    ├─ config/                 <- CONFIG_DIR
    └─ runtime/                <- RUNTIME_DIR

设 ``ONE_AGENT_ROOT`` 环境变量可覆盖项目根（自测把数据隔离到临时目录时用）。
"""

import os

ROOT_ENV = "ONE_AGENT_ROOT"

SRC_DIR = os.path.dirname(os.path.abspath(__file__))


def _project_root(src_dir: str) -> str:
    override = os.environ.get(ROOT_ENV, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.dirname(src_dir)


PROJECT_ROOT = _project_root(SRC_DIR)
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
RUNTIME_DIR = os.path.join(PROJECT_ROOT, "runtime")


def resolve(value, base: str = "") -> str:
    """把配置里的路径解析成绝对路径。

    ``~`` 会展开；已是绝对路径的原样返回；相对路径基于 ``base``
    （默认 ``PROJECT_ROOT``）。空值返回空串，交给调用方决定兜底。
    """
    if value is None:
        return ""
    text = os.path.expanduser(str(value).strip())
    if not text:
        return ""
    if os.path.isabs(text):
        return text
    return os.path.join(base or PROJECT_ROOT, text)


def config_path(name: str) -> str:
    """``config/`` 下的配置文件路径。"""
    return os.path.join(CONFIG_DIR, name)


def runtime_path(*parts: str) -> str:
    """``runtime/`` 下的路径（运行产物：截图、解题产物、状态、媒体）。"""
    return os.path.join(RUNTIME_DIR, *parts)


def ensure_parent(path: str) -> str:
    """确保 ``path`` 的父目录存在，返回原路径。

    写文件前调用——否则 ``config/`` 尚不存在时 ``open()`` 会抛
    ``FileNotFoundError``（自动写默认配置那条路径原本没兜住）。
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path
