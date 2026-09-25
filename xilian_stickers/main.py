# -*- coding: utf-8 -*-
"""xilian_stickers —— 昔涟的表情包，外加句尾的语气符号。

表情图取自 Cyrene 自带表情包（asar 内 dist/renderer/stickers），共 52 张，
语义描述沿用 Cyrene 自带的 sticker-descriptions。

提供四种用法：
1. LLM 工具 ``xilian_send_sticker`` —— 昔涟在聊天中自主选择并发送表情；
2. 指令 ``/表情 列表`` / ``/表情 <key>`` —— 手动查看或发送；
3. 发送前钩子 ``on_decorating_result`` —— 两个开关彼此独立：
   ``AUTO_SUFFIX_ON`` 在文字末尾落一个「♪」（结尾若挂着别的小 emoji 会先摘掉再换）；
   ``AUTO_STICKER_ON`` 在文字之后固定补一张表情图。
   两个都开，就是「文字末尾 ♪ + 回复下面一张表情」。
4. 补哪一张，由「读语境」决定（``LLM_PICK_ON`` / ``LLM_PICK_MODE``）：
   ``auto``    关键词能认出心情就用关键词，认不出再请一次 LLM 读语境挑（默认）；
   ``llm``     每条回复都请 LLM 读语境挑，最贴语境，代价是每条多等一两秒；
   ``keyword`` 从不请 LLM，退回纯关键词的老逻辑。
   实在挑不出时从 ``FALLBACK_POOL`` 轮着来，并避开最近用过的几张（``RECENT_AVOID_N``），
   所以不会再像以前那样句句都落在同一张上。
5. 剪掉模型漏出来的原始标记（``SANITIZE_ON``）：
   有些模型 / 中转不会把工具调用放进 tool_calls 字段，而是把
   ``<|tool_call_begin|>functions.xilian_send_sticker:0<|tool_call_argument_begin|>{"sticker": "blushhard"}<|tool_call_end|>``
   这种原始模板标记直接写进正文，末尾还可能挂着 ``.affirmations.love-happy`` 这类短码，
   一起发给用户（连 TTS 都会照着念）。发送前会把这些剪掉；
   若剪出来的正是 ``xilian_send_sticker``，就顺手把她原本想发的那张表情真的发出去
   （``HONOR_LEAKED_STICKER``）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
STICKER_DIR = os.path.join(PLUGIN_DIR, "stickers")

# key -> (文件名, 想表达的意思)
STICKERS: dict[str, tuple[str, str]] = {
    "playful": ("playful.png", "你看人家嘛"),
    "love-happy": ("love-happy.png", "好开心，喜欢你"),
    "confident": ("confident.png", "交给人家，放心"),
    "serious": ("serious.png", "说正经的，听好了"),
    "calm": ("calm.png", "静静陪着你就好"),
    "peek": ("peek.webp", "偷偷看一眼"),
    "clingy-confused": ("clingy-confused.webp", "等等人家嘛"),
    "love-calm": ("love-calm.webp", "这颗心给你的"),
    "HI": ("HI.jpg", "嗨，想我了吗"),
    "hello": ("hello.jpg", "嗨，你来啦"),
    "goodmoring1": ("goodmoring1.jpg", "早安，刚睡醒呢"),
    "goodnight": ("goodnight.jpg", "晚安，先睡了"),
    "teatime": ("teatime.jpg", "说来听听，吃瓜了"),
    "eating": ("eating.jpg", "饿了，先吃点东西"),
    "Allset": ("Allset.jpg", "搞定，交给我吧"),
    "OK": ("OK.jpg", "好的，没问题"),
    "copythat": ("copythat.jpg", "收到，明白了"),
    "Thumbsup": ("Thumbsup.jpg", "厉害，给你点赞"),
    "awesome": ("awesome.jpg", "太厉害了，给你点赞"),
    "sogood": ("sogood.jpg", "真不错，太满意了"),
    "sonice": ("sonice.jpg", "太好了，成了"),
    "fighting": ("fighting.jpg", "加油，你可以的"),
    "hellyeah": ("hellyeah.jpg", "对对对，就是这个"),
    "Thanks": ("Thanks.jpg", "谢谢你呀"),
    "foryou": ("foryou.jpg", "这个给你的"),
    "blushhard": ("blushhard.jpg", "人家脸红了啦"),
    "shyshort": ("shyshort.jpg", "有点不好意思"),
    "hmph": ("hmph.jpg", "哼，生气了哦"),
    "hugtight": ("hugtight.jpg", "来，抱抱你"),
    "Airkiss": ("Airkiss.jpg", "飞吻，接好了"),
    "Gigglelots": ("Gigglelots.jpg", "哈哈，太好笑了"),
    "thinking": ("thinking.jpg", "让我想想"),
    "putmd": ("putmd.jpg", "无语了，不想说话"),
    "Whatswrong": ("Whatswrong.jpg", "怎么了，发生什么了"),
    "midmeh": ("midmeh.jpg", "还行吧，就那样"),
    "awkward": ("awkward.jpg", "这有点尴尬"),
    "Madnow": ("Madnow.jpg", "这次真的生气了"),
    "Hurtcry": ("Hurtcry.jpg", "好难过，忍不住了"),
    "Sobbinghard": ("Sobbinghard.jpg", "感动得哭了"),
    "weeploud": ("weeploud.jpg", "好委屈，哭出来了"),
    "PanincCrying": ("PanincCrying.jpg", "忍不住了，好难过"),
    "missme": ("missme.jpg", "想我了吗"),
    "Free": ("Free.jpg", "放假啦，自由了"),
    "Dreak": ("Dreak.jpg", "不想动了，放过我吧"),
    "outfast": ("outfast.jpg", "溜了溜了"),
    "Vcayover": ("Vcayover.jpg", "假期结束了，不想回去"),
    "sleepynow": ("sleepynow.jpg", "困了，想睡觉"),
    "deadtired": ("deadtired.jpg", "累趴了，动不了了"),
    "sotired": ("sotired.jpg", "好累，趴一会儿"),
    "giveup": ("giveup.jpg", "摆了，不干了"),
    "poorwallet": ("poorwallet.jpg", "钱包空了，没钱了"),
    "please": ("please.jpg", "求求你了嘛"),
}

# 中文口语 -> key 的补充映射，方便 LLM 或用户直接说人话
EXTRA_ALIASES: dict[str, str] = {
    "抱抱": "hugtight",
    "抱一下": "hugtight",
    "亲亲": "Airkiss",
    "飞吻": "Airkiss",
    "晚安": "goodnight",
    "早安": "goodmoring1",
    "早安呀": "goodmoring1",
    "害羞": "blushhard",
    "脸红": "blushhard",
    "生气": "hmph",
    "真生气": "Madnow",
    "难过": "Hurtcry",
    "哭": "weeploud",
    "委屈": "weeploud",
    "开心": "love-happy",
    "喜欢": "love-happy",
    "点赞": "Thumbsup",
    "加油": "fighting",
    "谢谢": "Thanks",
    "感谢": "Thanks",
    "无语": "putmd",
    "尴尬": "awkward",
    "困": "sleepynow",
    "累了": "deadtired",
    "好累": "sotired",
    "摆烂": "giveup",
    "没钱": "poorwallet",
    "求你了": "please",
    "想你": "missme",
    "摸摸": "calm",
    "陪我": "calm",
}

# ===== 每条回复的收尾 =====
# 总闸。关掉之后下面两项都不生效。
AUTO_APPEND = True

# ① 文字末尾落一个语气符号（默认 ♪）。不想要 ♪ 就把它改成 False。
AUTO_SUFFIX_ON = True

# ① 落在句尾的符号本身。
AUTO_SUFFIX = "♪"

# ① 句尾挂着别的表情符号（😊、✨ 之类）时，先摘掉再落 ♪。
REPLACE_TRAILING_EMOJI = True

# ② 回复下方固定补一张表情图。不想每条都带图就把它改成 False。
AUTO_STICKER_ON = True

# ② 从回复里猜不到心情线索时，兜底用的那一张（也是兜底池里的第一张）。
AUTO_STICKER_FALLBACK = "love-happy"

# ② 兜底池：实在挑不出表情时按顺序轮着用，不会再每次都落在同一张上。
FALLBACK_POOL = [
    AUTO_STICKER_FALLBACK,
    "calm",
    "playful",
    "confident",
    "peek",
    "love-calm",
    "thinking",
]

# ② 她自己这一条已经发了图（比如调用了 xilian_send_sticker）时，不再补第二张。
SKIP_STICKER_IF_IMAGE_PRESENT = True

# ② 每补一张写一行 INFO 日志，方便确认它真的在补。
AUTO_STICKER_LOG = True

# ===== ③ 读语境挑表情 =====
# 总闸。关掉就退回纯关键词的老逻辑（快，但容易单调）。
LLM_PICK_ON = True

# 挑法：
#   "auto"    关键词能认出心情就用关键词，认不出才请 LLM（默认，省调用）；
#   "llm"     每条回复都请 LLM 读语境挑（最贴语境，每条多等约 1~3 秒）；
#   "keyword" 从不请 LLM，等价于旧行为。
LLM_PICK_MODE = "auto"

# 指定用哪个 provider 挑表情；留空 = 用当前会话默认的对话模型。
LLM_PICK_PROVIDER_ID = ""

# 等 LLM 挑的超时（秒）。超时就退回关键词 / 兜底，绝不把回复卡住。
LLM_PICK_TIMEOUT = 8.0

# 让 LLM 避开、也让兜底避开「最近用过的这几张」。0 = 不避重。
RECENT_AVOID_N = 5

# 每次挑完（含 LLM 的原始回答）写一行日志，方便回头看它挑得准不准。
LLM_PICK_LOG = True

# 句尾的表情符号：常见 emoji 区段 + 变体选择符 / 零宽连接符。
TRAILING_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U00002190-\U000021FF"
    "\uFE0F\u200D"
    "]+$"
)

# 从回复内容猜心情，顺序即优先级：越具体的说法越靠前。
MOOD_RULES: list[tuple[str, str]] = [
    ("晚安", "goodnight"),
    ("早安", "goodmoring1"),
    ("早上好", "goodmoring1"),
    ("睡不着", "sleepynow"),
    ("困", "sleepynow"),
    ("好累", "deadtired"),
    ("累", "deadtired"),
    ("抱抱", "hugtight"),
    ("抱一下", "hugtight"),
    ("摸摸", "calm"),
    ("陪我", "calm"),
    ("想你", "missme"),
    ("想我", "missme"),
    ("亲亲", "Airkiss"),
    ("飞吻", "Airkiss"),
    ("开心", "love-happy"),
    ("喜欢", "love-happy"),
    ("谢谢", "Thanks"),
    ("感谢", "Thanks"),
    ("加油", "fighting"),
    ("点赞", "Thumbsup"),
    ("厉害", "awesome"),
    ("搞定", "Allset"),
    ("收到", "copythat"),
    ("好耶", "sonice"),
    ("太好", "sonice"),
    ("哈哈", "Gigglelots"),
    ("笑", "Gigglelots"),
    ("委屈", "weeploud"),
    ("难过", "Hurtcry"),
    ("哭", "weeploud"),
    ("生气", "Madnow"),
    ("哼", "hmph"),
    ("无语", "putmd"),
    ("尴尬", "awkward"),
    ("求你了", "please"),
    ("摆烂", "giveup"),
    ("放假", "Free"),
    ("溜了", "outfast"),
]


def mood_stickers(text: str) -> list[str]:
    """从一段话里按优先级列出所有命中的表情 key（可能为空）。"""
    t = text or ""
    hits: list[str] = []
    for word, key in MOOD_RULES:
        if word in t and key not in hits:
            hits.append(key)
    return hits


def mood_sticker(text: str, avoid: list[str] | None = None) -> str | None:
    """从一段话里猜最贴的那张表情，猜不到返回 None。

    avoid 里是最近刚用过的 key，能避开就避开，免得连着两条同一个表情。
    """
    hits = mood_stickers(text)
    if not hits:
        return None
    for key in hits:
        if key not in (avoid or []):
            return key
    return hits[0]


def append_suffix(chain, suffix: str = AUTO_SUFFIX) -> bool:
    """把收尾符号落在链里最后一段文字末尾，末尾已经是它就不再加。

    返回是否真的动了链。
    """
    for comp in reversed(chain):
        if not isinstance(comp, Plain):
            continue
        text = comp.text or ""
        if not text.strip():
            continue
        body = text.rstrip()
        if REPLACE_TRAILING_EMOJI:
            body = TRAILING_EMOJI_RE.sub("", body).rstrip()
        if body.endswith(suffix):
            if body != text:
                comp.text = body
                return True
            return False
        comp.text = body + suffix
        return True
    return False


def _key_index() -> dict[str, str]:
    """key / 文件名 / 文件主名 的小写索引。"""
    idx: dict[str, str] = {}
    for key, (fn, _phrase) in STICKERS.items():
        idx[key.lower()] = key
        idx[fn.lower()] = key
        idx[os.path.splitext(fn)[0].lower()] = key
    return idx


_KEY_INDEX = _key_index()


def resolve_sticker(query: str) -> str | None:
    """把用户/LLM 给的任意说法解析成表情 key，找不到返回 None。"""
    q = (query or "").strip()
    if not q:
        return None
    low = q.lower()

    if low in _KEY_INDEX:
        return _KEY_INDEX[low]
    if q in EXTRA_ALIASES:
        return EXTRA_ALIASES[q]

    # 语义短语 / key 子串匹配
    for key, (_fn, phrase) in STICKERS.items():
        if q == phrase or q in phrase or phrase in q:
            return key
    for alias, key in EXTRA_ALIASES.items():
        if alias in q or q in alias:
            return key
    for key, (fn, _phrase) in STICKERS.items():
        if low in key.lower() or key.lower() in low:
            return key
    return None


def sticker_path(key: str) -> str:
    return os.path.join(STICKER_DIR, STICKERS[key][0])


def list_text(sep: str = "、") -> str:
    return sep.join("%s（%s）" % (key, phrase) for key, (_fn, phrase) in STICKERS.items())


# ===== ④ 剪掉模型漏出来的原始标记 =====

# 总闸。关掉就完全不动正文。
SANITIZE_ON = True

# 剪出来的工具调用里如果正是 xilian_send_sticker，就真的把那张表情发出去。
HONOR_LEAKED_STICKER = True

# 每次剪掉时写一行日志，方便回头确认还有没有别的模型在漏。
SANITIZE_LOG = True

# 成对的工具调用块：<|tool_call_begin|> ... <|tool_call_end|>
TOOL_CALL_BLOCK_RE = re.compile(r"<\|tool_call_begin\|>.*?<\|tool_call_end\|>", re.S)

# 调用块里的参数 JSON：<|tool_call_argument_begin|>{"sticker": "blushhard"}
CALL_ARGS_RE = re.compile(r"<\|tool_call_argument_begin\|>\s*(\{.*?\})", re.S)

# 零散剩下的特殊 token：<|tool_call_begin|>、<|tool_calls_section_end|> 之类
SPECIAL_TOKEN_RE = re.compile(r"<\|[A-Za-z_][A-Za-z0-9_]*\|>")

# 模型自带的短码：.affirmations.love-happy / .emotions.happy 之类
SHORTCODE_RE = re.compile(
    r"\.(?:affirmations?|emotions?|feelings?|stickers?)\.([A-Za-z0-9_\-]+)", re.I
)


def leaked_sticker_keys(text: str) -> list[str]:
    """从漏出来的工具调用里，抠出她本来想发的表情 key。

    只认 ``xilian_send_sticker`` 这种明确的调用；``.affirmations.xxx`` 之类的短码
    只是被剪掉，不当成「想发图」——不然每条带情绪短码的回复都会多出一张图。
    """
    t = text or ""
    keys: list[str] = []

    for body in TOOL_CALL_BLOCK_RE.findall(t):
        if "xilian_send_sticker" not in body:
            continue
        for raw in CALL_ARGS_RE.findall(body):
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if isinstance(data, dict) and isinstance(data.get("sticker"), str):
                key = resolve_sticker(data["sticker"])
                if key and key not in keys:
                    keys.append(key)
    return keys


def sanitize_text(text: str) -> str:
    """剪掉正文里漏出来的工具调用标记与短码，返回干净的正文。"""
    t = text or ""
    t = TOOL_CALL_BLOCK_RE.sub("", t)
    t = CALL_ARGS_RE.sub("", t)
    t = SPECIAL_TOKEN_RE.sub("", t)
    t = SHORTCODE_RE.sub("", t)
    t = re.sub(r"[ \t]+(?=\n)", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def sanitize_chain(chain) -> list[str]:
    """把链里每段文字剪干净，返回她原本想发的表情 key 列表。"""
    leaked: list[str] = []
    drop: list = []
    for comp in list(chain):
        if not isinstance(comp, Plain):
            continue
        text = comp.text or ""
        if not text:
            continue
        for key in leaked_sticker_keys(text):
            if key not in leaked:
                leaked.append(key)
        cleaned = sanitize_text(text)
        if cleaned != text:
            comp.text = cleaned
            if not cleaned:
                drop.append(comp)
    for comp in drop:
        try:
            chain.remove(comp)
        except ValueError:
            pass
    return leaked


# ===== 读语境挑表情：最近用过哪几张 + 兜底轮换 =====

# 会话(umo) -> 最近用过的 key，越靠前越新。只活到进程重启，不落盘。
_RECENT_KEYS: dict[str, list[str]] = {}


def recent_keys(umo: str) -> list[str]:
    """这个会话最近刚用过的那几张，新的在前。"""
    return list(_RECENT_KEYS.get(umo or "", []))


def remember_key(umo: str, key: str) -> None:
    """记下这一条补了哪张，供下一条避重。"""
    if not key or RECENT_AVOID_N <= 0:
        return
    bucket = _RECENT_KEYS.setdefault(umo or "", [])
    if key in bucket:
        bucket.remove(key)
    bucket.insert(0, key)
    del bucket[RECENT_AVOID_N:]


def fallback_key(avoid: list[str] | None = None) -> str:
    """兜底表情：按池子顺序挑一张最近没用过的，不再永远落在同一张上。"""
    pool = [k for k in FALLBACK_POOL if k in STICKERS]
    if not pool:
        pool = [AUTO_STICKER_FALLBACK]
    for key in pool:
        if key not in (avoid or []):
            return key
    return pool[0]


# ===== 读语境挑表情：请一次小 LLM 选 key =====

PICK_SYSTEM_PROMPT = (
    "你是昔涟的「表情包挑选器」。你会看到一段正在发生的对话，"
    "任务是替昔涟挑一张最贴当下情境的表情包。只输出一个 key，"
    "不要标点、不要引号、不要解释、不要输出任何别的东西。"
    "如果情境平淡没明显情绪，就挑一张中性温和的；"
    "如果清单里实在没有贴切的，挑最接近的那张即可，不要自创新 key。"
)


def sticker_menu() -> str:
    """给 LLM 看的表情清单：key = 想表达的意思。"""
    return "\n".join(
        "%s = %s" % (key, phrase) for key, (_fn, phrase) in STICKERS.items()
    )


def build_pick_prompt(
    user_text: str,
    reply_text: str,
    avoid: list[str] | None = None,
) -> str:
    """拼给挑表情 LLM 的提示词。"""
    lines = ["【表情清单】", sticker_menu(), "", "【正在发生的对话】"]
    user_text = (user_text or "").strip()
    reply_text = (reply_text or "").strip()
    if user_text:
        lines.append("对方说：%s" % user_text[:500])
    lines.append("昔涟回复：%s" % reply_text[:1000])
    if avoid:
        lines.append("")
        lines.append("【昔涟最近刚用过这几张，尽量别再挑】")
        lines.append("、".join(avoid))
    lines.append("")
    lines.append("现在只输出一个 key：")
    return "\n".join(lines)


def parse_pick(raw: str) -> str | None:
    """从 LLM 的回答里抠出一个合法 key，抠不出返回 None。"""
    t = (raw or "").strip()
    if not t:
        return None
    if t in STICKERS:
        return t
    low = t.lower()
    if low in _KEY_INDEX:
        return _KEY_INDEX[low]
    # 去掉常见的包裹符号（```json、引号、句号、key: xxx 等）再试
    cleaned = low.strip("`'\"。．，,.、:：;；* \n\t\r")
    cleaned = cleaned.split("\n")[0].strip()
    if cleaned in _KEY_INDEX:
        return _KEY_INDEX[cleaned]
    if cleaned.startswith("key"):
        cleaned = cleaned.lstrip("key").strip(" =:：")
        if cleaned in _KEY_INDEX:
            return _KEY_INDEX[cleaned]
    # 在整段回答里找独立的 key 词（长 key 优先，避免误中短的）
    for cand in sorted(_KEY_INDEX, key=len, reverse=True):
        if re.search(r"(?<![a-z0-9_-])%s(?![a-z0-9_-])" % re.escape(cand), low):
            return _KEY_INDEX[cand]
    # 中文说法兜一层
    for alias, key in EXTRA_ALIASES.items():
        if alias in t:
            return key
    return None


async def llm_pick_sticker(
    context,
    umo: str,
    user_text: str,
    reply_text: str,
    avoid: list[str] | None = None,
) -> str | None:
    """请一次模型读语境挑表情。超时 / 失败 / 挑不出都返回 None，由调用方降级。"""
    try:
        prov = None
        if LLM_PICK_PROVIDER_ID:
            prov = context.get_provider_by_id(LLM_PICK_PROVIDER_ID)
        if prov is None:
            prov = await context.get_using_provider_async(umo)
        if prov is None:
            logger.warning("[xilian_stickers] 没有可用的对话模型，跳过 LLM 挑表情")
            return None

        resp = await asyncio.wait_for(
            prov.text_chat(
                prompt=build_pick_prompt(user_text, reply_text, avoid),
                system_prompt=PICK_SYSTEM_PROMPT,
            ),
            timeout=LLM_PICK_TIMEOUT,
        )
        raw = getattr(resp, "completion_text", "") or ""
        key = parse_pick(raw)
        if LLM_PICK_LOG:
            logger.info(
                "[xilian_stickers] LLM 挑表情：%r -> %s"
                % (raw.strip()[:60], key or "（没抠出 key）")
            )
        return key
    except asyncio.TimeoutError:
        logger.warning(
            "[xilian_stickers] LLM 挑表情超时（%.1fs），退回关键词" % LLM_PICK_TIMEOUT
        )
        return None
    except Exception as e:
        logger.warning("[xilian_stickers] LLM 挑表情失败：%r" % e)
        return None


class XilianStickers(Star):
    """昔涟表情包：让昔涟在 QQ 里发送本机的 52 张表情包。

    /表情 列表 —— 查看所有表情包
    /表情 <key或说法> —— 发送一张，例如 /表情 抱抱、/表情 playful

    AUTO_APPEND 打开时，每条模型回复末尾会自动补一笔收尾（默认 ♪）。
    回复下方补哪一张表情，由 LLM_PICK_MODE 决定（默认 auto：关键词认不出就请 LLM 读语境挑）。
    """

    def __init__(self, context: Context):
        super().__init__(context)
        if not os.path.isdir(STICKER_DIR):
            logger.error("[xilian_stickers] 表情包目录不存在：%s" % STICKER_DIR)
        else:
            n = len([f for f in os.listdir(STICKER_DIR) if not f.startswith("_")])
            logger.info("[xilian_stickers] 表情包目录就绪：%s（%d 个文件）" % (STICKER_DIR, n))
        if not AUTO_APPEND:
            logger.info("[xilian_stickers] 回复收尾：总闸关，什么都不补")
        else:
            suffix_part = (
                "句尾补 %s" % AUTO_SUFFIX if AUTO_SUFFIX_ON else "句尾不补符号"
            )
            sticker_part = (
                "回复下方补表情图（兜底池 %s）" % "、".join(FALLBACK_POOL)
                if AUTO_STICKER_ON
                else "回复下方不补表情"
            )
            if not AUTO_STICKER_ON:
                pick_part = "不挑表情"
            elif LLM_PICK_ON and LLM_PICK_MODE != "keyword":
                pick_part = "挑法 %s（超时 %.1fs，避重 %d 张）" % (
                    LLM_PICK_MODE,
                    LLM_PICK_TIMEOUT,
                    RECENT_AVOID_N,
                )
            else:
                pick_part = "挑法 keyword（只认关键词）"
            logger.info(
                "[xilian_stickers] 回复收尾：%s；%s；%s"
                % (suffix_part, sticker_part, pick_part)
            )

    @filter.on_decorating_result()
    async def auto_append_sticker(self, event: AstrMessageEvent):
        """发送前：先剪掉模型漏出来的原始标记，再给模型回复补上收尾的一笔。指令回复不动。"""
        if not AUTO_APPEND and not SANITIZE_ON:
            return
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return
            if not result.is_llm_result():
                return

            # ④ 剪掉模型漏出来的工具调用标记 / 短码（跟补不补尾巴无关，永远先做）
            leaked: list[str] = []
            if SANITIZE_ON:
                leaked = sanitize_chain(result.chain)
                if SANITIZE_LOG and leaked:
                    logger.info(
                        "[xilian_stickers] 剪掉漏出的工具标记，她原本想发：%s"
                        % "、".join(leaked)
                    )

            umo = getattr(event, "unified_msg_origin", "") or ""

            # ⑤ 她本来想发的那张，代她补上（正文剪干净了，这份心意留着）
            if HONOR_LEAKED_STICKER and leaked and not any(
                isinstance(c, Image) for c in result.chain
            ):
                for key in leaked:
                    path = sticker_path(key)
                    if not os.path.isfile(path):
                        continue
                    result.chain.append(Image.fromFileSystem(path))
                    remember_key(umo, key)
                    if AUTO_STICKER_LOG:
                        logger.info(
                            "[xilian_stickers] 补上她本想要的表情 %s（%s）"
                            % (key, STICKERS[key][1])
                        )
                    break

            if not AUTO_APPEND:
                return

            text = "".join(c.text for c in result.chain if isinstance(c, Plain))
            if not text.strip():
                return

            # ① 文字末尾的语气符号
            if AUTO_SUFFIX_ON:
                append_suffix(result.chain, AUTO_SUFFIX)

            # ② 回复下面补一张表情
            if not AUTO_STICKER_ON:
                return
            if SKIP_STICKER_IF_IMAGE_PRESENT and any(
                isinstance(c, Image) for c in result.chain
            ):
                # 她自己这一条已经发了图（或刚由漏出的调用补上），不再补第二张
                return

            avoid = recent_keys(umo) if RECENT_AVOID_N > 0 else []

            # 先用关键词认一认；认不出、或者模式是 llm，就请 LLM 读语境挑。
            key = mood_sticker(text, avoid)
            if LLM_PICK_ON and LLM_PICK_MODE != "keyword" and (
                key is None or LLM_PICK_MODE == "llm"
            ):
                picked = await llm_pick_sticker(
                    self.context,
                    umo,
                    event.get_message_str(),
                    text,
                    avoid,
                )
                key = picked or key
            # 都没结论就从兜底池里挑一张最近没用过的
            key = key or fallback_key(avoid)

            path = sticker_path(key)
            if not os.path.isfile(path):
                logger.error(
                    "[xilian_stickers] 自动补表情失败，文件不存在：%s" % path
                )
                return
            result.chain.append(Image.fromFileSystem(path))
            remember_key(umo, key)
            if AUTO_STICKER_LOG:
                logger.info(
                    "[xilian_stickers] 已补表情 %s（%s）" % (key, STICKERS[key][1])
                )
        except Exception as e:  # 装饰失败绝不能拖累正常回复
            logger.error("[xilian_stickers] 句尾收尾出错：%r" % e)

    @filter.llm_tool(name="xilian_send_sticker")
    async def xilian_send_sticker(self, event: AstrMessageEvent, sticker: str):
        """给用户发一张昔涟的本机表情包。

        想用表情回应用户、撒娇、卖萌或表达心情时调用本工具，比纯文字更有温度。
        参数 sticker 可以填下表的 key，也可以直接填中文意思（如「抱抱」「生气」「晚安」）。
        可选表情与含义：playful=你看人家嘛；love-happy=好开心喜欢你；confident=交给人家放心；serious=说正经的；calm=静静陪着你；peek=偷偷看一眼；clingy-confused=等等人家嘛；love-calm=这颗心给你的；HI=嗨想我了吗；hello=嗨你来啦；goodmoring1=早安；goodnight=晚安；teatime=吃瓜听你说；eating=饿了吃点东西；Allset=搞定交给我；OK=好的没问题；copythat=收到明白；Thumbsup=厉害点赞；awesome=太厉害了；sogood=真不错；sonice=太好了成了；fighting=加油你可以的；hellyeah=对对对就是；Thanks=谢谢你呀；foryou=这个给你的；blushhard=人家脸红了；shyshort=有点不好意思；hmph=哼生气了；hugtight=来抱抱你；Airkiss=飞吻接好；Gigglelots=哈哈太好笑了；thinking=让我想想；putmd=无语不想说话；Whatswrong=怎么了；midmeh=还行吧；awkward=有点尴尬；Madnow=真的生气了；Hurtcry=好难过；Sobbinghard=感动哭了；weeploud=好委屈；PanincCrying=难过得受不了；missme=想我了吗；Free=放假啦；Dreak=不想动了；outfast=溜了溜了；Vcayover=假期结束了；sleepynow=困了想睡觉；deadtired=累趴了；sotired=好累趴一会儿；giveup=摆烂不干了；poorwallet=钱包空了；please=求求你了嘛。

        Args:
            sticker(string): 表情包的 key 或中文意思，例如 hugtight、抱抱、生气
        """
        key = resolve_sticker(sticker)
        if key is None:
            yield (
                "没有找到对应的表情包：「%s」。可用 key：%s"
                % (sticker, "、".join(STICKERS.keys()))
            )
            return

        path = sticker_path(key)
        if not os.path.isfile(path):
            logger.error("[xilian_stickers] 表情包文件缺失：%s" % path)
            yield "表情包文件缺失了：%s" % path
            return

        yield event.image_result(path)
        yield "已发送表情包 %s（%s）" % (key, STICKERS[key][1])

    @filter.command("表情", alias={"sticker"})
    async def sticker_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """查看或发送昔涟表情包。用法：/表情 列表 ；/表情 抱抱"""
        arg = (arg or "").strip()
        if not arg or arg in {"列表", "list", "帮助", "help", "-h"}:
            yield event.plain_result(
                "昔涟的表情包一共 %d 张呀：\n%s\n\n用法：/表情 <key 或说法>，比如 /表情 抱抱、/表情 goodnight"
                % (len(STICKERS), list_text())
            )
            return

        key = resolve_sticker(arg)
        if key is None:
            yield event.plain_result(
                "没找到「%s」这张呢…可用的是这些：\n%s" % (arg, list_text())
            )
            return

        path = sticker_path(key)
        if not os.path.isfile(path):
            logger.error("[xilian_stickers] 表情包文件缺失：%s" % path)
            yield event.plain_result("这张表情包的文件不见了：%s" % path)
            return

        yield event.image_result(path)
