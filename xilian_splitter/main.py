# -*- coding: utf-8 -*-
"""xilian_splitter —— 昔涟的「一句一句说」。

参考 nuomicici/astrbot_plugin_splitter（对话分段Pro）的思路重写的一版。

那个插件很能干，也很重：17KB 的配置 schema、三种模式、逐段 TTS 合成、
主动发送劫持、会话黑白名单……这台机器上真正用得上的，其实只有一件事——

    把一条长回复切成几段短消息，一段一段发出去。

所以这里只留下这件事，再添几处昔涟用得上的小心思：

  · 先切句、再并段 —— 按句末标点和逗号切细；没有标点的长句会在词与词之间补一刀，
                  再并成 6～9 字左右，凑出真人聊天一行的长度；
  · 该护住的都护住 —— 代码块、<think>、Markdown 表格、成对符号内部不切；
  · 图片和表情不落单 —— 跟着上一段一起走；
  · 拟真延迟 —— 下一段越长，等得越久一点；
  · 和语音配合 —— 文字一句一句地发，语音仍然只有一条（整条回复读下来）。
                 她说到哪句换了口气，那条语音里也听得出来：按分段的语气
                 逐段合成、再拼成一条，交给 xilian_tts 的 get_audio_segmented。
                 详细玩法见下面的 TTS_MODE。

拦点在 on_decorating_result，而且排在最后。前几段自己用 context.send_message
发出去，最后一段留在 result.chain 里交给框架 —— 这样引用、转发、文本转图片
这些装饰还都落在最后一段上，不会乱。

分工：防抖（xilian_debounce）管「你还没说完」，这里管「她一次说得太多」。

指令：/分段 …

注意：WebUI 里框架自带的「分段回复」（enable_segmented_reply）建议关掉，
两个一起开会把同一条回复切两遍。
"""

from __future__ import annotations

import asyncio
import math
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import BaseMessageComponent, Plain, Record, Reply
from astrbot.api.star import Context, Star

# ── 可调项 ────────────────────────────────────────────────────────────────

# 总闸。False 时完全不介入，回复照原样一整条发出去。
ENABLE = True

# 生效范围：private（只私聊）/ group（只群聊）/ all（都管）。
SCOPE = "private"

# 能改设置的 QQ。留空则任何管理员都能改。
MASTER_ID = "3614298015"

# 少于这个字数就整条原样发出，不动。太短的回复再切就只剩下碎屑了。
MIN_LEN = 25

# 一段大概多长。真人在聊天框里通常一行 6～9 个字，这里按 8 字瞄准。
# 注意这个值会被下面这条抬高：target = max(TARGET_LEN, 总字数 / MAX_SEGMENTS)。
# 想让短段维持得久一点，MAX_SEGMENTS 就得跟着放大。
TARGET_LEN = 8

# 最多切成几段。剩下的全并进最后一段。
# 30 段 × 8 字，约 240 字以内的回复都能保持这个粒度，更长的会变粗。
MAX_SEGMENTS = 30

# 尾巴短于这个字数，就并回前一段。免得最后蹦出「好呀♪」这种小碎片。
MIN_TAIL = 5

# 一块最长能有多长。标点断出来的块超过这个字数、又找不到下刀的地方，
# 就交给硬切在词与词之间补一刀（见 _split_oversized）。调大 = 更保守，调小 = 更碎。
HARD_MAX = 10

# 要不要动硬切。关掉之后，没有标点的长句会整句成段 —— 宁可长，也不切坏词。
HARD_CUT = True

# 段与段之间等一会儿：基础值 + 下一段字数 × 系数，封顶。
DELAY_BASE = 0.25
DELAY_PER_CHAR = 0.055
DELAY_MAX = 2.5

# 第一段带上引用，让上下文更清楚。平台不支持时会被忽略。
QUOTE_FIRST = True

# 这一条会走语音（TTS）时怎么办：
#   "full" —— 照拆；整条回复另合成一条语音（默认）。
#             语音按分段的语气逐段合成再拼起来，所以文字是一句一句的、
#             声音却是一个人在说，中间换气的起伏也还在。
#   "seg"  —— 照拆，每段各配一条语音（前几段自己配，最后一段框架配）
#   "text" —— 照拆，但不配语音（省几次合成）
#   "skip" —— 不拆，保持整条语音完整
TTS_MODE = "full"

# 旧值兼容：以前叫 "voice"，指的是「每段各配一条语音」，也就是现在的 "seg"。
TTS_MODE_ALIASES = {"voice": "seg", "整条": "full", "每段": "seg", "不拆": "skip"}


# ── 小工具 ────────────────────────────────────────────────────────────────


def _short(text: str, n: int = 40) -> str:
    """日志和预览用的一行摘要。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= n else flat[:n] + "…"


# 句末标点。英文句点不切，免得把网址、版本号、小数点切开。
_SENT_END = "。！？!?…♪♬~～"

# 次级断点。中文一句话动不动 25 字以上，只靠句末标点切，段长压不到 6～9 字，
# 所以在逗号、分号、冒号处也允许断。只认中文标点：英文逗号容易撞上数字和网址。
_CLAUSE_END = "，；："

# 次级断点前面至少要有这么多字才下刀，免得切出「对了对了，」这种片断。
# 这里连逗号一起数：「对了对了，」是 5 字，取 6 正好把它挡住；
# 而 8 字一段的粒度又几乎不会碰到这条线，两边都不耽误。
CLAUSE_MIN = 6

# 句末标点后面跟着的收尾符号，一起带上。
_TAIL_CHARS = "。！？!?…♪♬~～~～」』）】》"

# 空行（连着两个换行）才算段落边界；单个换行可能是在列表里。
_BLANK_LINE = re.compile(r"\n[ \t]*\n")

# 成对符号，内部不切。
PAIRS = {
    "「": "」",
    "『": "』",
    "（": "）",
    "(": ")",
    "【": "】",
    "《": "》",
    "[": "]",
    "{": "}",
    "“": "”",
    "‘": "’",
    '"': '"',
    "'": "'",
    "`": "`",
}


def cut_sentences(text: str) -> list[str]:
    """把整段文字切成句子。

    代码块、<think>、Markdown 表格、成对符号内部都当整体，不在里面下刀。
    """
    out: list[str] = []
    buf = ""
    stack: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        # ``` 代码块，整块留下
        if text.startswith("```", i) and (i == 0 or text[i - 1] == "\n"):
            end = text.find("```", i + 3)
            if end == -1:
                buf += text[i:]
                i = n
            else:
                buf += text[i : end + 3]
                i = end + 3
            continue

        # <think>…</think>，推理过程不切
        if text.startswith("<think>", i) and (i == 0 or text[i - 1] == "\n"):
            end = text.find("</think>", i + 7)
            if end == -1:
                buf += text[i:]
                i = n
            else:
                buf += text[i : end + 8]
                i = end + 8
            continue

        # Markdown 表格：以 | 开头的连续行（含 |---|---| 分隔行）整体保留
        if text[i] == "|" and (i == 0 or text[i - 1] == "\n"):
            moved = False
            while i < n:
                line_end = text.find("\n", i)
                line_end = n if line_end == -1 else line_end
                line = text[i:line_end].strip()
                if line.startswith("|") or (line and set(line) <= set("-| :")):
                    buf += text[i:line_end]
                    i = line_end
                    if i < n:
                        buf += "\n"
                        i += 1
                    moved = True
                else:
                    break
            if moved:
                continue

        ch = text[i]

        # 空行 → 段落边界
        if ch == "\n":
            m = _BLANK_LINE.match(text, i)
            if m:
                buf += text[i : m.end()]
                out.append(buf)
                buf = ""
                i = m.end()
                continue

        # 成对符号进出栈
        if not stack and ch in PAIRS:
            stack.append(ch)
        elif stack and ch == PAIRS.get(stack[-1]):
            stack.pop()

        buf += ch
        i += 1

        # 不在任何成对符号里，遇到句末标点就在这里断
        if not stack and ch in _SENT_END:
            while i < n and text[i] in _TAIL_CHARS:
                buf += text[i]
                i += 1
            out.append(buf)
            buf = ""
        # 句内也允许在逗号处喘口气（仅在前面够长时）
        elif not stack and ch in _CLAUSE_END and len(buf.strip()) >= CLAUSE_MIN:
            out.append(buf)
            buf = ""

    if buf.strip():
        out.append(buf)

    return [s for s in out if s.strip()]


def _protected(block: str) -> bool:
    """这些块整块保留，里面不下刀。"""
    return "```" in block or "<think>" in block or block.lstrip().startswith("|")


# 硬切用的避让表。中文没有空格，硬切只能靠启发式；这两张表挡掉最容易切坏的两种位置：
#   · 切点后面的字如果是「们 / 的 / 了…」，前面多半是半个词（「我 | 们」）；
#   · 切点前面的字如果是「我 / 这 / 就…」，后面多半是它的搭档（「这 | 样」）。
_BAD_FIRST = set("们的了着地得吗呢吧啊呀哦嘛么乎兮")
_BAD_LAST = set("我你他她它这那们很就都也还不没在是有要会能可把被给对与从向而但才又再并且因以为于若即使让")


def _pair_mask(s: str) -> list[bool]:
    """标出哪些位置落在成对符号里面（连符号本身一起标）。

    硬切只顾着在词与词之间找位置，看不到「」（）的边界，
    会从引号中间直直穿过去。拿这张表挡一下。
    """
    mask = [False] * len(s)
    stack: list[tuple[str, int]] = []
    for i, ch in enumerate(s):
        if not stack and ch in PAIRS:
            stack.append((ch, i))
        elif stack and ch == PAIRS.get(stack[-1][0]):
            _opener, begin = stack.pop()
            for k in range(begin, i + 1):
                mask[k] = True
    return mask


def _ok_cut(s: str, i: int, mask: list[bool] | None = None) -> bool:
    """切在 s[i] 之前，会不会切坏词、或者穿过成对符号。"""
    if i <= 0 or i >= len(s):
        return False
    if mask is not None and (mask[i - 1] or mask[i]):
        return False
    return s[i - 1] not in _BAD_LAST and s[i] not in _BAD_FIRST


def _find_cut(s: str, target: int, hard_max: int, margin: int) -> int:
    """在 s 里挑一刀最自然的位置：尽量靠近 target，又不切坏词。挑不到就返回 0。"""
    lo = max(margin, 1)
    hi = min(hard_max, len(s) - margin)
    if hi < lo:
        return 0
    mask = _pair_mask(s)
    center = max(lo, min(hi, target))
    for d in range(hi - lo + 1):
        for i in (center - d, center + d):
            if lo <= i <= hi and _ok_cut(s, i, mask):
                return i
    return 0


def _split_oversized(blocks: list[str], target: int, hard_max: int) -> list[str]:
    """把太长、又没处下刀的块再切细。

    只碰超过 hard_max 的普通文字块；代码块、表格、<think> 一律原样保留。
    碎片首尾不改动，拼起来仍是原来的串 —— 后面算位置全靠这个。
    """
    out: list[str] = []
    for block in blocks:
        if len(block) <= hard_max or _protected(block):
            out.append(block)
            continue
        rest = block
        while len(rest) > hard_max:
            i = _find_cut(rest, target, hard_max, max(2, MIN_TAIL))
            if i <= 0:
                break
            out.append(rest[:i])
            rest = rest[i:]
        out.append(rest)
    return out


def pack_spans(sentences: list[str], target: int, max_segments: int) -> list[tuple[int, int]]:
    """把句子并成几段，返回每段在原文里的 [start, end) 位置。

    不是「攒够 target 就断」，而是每段都在目标长度附近挑一个最接近的切点 ——
    这样段长不会因为中间冒出一句特别长的话而失控。
    目标长度还会随「剩下的字 ÷ 剩下的段」浮动，免得最后一段拖长大尾巴。
    """
    if not sentences:
        return []
    total_len = sum(len(s) for s in sentences)
    if len(sentences) == 1:
        return [(0, total_len)]

    limit = max(1, max_segments)

    spans: list[tuple[int, int]] = []
    start = 0
    i = 0
    n = len(sentences)

    while i < n and len(spans) < limit - 1:
        # 这一段的目标长度：至少 target，但也得把剩下的字摊给剩下的段 ——
        # 不然前面几段贪得多一点，最后一段就会拖出一条长尾巴。
        budget = limit - len(spans)
        goal = max(target, math.ceil((total_len - start) / budget))
        lo = max(1, int(goal * 0.75))
        hi = max(lo + 1, int(goal * 1.5))

        acc = 0
        best_j = -1
        best_total = 0
        best_diff = None
        j = i
        while j < n:
            acc += len(sentences[j])
            if acc >= lo:
                diff = abs(acc - goal)
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_j = j
                    best_total = acc
            if acc >= hi:
                break
            j += 1
        if best_j < 0:
            break
        spans.append((start, start + best_total))
        start += best_total
        i = best_j + 1

    if start < total_len:
        spans.append((start, total_len))

    return spans


def build_segments(
    chain: list[BaseMessageComponent],
    text: str,
    spans: list[tuple[int, int]],
) -> list[list[BaseMessageComponent]]:
    """按位置区间把整条消息链拆成几段。图片、表情这些跟着上一段走。"""
    segments: list[list[BaseMessageComponent]] = []
    for start, end in spans:
        seg: list[BaseMessageComponent] = []
        piece = text[start:end].strip()
        if piece:
            seg.append(Plain(piece))
        segments.append(seg)

    # 非文本组件：看它前面有多少纯文本，就知道该站哪一段
    offset = 0
    for comp in chain:
        if isinstance(comp, Plain):
            offset += len(comp.text)
            continue
        if isinstance(comp, Reply):
            continue  # 引用单独处理
        idx = 0
        for i, (start, _end) in enumerate(spans):
            if start <= offset:
                idx = i
        segments[idx].append(comp)

    return [seg for seg in segments if seg]


def delay_for(text: str) -> float:
    """下一段越长，等得越久一点。"""
    return min(DELAY_MAX, DELAY_BASE + len(text) * DELAY_PER_CHAR)


def _plain_of(seg: list[BaseMessageComponent]) -> str:
    return "".join(c.text for c in seg if isinstance(c, Plain))


def _all_text(segments: list[list[BaseMessageComponent]]) -> str:
    """所有段拼起来的纯文本 —— 整条语音要念的就是它。"""
    return "".join(_plain_of(seg) for seg in segments)


# ── 插件本体 ──────────────────────────────────────────────────────────────


class XilianSplitter(Star):
    """把一条长回复拆成几句，一句一句说出去。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        # 运行时可改的几项（/分段 段长 200 …）
        self._enabled = bool(ENABLE)
        self._scope = str(SCOPE or "private")
        self._min_len = int(MIN_LEN)
        self._target = int(TARGET_LEN)
        self._max_segments = int(MAX_SEGMENTS)
        self._per_char = float(DELAY_PER_CHAR)
        self._quote = bool(QUOTE_FIRST)
        mode = str(TTS_MODE or "full").strip().lower()
        self._tts_mode = TTS_MODE_ALIASES.get(mode, mode)
        self._hard_cut = bool(HARD_CUT)

        logger.info(
            "[xilian_splitter] 已装好：%d 字起分段 / 一段约 %d 字（上限 %d）/ 最多 %d 段 / 硬切 %s / 范围 %s"
            % (
                self._min_len,
                self._target,
                HARD_MAX,
                self._max_segments,
                "开" if self._hard_cut else "关",
                self._scope,
            )
        )

    # ── 小工具 ────────────────────────────────────────────────────────────

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = True
        if self._scope == "all":
            return True
        if self._scope == "group":
            return not is_private
        return is_private

    @staticmethod
    def _is_master(event: AstrMessageEvent) -> bool:
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            sender = ""
        if MASTER_ID:
            return sender == MASTER_ID
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    async def _tts_active(self, event: AstrMessageEvent) -> bool:
        """这一条会不会走语音输出。会的话就别拆了。"""
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin) or {}
            if not cfg.get("provider_tts_settings", {}).get("enable"):
                return False
        except Exception:
            return False
        try:
            prov = await self.context.get_using_tts_provider_async(event.unified_msg_origin)
        except Exception:
            prov = None
        return prov is not None

    def _plan(self, text: str) -> list[tuple[int, int]]:
        """算出该在哪里下刀。返回每段的 [start, end)。"""
        sentences = cut_sentences(text)
        if self._hard_cut:
            sentences = _split_oversized(sentences, self._target, HARD_MAX)
        if len(sentences) <= 1:
            return []

        # 段长随总字数浮动，保证段数不超过上限
        target = max(self._target, math.ceil(len(text) / max(1, self._max_segments)))
        spans = pack_spans(sentences, target, self._max_segments)
        if len(spans) <= 1:
            return []

        # 尾巴太短就并回前一段
        if len(spans) >= 2:
            tail_len = spans[-1][1] - spans[-1][0]
            if tail_len < MIN_TAIL:
                spans[-2] = (spans[-2][0], spans[-1][1])
                spans.pop()

        return spans if len(spans) > 1 else []

    # ── 主拦点：回复组装完毕、还没发出去 ──────────────────────────────────

    @filter.on_decorating_result(priority=-100000000000000000)
    async def split_reply(self, event: AstrMessageEvent) -> None:
        if not self._enabled:
            return
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return
            if getattr(result, "__xilian_split_done", False):
                return
            if not self._in_scope(event):
                return

            # 只管她自己说的话。指令回执、别的插件转发的不碰
            try:
                if not result.is_model_result():
                    return
            except Exception:
                pass

            tts_on = await self._tts_active(event)
            if tts_on and self._tts_mode == "skip":
                logger.debug("[xilian_splitter] 这条会走语音，不拆")
                return

            text = "".join(c.text for c in result.chain if isinstance(c, Plain))
            if len(text.strip()) < self._min_len:
                return

            spans = self._plan(text)
            if not spans:
                return

            segments = build_segments(result.chain, text, spans)
            if len(segments) <= 1:
                return

            setattr(result, "__xilian_split_done", True)
            await self._send_segments(event, result, segments, tts_on)
        except Exception as e:  # noqa: BLE001
            # 拆不动就算了，宁可整条发出去，别把人卡住
            logger.error("[xilian_splitter] 分段时出错，本条照原样发：%r" % e)

    async def _send_segments(
        self,
        event: AstrMessageEvent,
        result,
        segments: list[list[BaseMessageComponent]],
        tts_on: bool = False,
    ) -> None:
        """前几段自己发，最后一段看情况。

        语音怎么配由 self._tts_mode 定：
          full —— 整条回复另合成一条语音。最后一段也自己发，result 里只留那条语音，
                  免得框架又把末段文字合成一遍；
          seg  —— 前几段各配一条语音，最后一段留给框架配；
          text —— 只发文字，最后一段留给框架；
          skip —— 走不到这里，前面已经拦掉了。
        """
        total = len(segments)
        want_seg = tts_on and self._tts_mode == "seg"
        want_full = tts_on and self._tts_mode == "full"

        if self._quote:
            self._prepend_reply(segments[0], event)

        # 整条语音先在后台念起来：文字一段段发出去的时候，它正好在合成。
        voice_task = None
        if want_full:
            voice_task = asyncio.create_task(
                self._synth_full_voice(event, [_plain_of(seg) for seg in segments])
            )

        for i, seg in enumerate(segments[:-1]):
            try:
                if want_seg:
                    seg = await self._voice_wrap(event, seg)
                chain = result.derive(chain=seg)
                await self.context.send_message(event.unified_msg_origin, chain)
                logger.info(
                    "[xilian_splitter] 第 %d/%d 段已发出：%s"
                    % (i + 1, total, _short(_plain_of(seg)))
                )
            except Exception as e:  # noqa: BLE001
                logger.error("[xilian_splitter] 第 %d 段没发出去：%r" % (i + 1, e))

            await asyncio.sleep(delay_for(_plain_of(segments[i + 1])))

        last = segments[-1]

        # 整条语音备好了：最后一段也自己发，result 里只留这条语音。
        if voice_task is not None:
            voice_path = await voice_task
            if voice_path:
                await self._send_last(event, result, last, total)
                result.chain.clear()
                result.chain.append(
                    Record(file=voice_path, url=voice_path, text=_all_text(segments))
                )
                logger.info(
                    "[xilian_splitter] 共 %d 段；整条语音交给框架发出：%s"
                    % (total, _short(voice_path))
                )
                return

        # 没有整条语音（合成失败或没开）：最后一段照旧交给框架。
        result.chain.clear()
        result.chain.extend(last)
        logger.info(
            "[xilian_splitter] 共 %d 段，最后一段交给框架：%s"
            % (total, _short(_plain_of(last)))
        )

    async def _synth_full_voice(
        self,
        event: AstrMessageEvent,
        texts: list[str],
    ) -> str | None:
        """把整条回复合成成一条语音。合不出来就返回 None，文字照发。

        优先用支持「分段语气合成」的 provider（xilian_tts 的 get_audio_segmented）：
        分段喂进去，逐段判语气、语气变了才换调子，最后拼成一条。
        换成别的 TTS provider 时退回整条读一遍 —— 不挑食。
        """
        try:
            prov = await self.context.get_using_tts_provider_async(event.unified_msg_origin)
        except Exception:  # noqa: BLE001
            prov = None
        if prov is None:
            return None

        pieces = [t for t in texts if t and t.strip()]
        if not pieces:
            return None

        try:
            segmented = getattr(prov, "get_audio_segmented", None)
            if callable(segmented):
                return await segmented(pieces)
            return await prov.get_audio("".join(pieces))
        except Exception as e:  # noqa: BLE001
            logger.warning("[xilian_splitter] 整条语音没合成出来，这一条就只发文字：%r" % e)
            return None

    async def _send_last(
        self,
        event: AstrMessageEvent,
        result,
        seg: list[BaseMessageComponent],
        total: int,
    ) -> None:
        """整条语音模式下，最后一段文字也由自己发出去。"""
        try:
            await self.context.send_message(
                event.unified_msg_origin, result.derive(chain=seg)
            )
            logger.info(
                "[xilian_splitter] 第 %d/%d 段已发出：%s"
                % (total, total, _short(_plain_of(seg)))
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[xilian_splitter] 最后一段没发出去：%r" % e)

    async def _voice_wrap(
        self,
        event: AstrMessageEvent,
        seg: list[BaseMessageComponent],
    ) -> list[BaseMessageComponent]:
        """给这一段配上语音。

        拿不到 provider、或者念不出来，就原样返回 —— 绝不因为配音失败把内容弄丢。
        只在 "seg" 模式下用（每段各配一条语音），而且只用在「自己发出去的那几段」上；
        最后一段留给框架，框架自己会配。
        """
        try:
            prov = await self.context.get_using_tts_provider_async(event.unified_msg_origin)
        except Exception:
            prov = None
        if prov is None:
            return seg

        dual = True
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin) or {}
            dual = bool(cfg.get("provider_tts_settings", {}).get("dual_output", True))
        except Exception:
            pass

        out: list[BaseMessageComponent] = []
        for comp in seg:
            if isinstance(comp, Plain) and len(comp.text.strip()) > 1:
                try:
                    path = await prov.get_audio(comp.text)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[xilian_splitter] 这一段没念出来，改发文字：%r" % e)
                    out.append(comp)
                    continue
                if path:
                    out.append(Record(file=path, url=path, text=comp.text))
                    if dual:
                        out.append(comp)
                    continue
            out.append(comp)
        return out

    @staticmethod
    def _prepend_reply(seg: list[BaseMessageComponent], event: AstrMessageEvent) -> None:
        try:
            mid = getattr(event.message_obj, "message_id", None)
            if not mid:
                return
            if any(isinstance(c, Reply) for c in seg):
                return
            seg.insert(0, Reply(id=str(mid)))
        except Exception:
            pass

    # ── 指令 ──────────────────────────────────────────────────────────────

    @filter.command("分段", alias={"split", "分段设置"})
    async def split_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """看看或改改「一句一句说」的设置。用法：/分段 状态 / 开 / 关 / 试 <文本>"""
        if not self._is_master(event):
            yield event.plain_result("这个开关在人家自己手里，别人碰不到哦。")
            return

        args = [a for a in (arg or "").split() if a]
        head = args[0] if args else ""

        if not head or head in {"状态", "status"}:
            yield event.plain_result(self._status_text())
            return

        if head in {"开", "开启", "on"}:
            self._enabled = True
            yield event.plain_result("好呀，那人家就一句一句地说♪")
            return

        if head in {"关", "关闭", "off"}:
            self._enabled = False
            yield event.plain_result("行，那人家整段一起说。")
            return

        if head in {"试", "预览", "test"}:
            parts = (arg or "").split(None, 1)
            sample = parts[1].strip() if len(parts) > 1 else ""
            if not sample:
                yield event.plain_result(
                    "想试哪一段呀？这样写：/分段 试 嗨♪好久不见！今天也要开心哦～"
                )
                return
            yield event.plain_result(self._preview(sample))
            return

        if head in {"长度", "门槛", "min"}:
            if len(args) >= 2:
                self._min_len = self._clamp(args[1], self._min_len, 10, 2000)
            yield event.plain_result("超过 %d 字才分段。" % self._min_len)
            return

        if head in {"段长", "target"}:
            if len(args) >= 2:
                self._target = self._clamp(args[1], self._target, 5, 1000)
            yield event.plain_result("一段大概 %d 字。" % self._target)
            return

        if head in {"段数", "max"}:
            if len(args) >= 2:
                self._max_segments = self._clamp(args[1], self._max_segments, 2, 40)
            yield event.plain_result("最多切 %d 段。" % self._max_segments)
            return

        if head in {"延迟", "delay"}:
            if len(args) >= 2:
                try:
                    self._per_char = max(0.0, min(1.0, float(args[1])))
                except ValueError:
                    yield event.plain_result("要一个 0~1 之间的小数呀，比如 0.06。")
                    return
            yield event.plain_result("每字等 %.3f 秒（加底 %.2f 秒）。" % (self._per_char, DELAY_BASE))
            return

        if head in {"范围", "scope"}:
            if len(args) >= 2 and args[1] in {"private", "group", "all"}:
                self._scope = args[1]
            yield event.plain_result("生效范围：%s" % self._scope)
            return

        if head in {"引用", "quote"}:
            if len(args) >= 2 and args[1] in {"开", "on", "关", "off"}:
                self._quote = args[1] in {"开", "on"}
            yield event.plain_result("第一段%s引用。" % ("带" if self._quote else "不带"))
            return

        if head in {"语音", "tts"}:
            if len(args) >= 2:
                want = TTS_MODE_ALIASES.get(
                    args[1].strip().lower(), args[1].strip().lower()
                )
                if want in {"full", "seg", "text", "skip"}:
                    self._tts_mode = want
            yield event.plain_result(
                "开着语音的时候%s。"
                % {
                    "full": "文字照拆，整条回复合成一条语音（各段语气都留在里面）",
                    "seg": "文字照拆，每段各配一条语音",
                    "text": "文字照拆，不配语音",
                    "skip": "不拆，整条发",
                }.get(self._tts_mode, self._tts_mode)
            )
            return

        if head in {"硬切", "hard"}:
            if len(args) >= 2 and args[1] in {"开", "on", "关", "off"}:
                self._hard_cut = args[1] in {"开", "on"}
            yield event.plain_result(
                "没有标点的长句%s。"
                % (
                    "会在词与词之间断开（上限 %d 字）" % HARD_MAX
                    if self._hard_cut
                    else "整句成段，宁可长也不切坏词"
                )
            )
            return

        yield event.plain_result(
            "用法：/分段 状态 / 开 / 关 / 试 <文本>\n"
            "　　　/分段 长度 25 / 段长 8 / 段数 30 / 延迟 0.055\n"
            "　　　/分段 范围 private / 引用 开 / 语音 full / 硬切 开"
        )

    # ── 状态与预览 ────────────────────────────────────────────────────────

    def _status_text(self) -> str:
        lines = [
            "「一句一句说」现在%s。" % ("开着" if self._enabled else "关着"),
            "%d 字以上才动手，一段大概 %d 字（单块上限 %d 字），最多 %d 段；尾巴短于 %d 字会并回上一段。"
            % (self._min_len, self._target, HARD_MAX, self._max_segments, MIN_TAIL),
            "没有标点的长句%s。"
            % ("会在词与词之间断开" if self._hard_cut else "整句成段，不硬切"),
            "段与段之间等 %.2f 秒 + 下一段字数 × %.3f 秒（封顶 %.1f 秒）。"
            % (DELAY_BASE, self._per_char, DELAY_MAX),
            "生效范围 %s；第一段%s引用；开着语音时%s。"
            % (
                self._scope,
                "带" if self._quote else "不带",
                {
                    "full": "整条合成一条语音（分段语气保留）",
                    "seg": "每段各配一条语音",
                    "text": "只发文字，不配语音",
                    "skip": "不拆",
                }.get(self._tts_mode, self._tts_mode),
            ),
        ]
        return "\n".join(lines)

    def _preview(self, text: str) -> str:
        if len(text.strip()) < self._min_len:
            return "%d 字 —— 不到 %d 字的门槛，会整条发出。" % (len(text), self._min_len)

        spans = self._plan(text)
        if len(spans) <= 1:
            return "%d 字 —— 切不出两段，会整条发出。" % len(text)

        sentences = cut_sentences(text)
        if self._hard_cut:
            sentences = _split_oversized(sentences, self._target, HARD_MAX)

        lines = ["%d 字，切出 %d 块，准备发 %d 段：" % (len(text), len(sentences), len(spans))]
        for i, (s, e) in enumerate(spans, 1):
            piece = text[s:e].strip()
            lines.append("%d. [%d字] %s" % (i, len(piece), _short(piece, 60)))
        return "\n".join(lines)

    @staticmethod
    def _clamp(raw: str, current: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(float(raw))))
        except (TypeError, ValueError):
            return current

    async def terminate(self) -> None:
        logger.info("[xilian_splitter] 已卸下。")
