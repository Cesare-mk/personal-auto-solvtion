# 刷题中继 · 使用说明

把题目放在屏幕上，按一下 `Ctrl+\``，答案会发到你微信。

---

## 一、首次使用（只做一次）

**① 装依赖**（本机已装好，换机器才需要）

先把项目放到任意目录，然后**在该目录下**执行：

```bat
:: 下面的命令都在项目根执行（就是这一份代码所在的目录）
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

> 装完把 `.venv\Scripts\python.exe` 的路径告诉工具：设环境变量
> `set ONE_AGENT_PYTHON=<那个路径>`，或直接改 `scripts\py` / `scripts\py.cmd`
> 里的默认值。两者都不设时，`scripts\py` 会自己去找 `.venv`、再退回 PATH 上的 `python`。

**② 微信扫码登录**（**必须你本人扫**，Agent 不能代扫）

```bat
scripts\py src\wechat_gateway.py login --new
```

**③ 手机上给这个机器人发一条消息**（随便一个字）

> ⚠️ 不先打招呼，机器人**没法主动给你发消息**（会报 `prepare failed`）。

---

## 二、每次使用

### 第 1 步 · 起两个常驻进程

在**项目根**执行：

```bat
scripts\start-all.bat
```

会弹出**两个新窗口**，用完之前别关：

| 窗口 | 干什么 |
|---|---|
| 微信网关 | 收你从微信发来的消息，也负责把答案发回微信 |
| 截图端 | 按 `Ctrl+\`` 时截屏，存到 `runtime\shots\` |

> 这两个进程用 `CREATE_NEW_CONSOLE` 起在各自的窗口里，**通常**能活过 Claude Code 会话；
> 但实测有过「会话断了它们跟着一起没」的情况（`CREATE_NEW_CONSOLE` 不解除父子关系，
> 会话被拆时可能被连带回收）。所以**每次开工先 `scripts\py scripts\launcher.py status`
> 查一眼**，别凭印象。
>
> 进程莫名其妙没了时，先看 `runtime\logs\` 下对应的 `.log` —— 未捕获异常、后台线程异常、
> 段错误都会写进去。

### 第 2 步 · 开 Claude Code，说一句「开始中继」

Agent 会自动做两件事：

1. 检查上面两个常驻在不在跑，**缺哪个补哪个**
2. 进入阻塞等待，盯着截图目录和微信收件箱

> 也可以跳过第 1 步，直接说「开始中继」——Agent 会把两个常驻一起拉起来，效果一样。

### 第 3 步 · 把题目放到屏幕上，按 `Ctrl+\``

按完就等着，答案会发到你微信（思路文字 + 代码截图）。

**注意**：`Ctrl+\`` 会被系统**全局拦截**，不传给你正在用的程序 ——
所以 VS Code 的「切换终端」快捷键会失效（设计如此，见第 4 步）。

### 第 4 步 · 想停的时候

**停两个常驻进程**：

```bat
scripts\stop-all.bat
```

或者在项目根跑 `scripts\py scripts\launcher.py stop`。

**停 Claude Code 那边的等待循环**：在会话里按 `Esc` 或 `Ctrl+C`。

> 这两件事是分开的：`stop-all.bat` **故意不碰**等待循环，因为那是 Claude Code
> 侧跑的东西，归会话管。

---

## 三、命令速查

都在项目根执行。

```bat
:: 统一用 scripts\py —— 它转发到装了依赖的那个解释器（查找顺序见文件头注释）。
:: 别直接用 PATH 上的 python：可能是 Anaconda base，缺 mss/qrcode。

:: 常驻进程管理 —— 平时只用这三条
scripts\py scripts\launcher.py status        :: 看两个常驻在不在（最常用）
scripts\py scripts\launcher.py start         :: 起 / 补常驻（已在跑的不动）
scripts\py scripts\launcher.py stop          :: 停常驻

:: 微信
scripts\py src\wechat_gateway.py doctor            :: 自检
scripts\py src\wechat_gateway.py send --text "测试"
scripts\py src\wechat_gateway.py inbox --since 0   :: 看收到的消息

:: 排查
scripts\py src\wait_events.py --once      :: 只扫一遍，不阻塞
scripts\py src\wait_events.py --status    :: 看账本（不会被单实例锁挡住）
scripts\py src\wait_events.py --takeover  :: 有旧实例占着锁时，杀掉它并接管
scripts\py tests\selftest.py              :: 离线自测（52 项）
curl -s http://127.0.0.1:8799/health      :: 网关状态
type runtime\logs\wechat_gateway.log      :: 网关日志（崩溃现场在这里）
type runtime\logs\screenpeer.log          :: 截图端日志（热键有没有命中看这里）

:: 换机器后固定解释器（只做一次）
scripts\py --pin                          :: 用自动探测到的那个
scripts\py --pin <解释器路径>              :: 或显式指定
```

---

## 四、出问题怎么办

| 现象 | 处理 |
|---|---|
| `Ctrl+\`` 按了没反应 | 截图端没在跑。`scripts\py scripts\launcher.py status` 查一下，再 `start` |
| 按了热键但一直没答案 | 确认 Claude Code 会话**开着**——等待循环是它跑的，会话关了就没反应 |
| 按了热键、没出图 | 看 `runtime\logs\screenpeer.log`：`触发截屏` = 热键命中了；`触发被节流跳过` = 两次按键间隔小于 2s 被有意丢弃（连按太快） |
| 微信收不到答案 | 先看 `runtime\logs\wechat_gateway.log`。`send` 报 `prepare failed` **先别急着改配置**——等 10~30s 重试几次，多数会自愈；一直不行才是首次使用的**第 ③ 步**没做 |
| 常驻进程莫名其妙没了 | 看 `runtime\logs\` 下对应的 `.log`（未捕获异常 / 后台线程异常 / 段错误都会落盘），再 `launcher.py start`。重启不丢消息 |
| 截图端窗口一闪就没了 | 去窗口看报错，多半是解释器不对。用 `scripts\py --pin <解释器路径>` 固定正确的那一个 |
| 微信里图片是灰块 | `aes_key` 参数被改坏了。别自己改协议，恢复 `src/ilink_client.py` |
| token 失效 | `scripts\py src\wechat_gateway.py login --new` 重新扫码（必须你本人扫）|
| 想彻底清干净重来 | 删掉 `runtime\` 整个目录；微信凭据在 `config\wechat_gateway.json` |

---

## 五、几个要记住的点

- **`Ctrl+\`` 被全局拦截**：截图端在跑期间，这个键不传给任何程序。停掉截图端即恢复。
- **答案只发「思路 + 代码截图」，不发源码文件**（按你的要求）。
- **默认答题语言是 Java**；图里写了别的语言就用图里的。
- **算法题会先在本地跑通样例**再发给你，没跑通不会发。
- **同一张图只处理一次**：有账本记着已处理的，重复按热键不会重复污染上下文。

---

## 六、目录结构

```
one-person-agent/
├─ src/        脚本（wait_events.py 是轮询核心）
├─ config/     两个 json 配置
├─ scripts/    launcher.py 常驻管理 / py 解释器转发壳 / start-all.bat stop-all.bat
├─ tests/      selftest.py
├─ runtime/    运行产物，随便删（截图、解题批次、状态账本）
└─ docs/       技术细节与已知问题（ADAPTATION.md）
```

架构、适配记录、历史 bug、未验证的边角 —— 都在 **`docs/ADAPTATION.md`**。
