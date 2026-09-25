# xilian_agent · 昔涟的管家

三件事：

1. **管家** —— 只让名单里的 QQ 用 AstrBot 的 Agent 能力。名单外的人照常聊天，
   只是模型这一次的工具列表是空的。
2. **工具** —— 名单里的人能让昔涟真的动手：跑本机 shell/cmd、调 DSH、调 Cyrene。
3. **面板** —— 每一次动手都留一笔流水：谁、什么时候、想做什么、风险多大、结果如何。

## 两种范围（`GUARD_MODE`）

| 模式 | 名单外的人 | 怎么做的 |
| --- | --- | --- |
| `"agent"`（默认） | 能聊天，不能调工具 | 在 `on_llm_request` 里把 `req.func_tool` 摘掉 |
| `"all"` | 连 AI 都不能用 | 在 `on_waiting_llm_request` 里 `stop_event()` |

改 `main.py` 顶部的 `GUARD_MODE` 换挡，或者在群里发 `/管家 模式 agent` / `/管家 模式 all`。

## agent 模式：为什么摘工具就够

AstrBot 每次真正调模型前，管道会走到 `InternalAgentSubStage.process()`：

1. `collect_initial_request()` 建好 `req`；
2. `build_main_agent()` 往 `req.func_tool` 里装这次能用的**全部工具**——
   `computer_use_runtime == "local"` 时会加上 `LocalExecuteShellTool`、`LocalPythonTool`、
   `FileReadTool/WriteTool/EditTool`、`GrepTool` 等，再加上插件注册的 LLM 工具与 MCP 工具；
3. 然后才 `call_event_hook(event, EventType.OnLLMRequestEvent, req)`。

钩子拿到的是**已经装好工具的那个 `req`**。所以在这一步：

```python
req.func_tool = None      # AstrBot 自己在「到达最大步数」时也是这么摘的
```

模型这一次的选项里就没有任何函数可调，也就调不动本机。请求本身不被打断，
回复照常生成——只是它只能用嘴说，不能动手。

本插件注册的三个工具也在其中，所以**名单外的人同样看不见它们**。

## 名单里的人能用什么

| 工具 id | 干什么 | 参数 |
| --- | --- | --- |
| `xilian_agent_shell` | 在本机跑一条命令并把输出带回来 | `command`、`shell`(cmd/powershell)、`timeout`(秒)、`cwd` |
| `xilian_agent_dsh` | DSH（Deepseek Harness）的状态 / 启动 / 停止 / 交给它 CLI | `action`(status/start/stop/cli)、`args` |
| `xilian_agent_cyrene` | Cyrene 桌面端的状态 / 自带工具 / 本地服务 | `action`(status/tool/api)、`name`、`path` |

三道门，缺一条都不放行：

1. **摘工具**：名单外的人在这次请求里根本看不到这些工具；
2. **再查一遍名单**：工具函数自己会核实发送者（`_agent_allowed`），绕过对话管道也进不来；
3. **危险命令表**：执行前过一遍 `DANGEROUS_PATTERNS`，命中就拒绝，不做补正。

## 系统提示补丁：让模型真的去调工具

工具挂在请求上，不等于模型愿意用。实测过（`_tool/_probe_llm_toolcall.py`）：同一个模型、同一套工具，
在人格提示很长的聊天语境下，它会顺着「陪聊少女」的人设推辞——
「人家暂时没拿到本机磁盘的信息，你可以在 PowerShell 输一下 Get-PSDrive C」。

所以名单里的人开口、而且这次请求真的带着本机工具时，`guard_llm_request` 会在系统提示尾部接一段
`AGENT_PROMPT_HINT`，把三件事说清楚：工具叫什么、问本机的事要真去跑、不许把命令丢给对方。

- 只有名单里的人会拿到；名单外的人工具已经被摘了，自然也不接这段；
- 这次请求里没有本机工具就不接——没工具还提，等于教它胡说；
- 重复跑不叠加（`inject_hint` 会先查一遍）；
- 开关：`AGENT_HINT_ENABLED = True`。

## 本机命令（`xilian_agent_shell`）

- 解释器：`cmd`（默认，走 `%COMSPEC% /c`）或 `powershell`（`-NoProfile -NonInteractive`）；
- 超时：默认 30 秒，最长 300 秒，超时直接掐断；
- 输出：标准输出 + 错误输出，超过 `SHELL_OUTPUT_LIMIT`（8000 字）留头留尾；
- 子进程用 `CREATE_NO_WINDOW` 启动，不会在桌面上闪黑框。

命中的危险命令会被挡下：格式化磁盘、关机/重启、`mkfs`、`diskpart`、删注册表项、
删卷影副本、`cipher /w`、改启动配置、新建系统账户、WMI 删除、杀关键系统进程、
递归强删根目录、`-EncodedCommand`、`iex`/`Invoke-Expression`。

## DSH（`xilian_agent_dsh`）

本机 DSH = **Deepseek Harness**，默认位置写在 `main.py` 顶部：

| 常量 | 默认 |
| --- | --- |
| `DSH_HARNESS_ROOT` | `D:\deepseek-harness` |
| `DSH_CLI_ENTRY` | `<根目录>\apps\cli\lib\bin.js` |
| `DSH_FAIRY_DIR` | `D:\deepseekECR\Fairy-DSH` |
| `DSH_WEB_PORT` | `3081` |

- `action=status`：报告目录/CLI/脚本在不在、端口有没有人听；
- `action=start`：用 `start-fairy.ps1` 以隐藏窗口拉起，最多等 10 秒确认端口；
- `action=stop`：跑 `stop-fairy.ps1`；
- `action=cli`：`node <DSH_CLI_ENTRY> <args>`，在 DSH 根目录下执行。

## Cyrene（`xilian_agent_cyrene`）

| 常量 | 默认 |
| --- | --- |
| `CYRENE_HOME` | `D:\Cyrene` |
| `CYRENE_BIN_DIR` | `<HOME>\resources\bin` |
| `CYRENE_ALLOWED_BINS` | `("cyrene-screenshot.exe",)` |
| `CYRENE_SERVICE_PORTS` | `(57347,)` |
| `CYRENE_API_BASE` | `http://127.0.0.1:8792`（本机桥） |
| `CYRENE_API_TOKEN_FILE` | `D:\Astrbot\cyrene_bridge\token.txt` |
| `CYRENE_API_TIMEOUT` | `30` 秒 |

- `action=status`：程序在不在、自带工具目录里有什么、本地端口通不通、桥在不在听、
  令牌读不读得到；
- `action=tool`：跑 `CYRENE_BIN_DIR` 里**白名单内**的可执行（拒绝带路径的程序名，防目录穿越）；
- `action=api`：请求 `CYRENE_API_BASE + path`；`body` 填了 JSON 文本就走 POST。
  请求自动带 `X-Bridge-Token`，401 / 403 的响应体也会带回来（不再只剩一个状态码）。

> Cyrene 桌面端本体（`D:\Cyrene`，127.0.0.1:57347）没有对外接口，常见路径全是 404。
> 所以这里接的是 **Cyrene 本机桥** —— `D:\Astrbot\cyrene_bridge`，端点与安全边界
> 见它自己的 README。

## 执行面板（`pages/agent-console`）

插件详情页里会多出一张 Page「执行面板」（标题来自 `.astrbot-plugin/i18n/zh-CN.json`）。
页面跑在受限 iframe 里，通过 `window.AstrBotPluginPage` bridge 取数，自己不另加鉴权——
接口走的是 Dashboard 的登录态。

数据来自三个接口（`register_web_api` 注册，路由以插件名打头）：

| 接口 | 方法 | 干什么 |
| --- | --- | --- |
| `/xilian_agent/audit/events` | GET | 流水列表 + 统计 + 管家状态，认 `limit` / `level` / `kind` / `tool` |
| `/xilian_agent/audit/summary` | GET | 只要统计与状态 |
| `/xilian_agent/audit/clear` | POST | 清空流水，必须带 `confirm` |

面板上能看什么：

- 顶部五个数字：动作总数、已拦截、需要留意、只读动作、放行拦截记录（点一下就是筛选）；
- 两枚徽标：管家在岗还是休息、本机工具开着还是关着；
- 每张卡片：风险色条（低风险绿 / 注意黄 / 已拦截红）、工具名、是谁（是老公会标出来）、
  时间、一句话说明，外加状态、退出码、耗时、命中理由这些小标签；
- 展开「看细节」能看到原始动作与结果输出（都做转义，不会把内容当页面执行）；
- 底部清空要连点两次，第二次才真的清。

流水落盘在 `data/plugin_data/xilian_agent/audit.jsonl`，重启后还在；内存里留最近
`AUDIT_MAX_EVENTS`（500）条，文件超过 4 MB 自动滚动。记账是「只写不拦」的：
`AuditLog` 出问题不影响任何一次工具调用。

风险三档：`safe`（只读、无副作用）、`warn`（做了但值得看一眼，比如 `del`）、
`blocked`（已经拒绝：危险命令、名单外的人来调）。

## 白名单

`data/plugin_data/xilian_agent/allowlist.txt`，每行一个 QQ 号，`#` 开头是注释。
文件不存在时自动按 `3614298015` 建一份。用 `/管家 加` / `/管家 减` 改动立刻生效。

## 指令（仅管理员）

```
/管家                   看状态：开关、范围、名单、计数（含本机命令执行次数）
/管家 名单              列出名单
/管家 模式             看当前范围
/管家 模式 agent|all    换挡（立刻生效）
/管家 加 <QQ号>         放一个人进来
/管家 减 <QQ号>         请一个人出去（不允许把最后一个减掉）
/管家 开 / 关           临时让管家上岗或休息
/管家 清空              清空名单
```

非管理员问起来，只会得到一句「这个呀…人家不对外说啦。」

## 可调项（main.py 顶部）

| 常量 | 默认 | 说明 |
| --- | --- | --- |
| `ENABLE` | `True` | 总闸。False 时插件完全不介入 |
| `GUARD_MODE` | `"agent"` | 拦截范围：`agent` / `all` |
| `AGENT_TOOLS_ENABLED` | `True` | 三个工具的开关，关掉后只礼貌拒绝 |
| `SHELL_TIMEOUT_DEFAULT` / `_MAX` | `30` / `300` | 单条命令的默认与最长等待秒数 |
| `SHELL_OUTPUT_LIMIT` | `8000` | 回给模型的最大字符数 |
| `DANGEROUS_PATTERNS` | 见上 | 命中即拒绝的规则表 `(正则, 说明)` |
| `MASTER_ID` | `3614298015` | 名单为空时用来初始化的号 |
| `ALLOWLIST_FILE` | `allowlist.txt` | 名单文件名 |
| `NOTIFY_BLOCKED` / `BLOCK_REPLY` | `True` / 见 main.py | 仅在 `all` 模式下有效 |
| `AUDIT_ENABLED` | `True` | 流水总闸。关掉后照样执行，只是不记账 |
| `AUDIT_MAX_EVENTS` | `500` | 内存里保留多少条，也是面板一次能翻到的上限 |
| `AUDIT_FILE` | `audit.jsonl` | 落盘文件名（放数据目录），空串表示只留内存 |
| `RISKY_HINTS` | 见上 | 中风险信号表，命中的标成「注意」 |

## 管得到的 / 管不到的

管得到：任何**走消息管道**的请求——群聊、私聊、被 @、唤醒前缀。`agent` 模式下
名单外的人拿不到任何工具，也拿不到本插件这三件。

管不到，心里要有数：

1. **WebUI 里的对话**、定时任务（cron）主动发起的请求不走这条管道，插件插不上手；
2. 名单里的人放进来之后，Agent 能干什么仍取决于 AstrBot 自己的设置与本插件
   的三道门——管家管的是「谁能用」，也管住了「本插件这三个工具怎么用」；
3. `agent` 模式**只摘工具**。名单外的人还是能跟昔涟聊天，聊天里说了什么别人照样
   看得见；`all` 模式才是连回应都不给；
4. 命令执行没有沙箱，插件与 AstrBot 同权限。危险规则表挡的是常见自毁动作，
   不是万能的；名单本身就是信任边界。

## 测试

```
python D:\Astrbot\_tool\test_xilian_agent.py
```

用桩模块直接载入 `main.py`，覆盖名单解析与渲染、两种范围的钩子行为、工具摘除与
记账、出错按严处理、模式换挡、八个指令分支、名单落盘与重载，三件工具的权限守卫、
危险命令拦截、真实命令执行、超时/截断/解释器归一、DSH 与 Cyrene 的状态与参数校验，
以及面板：三条路由的注册与返回结构、流水记账与风险分级、落盘与重载、清空的 confirm 门。
