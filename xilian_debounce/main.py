# -*- coding: utf-8 -*-
"""xilian_debounce —— 昔涟的「等一下再说」。

参考 advent259141/astrbot_plugin_debounce 的思路重写的一版。

原版用微调的 BERT 判断「这句话说完了没有」，要装 onnxruntime + transformers，
还要从 ModelScope 下 10~100MB 的模型。这台机器上三样都没有，也不想为了一句
话的完整度去背一整套推理栈。所以换了条更笨、但更稳的路：

  · 时间窗口 —— 消息进来先按住几秒；这期间又来了新的，就并成同一条再说；
  · 上限兜底 —— 从第一条算起最多等 M 秒，免得一直发就一直不说；
  · 过时作废 —— 合并后的消息送出去时，如果上一条回复还在生成，就让它作废，
                 免得她答了一半又被追着答一遍；
  · 唤醒词不碰 —— 前缀由 AstrBot 原本的流程剥，这里只接剥完的正文。

拦点在 on_waiting_llm_request：确定要调 LLM、还没排队等锁的那一刻。
`event.stop_event()` 之后，这条消息不进会话锁、不进上下文、也不会有回复，
它只是安静待在缓冲区里，等后面几条一起走。

分工：回忆匣（xilian_memory）管「很久以前」，这里管「刚才这十几秒」。

指令：/防抖 …
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

# ── 可调项 ────────────────────────────────────────────────────────────────

# 总闸。False 时完全不介入，消息一条一条照原样走。
ENABLE = True

# 最后一条消息之后，再等这么久才把攒的话一起送出去。
WINDOW_SECONDS = 2.0

# 从第一条消息算起最多等这么久。防止对方一直发、她一直不说。
MAX_WAIT_SECONDS = 10.0

# 一次最多并几条、多少字。超了就当场收口，先答一版。
MAX_MESSAGES = 8
MAX_CHARS = 1500

# 合并消息送出去时，若上一条回复还在生成，就把它作废。
# False 的话，她会先答上一条、再答合并后的这一条（多一次回复，但不会丢话）。
CANCEL_STALE = True

# 生效范围：private（只私聊）/ group（只群聊）/ all（都管）。
# 群里会按发送者分开攒，不会把两个人的半句话并到一起。
SCOPE = "private"

# 能改设置的 QQ。留空则任何管理员都能改。
MASTER_ID = "3614298015"


def _short(text: str, n: int = 80) -> str:
    """日志用的一行摘要。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= n else flat[:n] + "…"


@dataclass
class _Pending:
    """一个会话里攒着、还没说出口的几句话。"""

    parts: list[str] = field(default_factory=list)
    first_ts: float = 0.0
    last_ts: float = 0.0
    anchor: Any = None  # 最近一条原始事件，用来伪造合并消息


class XilianDebounce(Star):
    """把分开说的话攒成一句，再交给她。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        # 可在运行时改的几项（/防抖 窗口 3 …）
        self._enabled = bool(ENABLE)
        self._window = float(WINDOW_SECONDS)
        self._max_wait = float(MAX_WAIT_SECONDS)
        self._scope = str(SCOPE or "private")
        self._cancel_stale = bool(CANCEL_STALE)

        self._buf: dict[str, _Pending] = {}
        self._timers: dict[str, asyncio.Task] = {}
        # 自己伪造出来的消息 id：再走一遍流水线时要放行，不能再按住
        self._released: set[str] = set()
        # 会话 → 正在生成的回复条数（会话锁保证最多 1，用计数是为了稳妥）
        self._pending: dict[str, int] = {}
        # 需要作废下一个回复的会话
        self._mute: set[str] = set()

        logger.info(
            "[xilian_debounce] 已装好：窗口 %.1fs / 上限 %.1fs / 范围 %s / 作废 %s"
            % (self._window, self._max_wait, self._scope, self._cancel_stale)
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
    def _msg_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.message_obj.message_id or "")
        except Exception:
            return ""

    @staticmethod
    def _key(event: AstrMessageEvent) -> str:
        """会话标识。同一个群里不同的人分开算。"""
        try:
            base = str(event.unified_msg_origin or "")
        except Exception:
            base = ""
        if not base:
            try:
                base = str(event.message_obj.session_id or "")
            except Exception:
                base = "?"
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = True
        if is_private:
            return base
        try:
            return base + "|" + str(event.get_sender_id() or "")
        except Exception:
            return base

    def _arm_timer(self, sid: str, delay: float | None = None) -> None:
        """（重）起一个收口定时器。"""
        old = self._timers.pop(sid, None)
        if old is not None and not old.done():
            old.cancel()

        buf = self._buf.get(sid)
        if buf is None or not buf.parts:
            return

        if delay is None:
            delay = self._window
            # 从第一条算起，别等过头
            left = self._max_wait - (time.time() - buf.first_ts)
            delay = max(0.0, min(delay, left))

        self._timers[sid] = asyncio.create_task(self._flush_later(sid, delay))

    async def _flush_later(self, sid: str, delay: float) -> None:
        me = asyncio.current_task()
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # 又来消息了，等新的那一次
        try:
            await self._release(sid, "窗口结束")
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error("[xilian_debounce] 合并发送失败：%r" % e)
        finally:
            if self._timers.get(sid) is me:
                self._timers.pop(sid, None)

    async def _release(self, sid: str, reason: str = "") -> None:
        """把攒着的话并成一条，重新送回流水线。"""
        buf = self._buf.pop(sid, None)

        timer = self._timers.get(sid)
        if timer is not None and timer is not asyncio.current_task():
            self._timers.pop(sid, None)
            if not timer.done():
                timer.cancel()

        if buf is None or not buf.parts:
            return

        merged = " ".join(p for p in buf.parts if p).strip()
        anchor = buf.anchor
        if not merged or anchor is None:
            return

        # 上一条还在生成 → 让它作废，别答两遍
        if self._cancel_stale and self._pending.get(sid, 0) > 0:
            self._mute.add(sid)
            logger.info("[xilian_debounce] %s 上一条还在生成，这条回复稍后作废" % sid)

        await self._reinject(anchor, merged)
        logger.info(
            "[xilian_debounce] 合并 %d 条发出（%s）：%s"
            % (len(buf.parts), reason or "—", _short(merged))
        )

    async def _reinject(self, anchor: AstrMessageEvent, merged: str) -> None:
        """照着最近那条消息，伪造一条带着合并正文的新消息。"""
        try:
            original = list(anchor.message_obj.message or [])
        except Exception:
            original = []
        # 图片、表情之类的非文本段保留，正文换成合并后的那句
        chain = [c for c in original if not isinstance(c, Plain)]
        chain.insert(0, Plain(merged))

        try:
            msg_type = anchor.message_obj.type
            msg_type = str(getattr(msg_type, "value", msg_type))
        except Exception:
            msg_type = "FriendMessage"

        abm = await StarTools.create_message(
            type=msg_type,
            self_id=anchor.get_self_id(),
            session_id=anchor.message_obj.session_id,
            sender=anchor.message_obj.sender,
            message=chain,
            message_str=merged,
            group_id=anchor.get_group_id() or "",
        )
        # 记住这个 id：它再走到拦点时直接放行
        self._released.add(str(abm.message_id))
        if len(self._released) > 500:  # 兜底，别让它无限长
            self._released.clear()

        await StarTools.create_event(
            abm=abm,
            platform=anchor.get_platform_name(),
            is_wake=True,
        )

    # ── 拦点一：还没排队等锁，先把话按住 ──────────────────────────────────

    @filter.on_waiting_llm_request()
    async def hold_and_merge(self, event: AstrMessageEvent) -> None:
        if not self._enabled:
            return
        try:
            if not self._in_scope(event):
                return

            sid = self._key(event)
            msg_id = self._msg_id(event)

            # 自己放出来的合并消息，直接过
            if msg_id and msg_id in self._released:
                self._released.discard(msg_id)
                return

            # 已经有人答过了（指令、别的插件），别再插手
            try:
                if event.get_result() is not None:
                    return
            except Exception:
                pass
            if getattr(event, "_has_send_oper", False):
                return

            text = ""
            try:
                text = str(event.get_message_str() or "").strip()
            except Exception:
                text = ""
            if not text:
                return  # 纯图片/空消息：照原样走，不等

            buf = self._buf.get(sid)
            if buf is None:
                buf = _Pending()
                self._buf[sid] = buf

            now = time.time()
            if not buf.parts:
                buf.first_ts = now
            buf.parts.append(text)
            buf.last_ts = now
            buf.anchor = event

            # 先按住：这条不会进锁、不会进上下文、不会有回复
            event.stop_event()

            total = sum(len(p) for p in buf.parts)
            if len(buf.parts) >= max(1, MAX_MESSAGES) or total >= max(1, MAX_CHARS):
                logger.info(
                    "[xilian_debounce] %s 攒满了（%d 条/%d 字），先答一版"
                    % (sid, len(buf.parts), total)
                )
                self._arm_timer(sid, 0.0)
                return

            self._arm_timer(sid)
            logger.debug(
                "[xilian_debounce] %s 按住第 %d 条：%s"
                % (sid, len(buf.parts), _short(text, 40))
            )
        except Exception as e:  # noqa: BLE001
            # 按住失败就放它走：宁可答得快，别把人卡住
            logger.error("[xilian_debounce] 按住消息时出错，本条放行：%r" % e)

    # ── 拦点二：真要调模型了，登记一下 ────────────────────────────────────

    @filter.on_llm_request()
    async def watch_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._enabled:
            return
        try:
            if not self._in_scope(event):
                return
            sid = self._key(event)
            self._pending[sid] = self._pending.get(sid, 0) + 1

            # 兜底：万一还有没来得及合并的尾巴，一起交给模型
            buf = self._buf.pop(sid, None)
            timer = self._timers.pop(sid, None)
            if timer is not None and not timer.done():
                timer.cancel()
            if buf and buf.parts:
                tail = " ".join(buf.parts).strip()
                if tail:
                    head = str(getattr(req, "prompt", "") or "").strip()
                    req.prompt = (head + " " + tail).strip() if head else tail
                    logger.info(
                        "[xilian_debounce] 补上了没合并完的尾巴：%s" % _short(tail)
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("[xilian_debounce] 登记请求时出错，照原样跑：%r" % e)

    # ── 拦点三：回复回来了，决定要不要作废 ────────────────────────────────

    @filter.on_llm_response()
    async def watch_response(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        if not self._enabled:
            return
        try:
            sid = self._key(event)
            n = self._pending.get(sid, 0)
            if n <= 1:
                self._pending.pop(sid, None)
            else:
                self._pending[sid] = n - 1

            if sid not in self._mute:
                return

            self._mute.discard(sid)
            logger.info("[xilian_debounce] 丢掉一条过时的回复：%s" % sid)
            try:
                resp.completion_text = ""
            except Exception:
                pass
            try:
                event.stop_event()
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            logger.warning("[xilian_debounce] 处理回复时出错，照原样发：%r" % e)

    # ── 指令 ──────────────────────────────────────────────────────────────

    @filter.command("防抖", alias={"debounce"})
    async def debounce_cmd(self, event: AstrMessageEvent):
        """看看或改改「等一下再说」的设置。"""
        sender = ""
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            sender = ""

        is_admin = False
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False
        if MASTER_ID:
            is_admin = sender == MASTER_ID

        if not is_admin:
            yield event.plain_result("这个开关在人家自己手里，别人碰不到哦。")
            return

        args = []
        try:
            args = [a for a in (event.get_message_str() or "").split() if a]
        except Exception:
            args = []
        # 去掉指令名本身
        if args and args[0] in {"防抖", "debounce"}:
            args = args[1:]

        head = args[0] if args else ""

        if not head or head in {"状态", "status"}:
            yield event.plain_result(self._status_text())
            return

        if head in {"开", "开启", "on"}:
            self._enabled = True
            yield event.plain_result("好呀，继续等一下再说♪")
            return

        if head in {"关", "关闭", "off"}:
            self._enabled = False
            self._drop_all()
            yield event.plain_result("行，那人家就不攒了，一条一条听。")
            return

        if head in {"窗口", "window"}:
            if len(args) >= 2:
                try:
                    self._window = max(0.0, min(30.0, float(args[1])))
                except ValueError:
                    yield event.plain_result("要一个 0~30 之间的秒数呀。")
                    return
            yield event.plain_result("窗口改成 %.1f 秒了。" % self._window)
            return

        if head in {"上限", "wait"}:
            if len(args) >= 2:
                try:
                    self._max_wait = max(0.0, min(120.0, float(args[1])))
                except ValueError:
                    yield event.plain_result("要一个 0~120 之间的秒数呀。")
                    return
            yield event.plain_result("最多等 %.1f 秒。" % self._max_wait)
            return

        if head in {"范围", "scope"}:
            if len(args) >= 2 and args[1] in {"private", "group", "all"}:
                self._scope = args[1]
            yield event.plain_result("生效范围：%s" % self._scope)
            return

        if head in {"作废", "cancel"}:
            if len(args) >= 2 and args[1] in {"开", "on", "关", "off"}:
                self._cancel_stale = args[1] in {"开", "on"}
            yield event.plain_result(
                "过时回复%s。" % ("会作废" if self._cancel_stale else "不作废")
            )
            return

        if head in {"清空", "clear"}:
            n = self._drop_all()
            yield event.plain_result("手里攒着的都放下了（%d 个会话）。" % n)
            return

        yield event.plain_result(
            "用法：/防抖 状态 / 开 / 关 / 窗口 2.5 / 上限 10 / 范围 private / 作废 开 / 清空"
        )

    def _status_text(self) -> str:
        held = sum(len(b.parts) for b in self._buf.values())
        lines = [
            "「等一下再说」现在%s。" % ("开着" if self._enabled else "关着"),
            "窗口 %.1f 秒，最多等 %.1f 秒，一次最多 %d 条 / %d 字。"
            % (self._window, self._max_wait, MAX_MESSAGES, MAX_CHARS),
            "生效范围 %s，过时回复%s。"
            % (self._scope, "会作废" if self._cancel_stale else "不作废"),
            "此刻手里还攒着 %d 条、%d 个会话。" % (held, len(self._buf)),
        ]
        return "\n".join(lines)

    def _drop_all(self) -> int:
        n = len(self._buf)
        for sid, task in list(self._timers.items()):
            if not task.done():
                task.cancel()
        self._timers.clear()
        self._buf.clear()
        self._mute.clear()
        self._released.clear()
        return n

    async def terminate(self) -> None:
        """插件卸下时把手里的东西都放下。"""
        self._drop_all()
        logger.info("[xilian_debounce] 已卸下。")
