# -*- coding: utf-8 -*-
"""xilian_agent —— 昔涟的管家。

默认只锁「Agent 能力」（GUARD_MODE = "agent"）：名单外的人照常和昔涟说话，
但这一次请求里的工具被全部摘掉——读文件、跑命令、操作本机那一套，模型的
选项里根本不存在。

为什么摘工具就够：AstrBot 每次真正调模型前会走 `on_llm_request` 钩子，此时
`req.func_tool` 已经装齐了这一次能用的全部工具（本机 shell / Python / 文件
读写、插件注册的工具、MCP 工具……）。把 `func_tool` 置空，模型这一次就没有
任何函数可调，自然调不动本机。AstrBot 自己在「到达最大步数」时用的也是同一
个手法（见 astr_agent_run_util.run_agent）。

切到 `GUARD_MODE = "all"` 就回到旧行为：名单外的人一开口就被整体拦下，连聊天
都不给。拦点在 `on_waiting_llm_request`——确定要调 LLM、还没排队等锁的那一刻，
`event.stop_event()` 之后 `call_event_hook` 返回 True，`InternalAgentSubStage`
直接 return，这次请求不会发生。

白名单落在 data/plugin_data/xilian_agent/allowlist.txt，每行一个 QQ 号，
`#` 开头是注释。改动即时生效，不用重启。
管理指令 /管家（仅管理员）。

执行流水（谁在什么时候动了本机、风险多大、结果如何）记在 AuditLog 里，
同时落盘到 data/plugin_data/xilian_agent/audit.jsonl。WebUI 面板放在
pages/agent-console/：在 AstrBot 的插件详情页里点开这张 Page 就能看到。
面板通过 register_web_api 注册的三个只读接口取数：
  GET  /xilian_agent/audit/events   —— 流水列表
  GET  /xilian_agent/audit/summary  —— 管家状态与统计
  POST /xilian_agent/audit/clear    —— 清空流水（要带 confirm）
"""

import asyncio
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:  # 面板接口依赖 astrbot.api.web；老版本没有就只关掉面板，不影响其他功能
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except Exception:  # pragma: no cover - 取决于 AstrBot 版本
    error_response = json_response = request = None  # type: ignore[assignment]
    WEB_API_AVAILABLE = False

# 插件名，同时是 Page 后端路由的前缀。
PLUGIN_NAME = "xilian_agent"

# ── 可调项 ────────────────────────────────────────────────────────────────

# 总闸。False 时插件完全不介入，谁都能照常用 AI 和工具。
ENABLE = True

# 拦截范围。改这一行就换挡：
#   "agent" —— 只锁 Agent 能力。名单外的人能聊天，但这次请求的工具被摘光。
#   "all"   —— 整体拦下。名单外的人连 AI 都用不了。
GUARD_MODE = "agent"

MODE_AGENT = "agent"
MODE_ALL = "all"
MODES = (MODE_AGENT, MODE_ALL)

# 名单里唯一的那个号；文件不存在时会用它初始化。
MASTER_ID = "3614298015"

# 名单文件名，放在插件的数据目录里。
ALLOWLIST_FILE = "allowlist.txt"

# 只在 "all" 模式下会用到：是否给被拦下来的人一句回话。设成 False 就是完全静默。
NOTIFY_BLOCKED = True
BLOCK_REPLY = "……这个呀，人家现在只听得见一个人的声音呢。"


# ── Agent 工具：把本机能力交到名单里的人手上 ──────────────────────────────

# 工具总闸。False 时这三个工具只会礼貌地拒绝，不会真的动本机。
AGENT_TOOLS_ENABLED = True

# 单条命令的默认/最长等待时间（秒），以及回给模型的最大字符数。
SHELL_TIMEOUT_DEFAULT = 30
SHELL_TIMEOUT_MAX = 300
SHELL_OUTPUT_LIMIT = 8000

# 命中即拒绝的危险命令。宁可挡住，也不靠模型自觉。
DANGEROUS_PATTERNS = (
    (r"\bformat(\.com)?\b\s+[a-z]:", "格式化磁盘"),
    (r"\bshutdown\b", "关机或重启"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r"\bdiskpart\b", "磁盘分区操作"),
    (r"\breg\s+delete\b", "删除注册表项"),
    (r"\bvssadmin\b[^\n]*\bdelete\b", "删除卷影副本"),
    (r"\bcipher\b[^\n]*\s/w\b", "擦除磁盘空闲空间"),
    (r"\bbcdedit\b", "修改启动配置"),
    (r"\bnet\s+user\b[^\n]*\/add\b", "新建系统账户"),
    (r"\bwmic\b[^\n]*\bdelete\b", "WMI 删除操作"),
    (r"\btaskkill\b[^\n]*\/f\b[^\n]*\/im\b\s+(winlogon|wininit|services|lsass|csrss|smss)",
     "结束关键系统进程"),
    (r"rm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*f[a-z]*\s+/(\s|$)", "递归强删根目录"),
    (r"\bdel\b[^\n]*\/[sf]\b[^\n]*\\\s*$", "递归强删根目录"),
    (r"\brd\s*\/s\s*\/q\s+[a-z]:\\?\s*$", "递归强删根目录"),
    (r"-[Ee]ncodedCommand\b", "编码命令（会绕过检查）"),
    (r"\b(iex|invoke-expression)\b", "动态执行字符串"),
)

# DSH（Deepseek Harness）在本机的位置：CLI 入口 + Fairy 启停脚本 + Web 端口。
DSH_HARNESS_ROOT = r"D:\deepseek-harness"
DSH_CLI_ENTRY = os.path.join(DSH_HARNESS_ROOT, "apps", "cli", "lib", "bin.js")
DSH_FAIRY_DIR = r"D:\deepseekECR\Fairy-DSH"
DSH_WEB_PORT = 3081

# Cyrene 桌面端：程序目录、自带小工具、本地服务端口。
CYRENE_HOME = r"D:\Cyrene"
CYRENE_EXE = os.path.join(CYRENE_HOME, "Cyrene.exe")
CYRENE_BIN_DIR = os.path.join(CYRENE_HOME, "resources", "bin")
CYRENE_ALLOWED_BINS = ("cyrene-screenshot.exe",)
CYRENE_SERVICE_PORTS = (57347,)
# Cyrene 本机桥（D:\Astrbot\cyrene_bridge）：架在回环地址上的本机能力出口，
# QQ 侧的昔涟和桌面端的 Cyrene 都走它。留空表示 api 动作先关着。
CYRENE_API_BASE = "http://127.0.0.1:8792"
# 桥的令牌文件：桥自己生成，插件只读。
CYRENE_API_TOKEN_FILE = r"D:\Astrbot\cyrene_bridge\token.txt"
# 访问桥时的最长等待（秒）。
CYRENE_API_TIMEOUT = 30

# 系统提示补丁。工具明明挂在请求上，模型却容易顺着聊天人设推辞
# （实测过：「人家暂时没拿到本机磁盘的信息，你可以在 PowerShell 输一下……」），
# 所以在名单里的人开口时，明确告诉它本机能力已经开通、该调就去调。
AGENT_HINT_ENABLED = True

# 只有这几个工具在这次的请求里，才值得补那段话。
AGENT_TOOL_NAMES = ("xilian_agent_shell", "xilian_agent_dsh", "xilian_agent_cyrene")

AGENT_PROMPT_HINT = (
    "\n[本机工具 · 已开通]\n"
    "你现在可以直接操作这台电脑，工具就在手边："
    "xilian_agent_shell（在本机跑 cmd / powershell）、xilian_agent_dsh、xilian_agent_cyrene。\n"
    "对方问的只要是这台电脑本身的事——磁盘剩余空间、目录、文件、进程、环境版本、跑命令跑脚本——"
    "就调用 xilian_agent_shell 真的去查，再用查到的结果回答。\n"
    "不要说「拿不到本机信息」，也不要把命令丢给对方让他自己去跑；"
    "调用前有没有资格由系统判断，你不需要自己推辞。\n"
    "闲聊和别的话题照常回答，不用调工具。\n"
)

# Windows 下起子进程时不弹黑框。
CREATE_NO_WINDOW = 0x08000000


# ── 执行流水与风险分级 ────────────────────────────────────────────────────

# 流水总闸。关掉之后照样执行，只是不再记账。
AUDIT_ENABLED = True

# 内存里保留多少条；同时是面板一次能翻到的上限。
AUDIT_MAX_EVENTS = 500

# 流水落盘的文件名（放在插件的数据目录里）。空串表示只留在内存。
AUDIT_FILE = "audit.jsonl"

# 落盘文件的上限，超过就按内存里的内容重写一遍（滚动，不会无限长）。
AUDIT_FILE_MAX_BYTES = 4 * 1024 * 1024

# 风险三档。blocked 是「已经拒了」，warn 是「做了但值得看一眼」，safe 是「只读、无副作用」。
RISK_SAFE = "safe"
RISK_WARN = "warn"
RISK_BLOCKED = "blocked"
RISK_LEVELS = (RISK_SAFE, RISK_WARN, RISK_BLOCKED)

RISK_LABELS = {
    RISK_SAFE: "低风险",
    RISK_WARN: "注意",
    RISK_BLOCKED: "已拦截",
}

# 单条流水里动作/输出的截断长度，别让一条把面板撑爆。
AUDIT_ACTION_LIMIT = 400
AUDIT_DETAIL_LIMIT = 4000
AUDIT_OUTPUT_LIMIT = 4000

# 中风险信号：命中不会拒绝，只在面板上标成「注意」，让人一眼看见谁在动本机。
RISKY_HINTS = (
    (r"\b(del|erase)\b", "删除文件"),
    (r"\b(rmdir|rd)\b", "删除目录"),
    (r"\b(move|move-item|rename-item)\b", "移动或改名"),
    (r"\b(copy|robocopy|xcopy|copy-item)\b", "复制文件"),
    (r"\bremove-item\b", "删除条目"),
    (r"\b(reg\s+add|new-itemproperty|set-itemproperty)\b", "改动注册表"),
    (r"\b(set-content|out-file|add-content)\b|>", "写文件"),
    (r"\b(stop-process|taskkill|kill)\b", "结束进程"),
    (r"\b(net\s+(stop|start)|sc\s+(config|stop|start))\b", "改动系统服务"),
    (r"\b(pip\s+install|npm\s+install|winget|choco)\b", "安装软件"),
    (r"\bgit\s+(push|reset|clean|checkout)\b", "改动仓库"),
    (r"\b(curl|wget|invoke-webrequest|invoke-restmethod)\b", "联网拉取"),
)


# ── 纯函数：便于单独测试 ──────────────────────────────────────────────────


def normalize_id(value: Any) -> str:
    """QQ 号统一成去掉空白的字符串。"""
    return str(value or "").strip()


def parse_allowlist(text: str) -> list[str]:
    """解析名单文本：每行一个号，`#` 之后当注释，保序去重。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        line = line.strip().strip(",").strip()
        if not line or line in out:
            continue
        out.append(line)
    return out


def is_allowed(sender: Any, allowed: Any) -> bool:
    """这个发送者在不在名单里。空号一律不算。"""
    key = normalize_id(sender)
    if not key:
        return False
    return key in {normalize_id(x) for x in (allowed or [])}


def parse_mode(value: Any) -> str:
    """把外部输入归一成合法模式；不认识就返回空串。"""
    v = str(value or "").strip().lower()
    if v in {"agent", "工具", "锁工具", "能力", "agent_only"}:
        return MODE_AGENT
    if v in {"all", "全部", "整体", "全拦", "禁止"}:
        return MODE_ALL
    return ""


def mode_label(mode: Any) -> str:
    """给人看的模式说明。"""
    m = parse_mode(mode)
    if m == MODE_AGENT:
        return "只锁 Agent 能力（工具：本机操作、文件、命令那一套）"
    if m == MODE_ALL:
        return "整体拦下（名单外的人连 AI 都不能用）"
    return "未识别：%s" % mode


def strip_tools(req: Any) -> int:
    """把这次请求的工具全部摘掉。

    返回摘掉的条数：0 表示本来就没装工具（不用记账），-1 表示装了但数不清。
    写失败会把异常抛出去，交给调用方按严处理。
    """
    if req is None:
        return 0
    toolset = getattr(req, "func_tool", None)
    if toolset is None:
        return 0
    try:
        count = len(list(getattr(toolset, "tools", []) or []))
    except Exception:  # 数不清也算摘了，只是不知道几个
        count = -1
    # AstrBot 自己在「到达最大步数」时也是这么摘的，是最稳的一条路。
    req.func_tool = None
    return count


def tool_names(req: Any) -> set[str]:
    """这次请求里装了哪些工具的名字。读不到就给空集合。"""
    if req is None:
        return set()
    try:
        tools = list(getattr(getattr(req, "func_tool", None), "tools", []) or [])
    except Exception:
        return set()
    out: set[str] = set()
    for tool in tools:
        name = str(getattr(tool, "name", "") or "").strip()
        if name:
            out.add(name)
    return out


def inject_hint(prompt: Any, hint: str = "") -> str:
    """把「本机能力已开通」接到系统提示尾巴上。已经接过就不重复接。"""
    text = str(prompt or "")
    chunk = hint or AGENT_PROMPT_HINT
    body = chunk.strip()
    if not body or body in text:
        return text
    return text + chunk


def render_allowlist(allowed: Any) -> str:
    """把名单排成给人看的样子。"""
    items = list(allowed or [])
    if not items:
        return "（名单是空的——现在谁也叫不动人家）"
    lines = []
    for qq in items:
        tail = "（老公）" if normalize_id(qq) == MASTER_ID else ""
        lines.append("· %s%s" % (normalize_id(qq), tail))
    return "\n".join(lines)


def render_allowlist_file(allowed: Any) -> str:
    """写回文件时的内容：带两行说明，方便手改。"""
    head = [
        "# 昔涟的管家 · 白名单",
        "# 每行一个 QQ 号；# 开头是注释。名单里的人才能用 AstrBot 的 Agent 能力。",
    ]
    return "\n".join(head + [normalize_id(x) for x in (allowed or [])]) + "\n"


def match_dangerous(command: Any) -> str:
    """命令命中危险规则时返回说明，安全时返回空串。"""
    text = str(command or "")
    for pattern, label in DANGEROUS_PATTERNS:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return label
        except re.error:  # 规则写坏了不拖累整体
            continue
    return ""


def classify_risk(text: Any, danger_label: str = "") -> tuple[str, list[str]]:
    """给一条动作定风险等级，返回 (等级, 理由列表)。

    命中危险规则 → blocked（会被拒绝）；
    命中中风险信号 → warn（照做，但面板上要看得见）；
    其余 → safe。
    """
    if danger_label:
        return RISK_BLOCKED, [danger_label]

    raw = str(text or "")
    reasons: list[str] = []
    for pattern, label in RISKY_HINTS:
        try:
            hit = re.search(pattern, raw, re.IGNORECASE)
        except re.error:  # 规则写坏了不拖累整体
            continue
        if hit and label not in reasons:
            reasons.append(label)
    if reasons:
        return RISK_WARN, reasons
    return RISK_SAFE, []


def risk_label(level: Any) -> str:
    """风险等级的中文说法，给指令回复和面板用。"""
    return RISK_LABELS.get(str(level or ""), "未分级")


def event_brief(event: Any) -> str:
    """把一条流水压成一行，供 /管家 指令回显。"""
    if not isinstance(event, dict):
        return ""
    who = normalize_id(event.get("actor")) or "（读不到号）"
    return "%s [%s] %s：%s" % (
        event.get("at", ""),
        risk_label(event.get("risk")),
        who,
        clip_text(event.get("action", ""), 80),
    )


def normalize_shell(value: Any) -> str:
    """把外部给的解释器名归一成 cmd 或 powershell。"""
    kind = str(value or "").strip().lower()
    if kind in {"ps", "pwsh", "power", "powershell", "ps1"}:
        return "powershell"
    return "cmd"


def clamp_timeout(value: Any, default: int = SHELL_TIMEOUT_DEFAULT) -> int:
    """把外部给的超时夹到合法区间。"""
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        seconds = default
    if seconds <= 0:
        seconds = default
    return max(1, min(seconds, SHELL_TIMEOUT_MAX))


def clip_text(text: Any, limit: int = SHELL_OUTPUT_LIMIT) -> str:
    """输出太长就留头留尾，中间写明省略了多少字。"""
    s = str(text or "")
    if len(s) <= limit:
        return s
    head = limit // 2
    tail = limit - head
    return "%s\n……（中间省略 %d 个字符）……\n%s" % (
        s[:head], len(s) - limit, s[-tail:])


def decode_output(raw: Any) -> str:
    """Windows 中文控制台的输出可能是 GBK，逐个编码试。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    data = bytes(raw)
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def port_listening(port: Any, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    """本机某个端口有没有人在听。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, int(port))) == 0
    except (OSError, TypeError, ValueError):
        return False


def cyrene_bridge_port(default: int = 8792) -> int:
    """从 CYRENE_API_BASE 里抠出端口；抠不到就用默认值。"""
    found = re.search(r":(\d+)", str(CYRENE_API_BASE or ""))
    return int(found.group(1)) if found else default


def read_cyrene_token() -> str:
    """读桥的令牌；读不到就给空串，调用方会拿到 401 并如实说明。"""
    path = Path(CYRENE_API_TOKEN_FILE)
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def split_args(text: Any) -> list[str]:
    """把一行参数拆成 argv。够用就好，不追求完整 shell 语义。"""
    import shlex

    raw = str(text or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw, posix=False)
    except ValueError:
        return raw.split()


def render_result(result: dict, title: str = "") -> str:
    """把 run_command 的结果排成给模型看的文本。"""
    lines = []
    if title:
        lines.append(title)
    if result.get("timedOut"):
        lines.append("· 结果：超时中断")
    else:
        code = result.get("exitCode")
        lines.append("· 退出码：%s" % ("未知" if code is None else code))
    if result.get("error"):
        lines.append("· 说明：%s" % result["error"])
    out = clip_text(result.get("stdout") or "")
    err = clip_text(result.get("stderr") or "")
    lines.append("· 标准输出：")
    lines.append(out if out.strip() else "（空）")
    if err.strip():
        lines.append("· 错误输出：")
        lines.append(err)
    return "\n".join(lines)


async def run_command(
    command: str,
    shell: str = "cmd",
    timeout: int = SHELL_TIMEOUT_DEFAULT,
    cwd: str = "",
) -> dict:
    """跑一条命令，返回结构化结果。失败也返回，让调用方自己决定怎么说。"""
    text = str(command or "").strip()
    if not text:
        return {"ok": False, "exitCode": None, "stdout": "", "stderr": "",
                "timedOut": False, "error": "命令是空的"}

    workdir = str(cwd or "").strip()
    if workdir and not os.path.isdir(workdir):
        return {"ok": False, "exitCode": None, "stdout": "", "stderr": "",
                "timedOut": False, "error": "工作目录不存在：%s" % workdir}

    if normalize_shell(shell) == "cmd":
        argv = [os.environ.get("COMSPEC") or "cmd.exe", "/c", text]
    else:
        argv = ["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", text]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir or None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "exitCode": None, "stdout": "", "stderr": "",
                "timedOut": False, "error": "起不来：%r" % exc}

    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except Exception:  # 已经退了就算了
            pass
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            out, err = b"", b""

    return {
        "ok": (not timed_out) and proc.returncode == 0,
        "exitCode": proc.returncode,
        "stdout": decode_output(out),
        "stderr": decode_output(err),
        "timedOut": timed_out,
        "error": ("超过 %d 秒没跑完，已经掐断" % timeout) if timed_out else "",
    }


# ── 执行流水 ──────────────────────────────────────────────────────────────


class AuditLog:
    """agent 的每一步动作：谁、什么时候、想干什么、风险多大、结果如何。

    内存里留最近 limit 条给面板读，同时按行（JSONL）追加到磁盘，重启后还能翻。
    文件超过 AUDIT_FILE_MAX_BYTES 就按内存里的内容重写一遍，天然滚动，不会无限长。
    这个类是纯记账，不参与放行判断——它坏了不影响任何工具能不能跑。
    """

    def __init__(self, path: Any = None, limit: int = AUDIT_MAX_EVENTS):
        self.path = Path(path) if path else None
        self.limit = max(1, int(limit or AUDIT_MAX_EVENTS))
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._seq = 0
        self.load()

    # ---- 记账 ----

    def record(
        self,
        *,
        kind: str,
        stage: str,
        action: str,
        actor: str = "",
        tool: str = "",
        risk: str = RISK_SAFE,
        reasons: Any = None,
        ok: Any = None,
        exit_code: Any = None,
        duration_ms: Any = None,
        detail: str = "",
        output: str = "",
    ) -> dict | None:
        """写一条流水。关掉 AUDIT_ENABLED 时什么都不做，返回 None。"""
        if not AUDIT_ENABLED:
            return None

        stamp = time.time()
        with self._lock:
            self._seq += 1
            event = {
                "id": self._seq,
                "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)),
                "ts": stamp,
                "kind": str(kind or "tool"),
                "stage": str(stage or "done"),
                "tool": str(tool or ""),
                "actor": normalize_id(actor),
                "action": clip_text(action, AUDIT_ACTION_LIMIT),
                "risk": risk if risk in RISK_LEVELS else RISK_SAFE,
                "reasons": [str(x) for x in (reasons or []) if str(x).strip()],
                "ok": ok,
                "exitCode": exit_code,
                "durationMs": duration_ms,
                "detail": clip_text(detail, AUDIT_DETAIL_LIMIT),
                "output": clip_text(output, AUDIT_OUTPUT_LIMIT),
            }
            self._events.append(event)
            if len(self._events) > self.limit:
                del self._events[: len(self._events) - self.limit]

        self._persist(event)
        return event

    # ---- 读 ----

    def tail(
        self,
        limit: int = 100,
        level: str = "",
        kind: str = "",
        tool: str = "",
    ) -> list[dict]:
        """按时间倒序取流水，可按风险等级 / 类型 / 工具过滤。"""
        try:
            want = int(limit)
        except (TypeError, ValueError):
            want = 100
        want = max(1, min(want, self.limit))

        level = str(level or "").strip()
        kind = str(kind or "").strip()
        tool = str(tool or "").strip()

        with self._lock:
            events = list(self._events)
        if level:
            events = [item for item in events if item.get("risk") == level]
        if kind:
            events = [item for item in events if item.get("kind") == kind]
        if tool:
            events = [item for item in events if item.get("tool") == tool]
        return list(reversed(events))[:want]

    def stats(self) -> dict:
        """给面板顶部的几个数字。"""
        with self._lock:
            events = list(self._events)

        counts = {"total": len(events), RISK_SAFE: 0, RISK_WARN: 0, RISK_BLOCKED: 0}
        for item in events:
            level = item.get("risk")
            if level in counts:
                counts[level] += 1
        counts["firstAt"] = events[0].get("at", "") if events else ""
        counts["lastAt"] = events[-1].get("at", "") if events else ""
        counts["limit"] = self.limit
        counts["file"] = str(self.path) if self.path else ""
        return counts

    def clear(self) -> int:
        """清空流水，返回清掉几条。"""
        with self._lock:
            removed = len(self._events)
            self._events = []
            self._seq = 0
        if self.path:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("[xilian_agent] 流水文件删不掉：%r" % exc)
        return removed

    # ---- 落盘 ----

    def load(self) -> None:
        """把上次留下的流水读回来，只保留最近 limit 条。"""
        if not self.path or not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-self.limit :]
        except OSError as exc:
            logger.warning("[xilian_agent] 流水读不出来：%r" % exc)
            return

        events: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue  # 半行/坏行直接跳过
            if isinstance(item, dict):
                events.append(item)

        with self._lock:
            self._events = events
            self._seq = max([int(item.get("id") or 0) for item in events] + [0])
        if events:
            logger.info("[xilian_agent] 读回 %d 条执行流水：%s" % (len(events), self.path))

    def _persist(self, event: dict) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[xilian_agent] 流水写不进去：%r" % exc)
            return

        try:
            if self.path.stat().st_size > AUDIT_FILE_MAX_BYTES:
                self._rewrite_file()
        except OSError:
            pass

    def _rewrite_file(self) -> None:
        """文件太大就按内存里的内容重写一遍。"""
        with self._lock:
            snapshot = list(self._events)
        try:
            with self.path.open("w", encoding="utf-8") as fh:
                for item in snapshot:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[xilian_agent] 流水重写失败：%r" % exc)


# ── 插件本体 ──────────────────────────────────────────────────────────────


class XilianAgent(Star):
    """昔涟的管家：只让名单里的人用 AstrBot 的 Agent 能力。"""

    def __init__(self, context: Context):
        super().__init__(context)
        try:
            data_dir = StarTools.get_data_dir("xilian_agent")
        except Exception as e:  # 拿不到规范目录时退回插件目录，别让插件起不来
            logger.error("[xilian_agent] 取数据目录失败，改用插件目录：%r" % e)
            data_dir = Path(__file__).resolve().parent / "data"

        self.data_dir = Path(data_dir)
        self.path = self.data_dir / ALLOWLIST_FILE

        self.enabled = ENABLE
        self.mode = parse_mode(GUARD_MODE) or MODE_AGENT
        self.allowed: list[str] = []
        self.blocked_count = 0
        self.last_block_at = ""
        self.last_block_who = ""
        self.stripped_count = 0
        self.last_strip_at = ""
        self.last_strip_who = ""
        self.ran_count = 0
        self.last_run_at = ""
        self.last_run_who = ""
        self.last_run_brief = ""

        self.audit = AuditLog(
            (self.data_dir / AUDIT_FILE) if AUDIT_FILE else None,
            limit=AUDIT_MAX_EVENTS,
        )

        self.load()
        self._register_panel_apis()

        if not self.enabled:
            logger.info("[xilian_agent] 已关闭：谁都能用 AI 和工具，管家不介入")
        elif self.mode == MODE_AGENT:
            logger.info(
                "[xilian_agent] 管家上岗（只锁 Agent 能力）：名单 %d 人 → %s；"
                "名单外的人聊天照常，但这次请求的工具会被摘光"
                % (len(self.allowed), "、".join(self.allowed) or "（空）")
            )
        else:
            logger.info(
                "[xilian_agent] 管家上岗（整体拦下）：名单 %d 人 → %s；"
                "名单外的请求会在调用模型前被拦下"
                % (len(self.allowed), "、".join(self.allowed) or "（空）")
            )

    # ---- 面板接口 ----

    def _register_panel_apis(self) -> None:
        """把 WebUI 面板要用的三个接口挂到 Dashboard 上。

        Page 里的 bridge 会以插件名做前缀去请求，所以路由写成
        `/<PLUGIN_NAME>/audit/events` 这样，页面那边写 "audit/events" 就行。
        """
        if not WEB_API_AVAILABLE:
            logger.warning(
                "[xilian_agent] 这个版本的 AstrBot 没提供 astrbot.api.web，"
                "面板接口没注册；管家和其他工具照常"
            )
            return

        routes = (
            (
                "/%s/audit/events" % PLUGIN_NAME,
                self.api_audit_events,
                ["GET"],
                "读取 agent 执行流水",
            ),
            (
                "/%s/audit/summary" % PLUGIN_NAME,
                self.api_audit_summary,
                ["GET"],
                "管家状态与流水统计",
            ),
            (
                "/%s/audit/clear" % PLUGIN_NAME,
                self.api_audit_clear,
                ["POST"],
                "清空执行流水",
            ),
        )
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception as e:  # 注册不上不该拖垮插件
                logger.error("[xilian_agent] 注册面板接口失败 %s：%r" % (route, e))

    # ---- 名单的读写 ----

    def load(self) -> None:
        """读名单。文件不在就用 MASTER_ID 初始化一份写下去。"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("[xilian_agent] 建数据目录失败：%r" % e)

        if not self.path.is_file():
            self.allowed = [MASTER_ID]
            self.save()
            logger.info("[xilian_agent] 名单文件不存在，已按 %s 新建：%s" % (MASTER_ID, self.path))
            return

        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            logger.error("[xilian_agent] 名单读不出来，先用老公一个人的：%r" % e)
            self.allowed = [MASTER_ID]
            return

        parsed = parse_allowlist(text)
        if not parsed:
            logger.warning("[xilian_agent] 名单是空的——现在谁也叫不动人家")
        self.allowed = parsed

    def save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(render_allowlist_file(self.allowed), encoding="utf-8")
        except OSError as e:
            logger.error("[xilian_agent] 名单写不进去：%r" % e)

    # ---- 拦截 ----

    def _sender(self, event: AstrMessageEvent) -> str:
        return normalize_id(event.get_sender_id())

    def _note_strip(self, who: str, count: int = -1) -> None:
        self.stripped_count += 1
        self.last_strip_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_strip_who = who
        shown = "%d 个" % count if isinstance(count, int) and count > 0 else "若干"
        self.audit.record(
            kind="guard",
            stage="stripped",
            tool="%s_guard" % PLUGIN_NAME,
            actor=who,
            action="名单外的人想用工具，这次请求的 %s工具被摘掉" % shown,
            risk=RISK_WARN,
            reasons=["不在白名单里"],
            ok=True,
        )

    def _note_block(self, who: str) -> None:
        self.blocked_count += 1
        self.last_block_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_block_who = who
        self.audit.record(
            kind="guard",
            stage="blocked",
            tool="%s_guard" % PLUGIN_NAME,
            actor=who,
            action="名单外的人开口，这次请求被整体拦下",
            risk=RISK_WARN,
            reasons=["不在白名单里"],
            ok=True,
        )

    async def _guard_all(self, event: AstrMessageEvent, notify: bool) -> bool:
        """all 模式：名单外就整体拦下。返回是否拦了。"""
        if not self.enabled:
            return False

        sender = self._sender(event)
        if is_allowed(sender, self.allowed):
            logger.debug("[xilian_agent] 放行：%s" % sender)
            return False

        who = sender or "（读不到号）"
        self._note_block(who)
        logger.info("[xilian_agent] 挡下一次 AI 请求：%s 不在名单里" % who)

        if notify and NOTIFY_BLOCKED and BLOCK_REPLY:
            try:
                await event.send(event.plain_result(BLOCK_REPLY))
            except Exception as e:  # 提示发不出去不影响拦截本身
                logger.warning("[xilian_agent] 拦截提示没发出去：%r" % e)

        event.stop_event()
        return True

    def _guard_tools(self, event: AstrMessageEvent, req: Any) -> bool:
        """agent 模式：聊天照常，只把工具摘掉。返回是否摘了。"""
        if not self.enabled:
            return False

        sender = self._sender(event)
        if is_allowed(sender, self.allowed):
            logger.debug("[xilian_agent] 放行（带工具）：%s" % sender)
            return False

        count = strip_tools(req)
        if count == 0:
            # 这次本来就没有工具可用，没什么可摘的，也不记账。
            return False

        who = sender or "（读不到号）"
        self._note_strip(who, count)
        shown = "%d 个" % count if count > 0 else "若干"
        logger.info("[xilian_agent] 摘掉工具：%s 不在名单里（%s工具）" % (who, shown))
        return True

    def _inject_agent_hint(self, event: AstrMessageEvent, req: Any) -> bool:
        """名单里的人开口时，告诉模型本机工具已经开通。返回是否注入了。

        只在这次请求真的带着本机工具时才补——没装工具还提，等于教它胡说。
        """
        if not (self.enabled and AGENT_TOOLS_ENABLED and AGENT_HINT_ENABLED):
            return False
        if req is None or not is_allowed(self._sender(event), self.allowed):
            return False
        if not (tool_names(req) & set(AGENT_TOOL_NAMES)):
            return False
        if not hasattr(req, "system_prompt"):
            return False
        before = str(getattr(req, "system_prompt", "") or "")
        after = inject_hint(before)
        if after == before:
            return False
        req.system_prompt = after
        logger.debug("[xilian_agent] 已给 %s 补上本机能力提示" % self._sender(event))
        return True

    @filter.on_waiting_llm_request()
    async def guard_waiting(self, event: AstrMessageEvent) -> None:
        """整体拦下的入口。只在 all 模式下动手。"""
        if self.mode != MODE_ALL:
            return
        try:
            await self._guard_all(event, notify=True)
        except Exception as e:
            # 安全类插件，出错就往严了走：宁可这次谁都用不了，也不漏过去。
            logger.error("[xilian_agent] 拦截时出错，已按拒绝处理：%r" % e)
            try:
                event.stop_event()
            except Exception:
                pass

    @filter.on_llm_request()
    async def guard_llm_request(self, event: AstrMessageEvent, req: Any = None) -> None:
        """真正动手的地方。

        all 模式：补一道拦截，兜住绕过前一个钩子的路径（静默，不重复提示）。
        agent 模式：把没资格的人的工具摘光，请求本身放行。
        """
        if self.mode == MODE_ALL:
            try:
                blocked = await self._guard_all(event, notify=False)
            except Exception as e:
                logger.error("[xilian_agent] 补拦时出错，已按拒绝处理：%r" % e)
                try:
                    event.stop_event()
                except Exception:
                    pass
                return
            if not blocked:
                # 名单里的人：工具照给，再补一句「本机能力已开通」。
                try:
                    self._inject_agent_hint(event, req)
                except Exception as e:
                    logger.warning("[xilian_agent] 补提示出错，这次就照原样跑：%r" % e)
            return

        try:
            if self._guard_tools(event, req):
                return
        except Exception as e:
            # 摘不掉就可能带着工具跑，那就干脆别跑。
            logger.error("[xilian_agent] 摘工具出错，已按拒绝处理：%r" % e)
            try:
                event.stop_event()
            except Exception:
                pass
            return

        # 走到这里说明工具是留给名单里的人的，那就把话说明白。
        try:
            self._inject_agent_hint(event, req)
        except Exception as e:
            logger.warning("[xilian_agent] 补提示出错，这次就照原样跑：%r" % e)

    # ---- Agent 工具：本机执行 ----

    def _agent_allowed(self, event: AstrMessageEvent) -> bool:
        """工具层面的第二道门：名单外的人就算摸到了工具也过不去。"""
        if not (self.enabled and AGENT_TOOLS_ENABLED):
            return False
        return is_allowed(self._sender(event), self.allowed)

    def _note_run(self, who: str, brief: str) -> None:
        self.ran_count += 1
        self.last_run_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_run_who = who
        self.last_run_brief = clip_text(brief, 120)

    def _blocked_tool_reply(self) -> str:
        return "这个呀…本机操作只交给名单里的人呢。"

    async def _exec(
        self,
        event: AstrMessageEvent,
        command: str,
        shell: str = "cmd",
        timeout: Any = 0,
        cwd: str = "",
        title: str = "",
        tool: str = "xilian_agent_shell",
    ) -> str:
        """三个工具共用的一条路：查权限 → 挡危险命令 → 跑 → 记账 → 排版。"""
        who = self._sender(event) or "（读不到号）"
        if not self._agent_allowed(event):
            self.audit.record(
                kind="tool",
                stage="denied",
                tool=tool,
                actor=who,
                action="名单外的人想动本机，已拒绝",
                risk=RISK_BLOCKED,
                reasons=["不在白名单里"],
                ok=False,
                detail=str(command or ""),
            )
            return self._blocked_tool_reply()

        danger = match_dangerous(command)
        level, reasons = classify_risk(command, danger)
        if danger:
            logger.warning("[xilian_agent] 拒了一条危险命令（%s）：%s"
                           % (danger, clip_text(command, 200)))
            self.audit.record(
                kind="tool",
                stage="blocked",
                tool=tool,
                actor=who,
                action="危险命令被拒：%s" % clip_text(command, 200),
                risk=RISK_BLOCKED,
                reasons=reasons,
                ok=False,
                detail=str(command or ""),
            )
            return "这条人家不跑：涉及%s。" % danger

        seconds = clamp_timeout(timeout)
        self._note_run(who, command)

        started = time.time()
        result = await run_command(command, shell=shell, timeout=seconds, cwd=cwd)
        duration_ms = int((time.time() - started) * 1000)
        brief = str(title or "").strip().strip("·").strip() or str(command or "").strip()
        self.audit.record(
            kind="tool",
            stage="done",
            tool=tool,
            actor=who,
            action=brief,
            risk=level,
            reasons=reasons,
            ok=bool(result.get("ok")),
            exit_code=result.get("exitCode"),
            duration_ms=duration_ms,
            detail=str(command or ""),
            output=self._output_digest(result),
        )

        kind = normalize_shell(shell)
        head = title or "· 命令：%s" % clip_text(str(command or "").strip(), 300)
        head = "%s\n· 解释器：%s\n· 最长等待：%d 秒" % (head, kind, seconds)
        return render_result(result, head)

    @staticmethod
    def _output_digest(result: Any) -> str:
        """把一次执行的结果压成一段给面板看的文字。"""
        if not isinstance(result, dict):
            return ""
        parts: list[str] = []
        if result.get("error"):
            parts.append("[说明] %s" % result["error"])
        out = str(result.get("stdout") or "").strip()
        if out:
            parts.append(out)
        err = str(result.get("stderr") or "").strip()
        if err:
            parts.append("[stderr] %s" % err)
        return "\n".join(parts)

    @filter.llm_tool(name="xilian_agent_shell")
    async def tool_shell(
        self,
        event: AstrMessageEvent,
        command: str,
        shell: str = "cmd",
        timeout: int = 0,
        cwd: str = "",
    ) -> str:
        """在本机执行一条命令并把输出带回来。要真实跑命令、看目录、查环境、跑脚本时用它。

        只有白名单里的人开口才会真正执行；格式化、关机、删注册表一类动作会被直接拒绝。

        Args:
            command(string): 要执行的完整命令
            shell(string): 用哪个解释器，cmd 或 powershell，默认 cmd
            timeout(number): 最长等待秒数，默认 30，最多 300
            cwd(string): 工作目录，留空用默认目录
        """
        return await self._exec(event, command, shell=shell, timeout=timeout, cwd=cwd)

    @filter.llm_tool(name="xilian_agent_dsh")
    async def tool_dsh(
        self,
        event: AstrMessageEvent,
        action: str = "status",
        args: str = "",
    ) -> str:
        """调用本机的 DSH（Deepseek Harness）：看状态、启动、停止，或把参数交给它的 CLI。

        只有白名单里的人能用。

        Args:
            action(string): status=看状态；start=启动；stop=停止；cli=把 args 交给 DSH CLI
            args(string): action=cli 时传给 DSH CLI 的参数
        """
        if not self._agent_allowed(event):
            return self._blocked_tool_reply()

        act = str(action or "status").strip().lower()
        if act in {"", "status", "状态", "check"}:
            detail = self._dsh_status()
            self.audit.record(
                kind="tool",
                stage="done",
                tool="xilian_agent_dsh",
                actor=self._sender(event),
                action="查看 DSH 状态",
                risk=RISK_SAFE,
                ok=True,
                output=detail,
            )
            return detail
        if act in {"start", "启动", "up"}:
            return await self._dsh_start(event)
        if act in {"stop", "停止", "停", "down"}:
            return await self._dsh_stop(event)
        if act in {"cli", "命令", "run"}:
            return await self._dsh_cli(event, args)
        return "认得的动作只有 status / start / stop / cli 四个呀。"

    def _dsh_status(self) -> str:
        alive = port_listening(DSH_WEB_PORT)
        return "\n".join([
            "· DSH 根目录：%s%s" % (
                DSH_HARNESS_ROOT, "" if os.path.isdir(DSH_HARNESS_ROOT) else "（找不到）"),
            "· CLI 入口：%s%s" % (
                DSH_CLI_ENTRY, "" if os.path.isfile(DSH_CLI_ENTRY) else "（找不到）"),
            "· Fairy 目录：%s%s" % (
                DSH_FAIRY_DIR, "" if os.path.isdir(DSH_FAIRY_DIR) else "（找不到）"),
            "· 服务端口 %d：%s" % (DSH_WEB_PORT, "在听" if alive else "没动静"),
        ])

    async def _dsh_start(self, event: AstrMessageEvent) -> str:
        if port_listening(DSH_WEB_PORT):
            return "DSH 已经在 %d 端口上跑着了。" % DSH_WEB_PORT
        script = os.path.join(DSH_FAIRY_DIR, "start-fairy.ps1")
        if not os.path.isfile(script):
            return "找不到启动脚本：%s" % script
        command = (
            'Start-Process -WindowStyle Hidden -FilePath "powershell" '
            '-ArgumentList @("-ExecutionPolicy","Bypass","-File","%s")' % script
        )
        who = self._sender(event) or "（读不到号）"
        self._note_run(who, "dsh start")
        started = time.time()
        await run_command(command, shell="powershell", timeout=30)
        alive = False
        for _ in range(20):
            if port_listening(DSH_WEB_PORT):
                alive = True
                break
            await asyncio.sleep(0.5)
        self.audit.record(
            kind="tool",
            stage="done",
            tool="xilian_agent_dsh",
            actor=who,
            action="启动 DSH：%s" % script,
            risk=RISK_WARN,
            reasons=["拉起本机服务"],
            ok=alive,
            duration_ms=int((time.time() - started) * 1000),
            detail=command,
            output=("端口 %d 已经在听" % DSH_WEB_PORT) if alive
            else "10 秒内还没听到端口 %d" % DSH_WEB_PORT,
        )
        if alive:
            return "DSH 起来了，%d 端口已经在听。" % DSH_WEB_PORT
        return ("启动指令已经发出去了，但 10 秒内还没听到 %d 端口。"
                "稍等一下再叫人家看一眼 status 吧。" % DSH_WEB_PORT)

    async def _dsh_stop(self, event: AstrMessageEvent) -> str:
        script = os.path.join(DSH_FAIRY_DIR, "stop-fairy.ps1")
        if not os.path.isfile(script):
            return "找不到停止脚本：%s" % script
        who = self._sender(event) or "（读不到号）"
        self._note_run(who, "dsh stop")
        started = time.time()
        command = '& "%s"' % script
        result = await run_command(command, shell="powershell", timeout=60)
        self.audit.record(
            kind="tool",
            stage="done",
            tool="xilian_agent_dsh",
            actor=who,
            action="停止 DSH：%s" % script,
            risk=RISK_WARN,
            reasons=["停掉本机服务"],
            ok=bool(result.get("ok")),
            exit_code=result.get("exitCode"),
            duration_ms=int((time.time() - started) * 1000),
            detail=command,
            output=self._output_digest(result),
        )
        return render_result(result, "· 停止 DSH：%s" % script)

    async def _dsh_cli(self, event: AstrMessageEvent, args: str) -> str:
        argv = split_args(args)
        if not argv:
            return "要我把什么参数交给 DSH CLI 呀？例如 args=--help。"
        if not os.path.isfile(DSH_CLI_ENTRY):
            return "找不到 DSH CLI 入口：%s" % DSH_CLI_ENTRY
        command = " ".join(["node", '"%s"' % DSH_CLI_ENTRY] + ['"%s"' % a for a in argv])
        return await self._exec(
            event, command, shell="cmd", timeout=120, cwd=DSH_HARNESS_ROOT,
            title="· DSH CLI：%s" % clip_text(" ".join(argv), 300),
            tool="xilian_agent_dsh")

    @filter.llm_tool(name="xilian_agent_cyrene")
    async def tool_cyrene(
        self,
        event: AstrMessageEvent,
        action: str = "status",
        name: str = "",
        path: str = "",
        body: str = "",
    ) -> str:
        """调用本机的 Cyrene 桥：看状态、跑 Cyrene 自带小工具、访问桥的本机能力端点。

        桥是这台电脑的本机能力出口（默认 http://127.0.0.1:8792）。只有白名单里的人能用。

        Args:
            action(string): status=看状态；tool=跑自带工具；api=访问桥的端点
            name(string): action=tool 时的程序名，目前只认 cyrene-screenshot.exe
            path(string): action=api 时的端点，比如 /sys/drives、/sys/info、/exec、/fs/list
            body(string): action=api 时可选，JSON 文本；填了就走 POST，
                例如 {"command": "ipconfig"} 或 {"path": "D:/Astrbot"}
        """
        if not self._agent_allowed(event):
            return self._blocked_tool_reply()

        act = str(action or "status").strip().lower()
        if act in {"", "status", "状态", "check"}:
            detail = self._cyrene_status()
            self.audit.record(
                kind="tool",
                stage="done",
                tool="xilian_agent_cyrene",
                actor=self._sender(event),
                action="查看 Cyrene 状态",
                risk=RISK_SAFE,
                ok=True,
                output=detail,
            )
            return detail
        if act in {"tool", "工具"}:
            return await self._cyrene_tool(event, name)
        if act in {"api", "接口"}:
            return await self._cyrene_api(event, path, body)
        return "认得的动作只有 status / tool / api 三个呀。"

    def _cyrene_status(self) -> str:
        ports = "、".join(
            "%d=%s" % (p, "在听" if port_listening(p) else "没动静")
            for p in CYRENE_SERVICE_PORTS
        )
        bins: list[str] = []
        if os.path.isdir(CYRENE_BIN_DIR):
            try:
                bins = sorted(os.listdir(CYRENE_BIN_DIR))[:20]
            except OSError:
                bins = []
        return "\n".join([
            "· Cyrene 程序：%s%s" % (
                CYRENE_EXE, "" if os.path.isfile(CYRENE_EXE) else "（找不到）"),
            "· 自带工具目录：%s%s" % (
                CYRENE_BIN_DIR, "：" + "、".join(bins) if bins else "（空或读不到）"),
            "· 本地端口：%s" % ports,
            "· 桥基址：%s%s" % (
                CYRENE_API_BASE or "还没配（api 动作先关着）",
                "" if not CYRENE_API_BASE else
                "，" + ("在听" if port_listening(cyrene_bridge_port()) else "没动静")),
            "· 桥令牌：%s" % (
                "读到了：%s" % CYRENE_API_TOKEN_FILE if read_cyrene_token()
                else "读不到：%s" % CYRENE_API_TOKEN_FILE),
        ])

    async def _cyrene_tool(self, event: AstrMessageEvent, name: str) -> str:
        binary = str(name or "").strip()
        if not binary:
            return "要跑哪个工具呀？目前只认：%s。" % "、".join(CYRENE_ALLOWED_BINS)
        if os.path.basename(binary) != binary:
            return "只写程序名就好，不要带路径。"
        if binary.lower() not in {b.lower() for b in CYRENE_ALLOWED_BINS}:
            return "这个不在允许名单里，人家只认：%s。" % "、".join(CYRENE_ALLOWED_BINS)
        exe = os.path.join(CYRENE_BIN_DIR, binary)
        if not os.path.isfile(exe):
            return "找不到这个程序：%s" % exe
        return await self._exec(
            event, '"%s"' % exe, shell="cmd", timeout=60,
            title="· Cyrene 工具：%s" % binary,
            tool="xilian_agent_cyrene")

    async def _cyrene_api(self, event: AstrMessageEvent, path: str,
                          body: str = "") -> str:
        """访问 Cyrene 桥。path 是端点，body 给了 JSON 就走 POST。"""
        if not CYRENE_API_BASE:
            return ("Cyrene 的桥还没配呢——先在 main.py 里把 CYRENE_API_BASE "
                    "填成 http://127.0.0.1:<端口>，api 动作才会开。")

        target = str(path or "/").strip() or "/"
        if not target.startswith("/"):
            target = "/" + target
        url = CYRENE_API_BASE.rstrip("/") + target
        who = self._sender(event) or "（读不到号）"

        payload = None
        text = str(body or "").strip()
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                return ("body 得是 JSON 呀，比如 {\"command\": \"ipconfig\"}、"
                        "{\"path\": \"D:/Astrbot\"}。")
            if not isinstance(payload, dict):
                return "body 要是一个 JSON 对象呢。"
        verb = "POST" if payload is not None else "GET"
        token = read_cyrene_token()
        self._note_run(who, "cyrene bridge %s %s" % (verb, target))

        def _fetch() -> tuple[bool, str]:
            import urllib.error
            import urllib.request

            data = None
            headers = {"Accept": "application/json"}
            if payload is not None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json; charset=utf-8"
            if token:
                headers["X-Bridge-Token"] = token
            req = urllib.request.Request(url, data=data, headers=headers, method=verb)
            try:
                with urllib.request.urlopen(req, timeout=CYRENE_API_TIMEOUT) as resp:
                    raw = resp.read(400000)
                    return True, "· %s %s → HTTP %d\n%s" % (
                        verb, url, resp.status, clip_text(decode_output(raw), 6000))
            except urllib.error.HTTPError as exc:
                raw = exc.read(400000)
                note = clip_text(decode_output(raw), 3000) if raw else ""
                hint = ""
                if exc.code == 401:
                    hint = "\n（令牌不对或没带：看看 %s）" % CYRENE_API_TOKEN_FILE
                return False, "· %s %s → HTTP %d%s\n%s" % (verb, url, exc.code, hint, note)
            except Exception as exc:
                return False, ("· %s %s → 没连上：%r\n（桥在不在？跑一下 "
                               "D:\\Astrbot\\cyrene_bridge\\start-bridge.ps1）"
                               % (verb, url, exc))

        started = time.time()
        ok, output = await asyncio.get_running_loop().run_in_executor(None, _fetch)
        self.audit.record(
            kind="tool",
            stage="done",
            tool="xilian_agent_cyrene",
            actor=who,
            action="访问 Cyrene 桥：%s %s" % (verb, target),
            risk=RISK_SAFE if verb == "GET" else RISK_WARN,
            reasons=["只读端点"] if verb == "GET" else ["会改动本机"],
            ok=ok,
            duration_ms=int((time.time() - started) * 1000),
            detail=url,
            output=output,
        )
        return output

    # ---- 面板接口的实现 ----

    def _panel_state(self) -> dict:
        """面板顶部的管家状态。"""
        return {
            "enabled": bool(self.enabled),
            "mode": self.mode,
            "modeLabel": mode_label(self.mode),
            "toolsEnabled": bool(AGENT_TOOLS_ENABLED),
            "allowlist": list(self.allowed),
            "master": MASTER_ID,
            "blockedCount": self.blocked_count,
            "lastBlockAt": self.last_block_at,
            "lastBlockWho": self.last_block_who,
            "strippedCount": self.stripped_count,
            "lastStripAt": self.last_strip_at,
            "lastStripWho": self.last_strip_who,
            "ranCount": self.ran_count,
            "lastRunAt": self.last_run_at,
            "lastRunWho": self.last_run_who,
            "lastRunBrief": self.last_run_brief,
            "riskLabels": dict(RISK_LABELS),
            "allowlistFile": str(self.path),
            "auditFile": str(self.audit.path) if self.audit.path else "",
            "limits": {
                "shellTimeoutDefault": SHELL_TIMEOUT_DEFAULT,
                "shellTimeoutMax": SHELL_TIMEOUT_MAX,
                "auditMaxEvents": AUDIT_MAX_EVENTS,
            },
            "dangerRules": [label for _, label in DANGEROUS_PATTERNS],
        }

    async def api_audit_events(self):
        """流水列表。参数：limit / level / kind / tool。"""
        if not WEB_API_AVAILABLE:
            return None

        try:
            limit = request.query.get("limit", 100, type=int)
        except Exception:  # 参数写坏了就用默认值
            limit = 100
        level = str(request.query.get("level", "") or "").strip()
        kind = str(request.query.get("kind", "") or "").strip()
        tool = str(request.query.get("tool", "") or "").strip()
        if level and level not in RISK_LEVELS:
            level = ""

        events = self.audit.tail(
            limit=limit or 100, level=level, kind=kind, tool=tool
        )
        return json_response(
            {
                "status": "ok",
                "data": {
                    "events": events,
                    "stats": self.audit.stats(),
                    "state": self._panel_state(),
                },
            }
        )

    async def api_audit_summary(self):
        """只要状态和统计，给面板定期刷新用。"""
        if not WEB_API_AVAILABLE:
            return None
        return json_response(
            {
                "status": "ok",
                "data": {"stats": self.audit.stats(), "state": self._panel_state()},
            }
        )

    async def api_audit_clear(self):
        """清空流水。要显式带 confirm，免得手滑。"""
        if not WEB_API_AVAILABLE:
            return None

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("confirm") not in (True, "yes", "confirm", "清空"):
            return error_response("要清空流水请带上 confirm")

        cleared = self.audit.clear()
        self.audit.record(
            kind="notice",
            stage="cleared",
            tool="xilian_agent_panel",
            actor=normalize_id(getattr(request, "username", "")),
            action="在面板上清空了执行流水（%d 条）" % cleared,
            risk=RISK_SAFE,
            ok=True,
        )
        return json_response({"status": "ok", "data": {"cleared": cleared}})

    # ---- 管理指令 ----

    def _status(self) -> str:
        lines = [
            "昔涟的管家",
            "· 状态：%s" % ("开着" if self.enabled else "关着（谁都能用 AI 和工具）"),
            "· 范围：%s" % mode_label(self.mode),
            "· 名单：%d 人" % len(self.allowed),
            render_allowlist(self.allowed),
        ]
        if self.mode == MODE_ALL:
            lines.append("· 已整体拦下：%d 次" % self.blocked_count)
            if self.last_block_at:
                lines.append("  最近一次 %s，来自 %s" % (self.last_block_at, self.last_block_who))
        else:
            lines.append("· 已摘掉工具：%d 次" % self.stripped_count)
            if self.last_strip_at:
                lines.append("  最近一次 %s，来自 %s" % (self.last_strip_at, self.last_strip_who))
        lines.append("· 本机命令：已执行 %d 次" % self.ran_count)
        if self.last_run_at:
            lines.append("  最近一次 %s，来自 %s：%s"
                         % (self.last_run_at, self.last_run_who, self.last_run_brief))
        lines.append("· 名单文件：%s" % self.path)
        return "\n".join(lines)

    def _usage(self) -> str:
        return (
            "/管家 —— 看状态\n"
            "/管家 名单 —— 列出名单\n"
            "/管家 加 <QQ号> —— 放一个人进来\n"
            "/管家 减 <QQ号> —— 把一个人请出去\n"
            "/管家 模式 agent —— 只锁 Agent 能力（推荐）\n"
            "/管家 模式 all —— 连聊天一起拦下\n"
            "/管家 开 / 关 —— 暂时让管家休息或上岗\n"
            "改 %s 也行，每行一个号，改完即时生效。\n"
            "名单里的人还能让昔涟跑本机命令、调 DSH 与 Cyrene（三个 llm_tool）。"
            % ALLOWLIST_FILE
        )

    @filter.command("管家")
    async def butler_cmd(self, event: AstrMessageEvent):
        """查看或修改白名单和范围。仅管理员可用。"""
        if not event.is_admin():
            yield event.plain_result("这个呀…人家不对外说啦。")
            return

        args = [a for a in str(event.get_message_str() or "").split() if a]
        # 第一个词是指令名本身，去掉。
        if args and args[0].lstrip("/") == "管家":
            args = args[1:]

        if not args:
            yield event.plain_result(self._status())
            return

        head = args[0]

        if head in {"名单", "列表", "list"}:
            yield event.plain_result(render_allowlist(self.allowed))
            return

        if head in {"用法", "帮助", "help", "-h"}:
            yield event.plain_result(self._usage())
            return

        if head in {"模式", "范围", "mode"}:
            if len(args) < 2:
                yield event.plain_result(
                    "现在是：%s\n想换的话写 /管家 模式 agent 或 /管家 模式 all。"
                    % mode_label(self.mode)
                )
                return
            want = parse_mode(args[1])
            if not want:
                yield event.plain_result("人家只认得 agent 和 all 两种呀。")
                return
            self.mode = want
            yield event.plain_result("换好了。现在：%s" % mode_label(self.mode))
            return

        if head in {"开", "关", "开关"}:
            if head == "开关":
                self.enabled = not self.enabled
            else:
                self.enabled = head == "开"
            yield event.plain_result(
                "管家%s了。" % ("上岗" if self.enabled else "先歇一会（现在谁都能用 AI 和工具）")
            )
            return

        if head in {"加", "添", "add"}:
            if len(args) < 2:
                yield event.plain_result("要加谁呀？写成 /管家 加 12345678。")
                return
            qq = normalize_id(args[1])
            if not qq:
                yield event.plain_result("这个号人家读不懂呢。")
                return
            if qq in self.allowed:
                yield event.plain_result("QQ %s 本来就在名单里。" % qq)
                return
            self.allowed.append(qq)
            self.save()
            yield event.plain_result("QQ %s 进来了，现在也能叫得动人家。" % qq)
            return

        if head in {"减", "删", "remove", "del"}:
            if len(args) < 2:
                yield event.plain_result("要请走谁呀？写成 /管家 减 12345678。")
                return
            qq = normalize_id(args[1])
            if qq not in self.allowed:
                yield event.plain_result("QQ %s 本来就不在名单里。" % qq)
                return
            if len(self.allowed) <= 1:
                yield event.plain_result("最后一个了，再减就谁也叫不动人家啦。")
                return
            self.allowed.remove(qq)
            self.save()
            yield event.plain_result("QQ %s 请出去了。" % qq)
            return

        if head in {"清空", "clear"}:
            self.allowed = []
            self.save()
            yield event.plain_result("名单清空了——现在谁也叫不动人家。")
            return

        yield event.plain_result(self._usage())

    async def terminate(self):
        """插件停止时把名单存好。"""
        self.save()
