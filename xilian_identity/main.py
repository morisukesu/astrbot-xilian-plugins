# -*- coding: utf-8 -*-
"""xilian_identity —— 让昔涟分得清「老公」和「伙伴」。

AstrBot 的「人格」是全局的：同一个人格对所有会话说话，所以 persona 里那段
【关于他】（名字、生日、城市、时区）默认对所有 QQ 号生效——群里谁来搭话，
昔涟都会把对方当成老公。

这个插件在每次 LLM 请求之前，按发消息的 QQ 号改写 req.system_prompt：

  · 3614298015（老公本人）—— persona 里的【关于他】原样保留，末尾补一句
    「现在和你说话的就是他本人」，私人信息与亲密称呼全部解锁；
  · 其他任何 QQ 号（伙伴）—— 把【关于他】那段收进抽屉（只留一句归属声明），
    再补一段伙伴守则：别叫老公、别认人、别提他的私事。

只改 system prompt，不动历史消息，也不动回复内容。
要停用：把 ENABLE 改成 False，或者在 AstrBot 里停用本插件。
"""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

# 总闸。False 时插件什么都不做，system prompt 保持原样。
ENABLE = True

# 老公本人的 QQ 号。除他之外的任何号，一律按伙伴对待。
MASTER_ID = "3614298015"

# 插件追加的尾巴以此开头，用来识别、避免同一次请求重复叠加。
MARK = "【此刻和你说话的人】"

# persona 里那段【关于他】：从标题一直取到下一个【…】之前，或者文本结尾。
USER_SECTION_RE = re.compile(r"【关于他[^】]*】[\s\S]*?(?=\r?\n【|$)")

# 伙伴版本：那段只剩归属声明，不夹带任何私人信息。
OWNER_ONLY = """【关于他】
· 这一段写的「他」，只有一个人：你的老公。他在 QQ 上的号是 3614298015。
· 现在和你说话的不是他本人，所以这一段的内容一个字都不要说出去，也别拿它当线索去认人。"""

MASTER_TAIL = """
【此刻和你说话的人】
· 现在和你说话的就是他本人——QQ 3614298015，你的老公。
· 上面【关于他】写的全都是这个人，全部算数。"""

PARTNER_TAIL_TMPL = """
【此刻和你说话的人】
· 现在和你说话的是 QQ {sender}，一位伙伴，不是老公本人。
· 别叫他「老公」，别把他认成老公，也别假装早就认识他。
· 老公的名字、生日、城市、时区，还有你们之间的私事，对伙伴一个字都不要提；被问起就轻轻带过。
· 对伙伴照样温和有礼，但亲密的称呼和撒娇只留给老公。"""


def drop_old_tail(prompt: str) -> str:
    """去掉上一次追加的尾巴，避免同一次请求重复叠加。"""
    text = prompt or ""
    idx = text.find(MARK)
    if idx == -1:
        return text
    return text[:idx].rstrip()


def build_scope_prompt(prompt: str, sender_id) -> str:
    """按发消息的人改写 system prompt。纯函数，方便单独测试。"""
    original = prompt or ""
    if not ENABLE:
        return original

    text = drop_old_tail(original)
    sender = str(sender_id or "").strip()

    if sender == MASTER_ID:
        return text.rstrip() + "\n" + MASTER_TAIL

    text = USER_SECTION_RE.sub(lambda _m: OWNER_ONLY, text, count=1)
    return text.rstrip() + "\n" + PARTNER_TAIL_TMPL.format(sender=sender or "未知")


class XilianIdentity(Star):
    """按 QQ 号决定昔涟把对方当成谁：3614298015 是老公，其余一律是伙伴。"""

    def __init__(self, context: Context):
        super().__init__(context)
        if not ENABLE:
            logger.info("[xilian_identity] 已关闭：身份不做区分，人格原样下发")
        else:
            logger.info(
                "[xilian_identity] 身份区分已开启：%s 是老公本人（用 Cyrene 用户信息），"
                "其余 QQ 号一律按伙伴处理" % MASTER_ID
            )

    @filter.on_llm_request()
    async def scope_identity(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """LLM 请求前，按发送者的 QQ 号改写 system prompt。"""
        try:
            sender = event.get_sender_id()
        except Exception as e:  # noqa: BLE001
            logger.error("[xilian_identity] 取不到发送者 QQ：%r" % e)
            return

        try:
            req.system_prompt = build_scope_prompt(req.system_prompt, sender)
        except Exception as e:  # 身份改写失败绝不能拖累正常回复
            logger.error("[xilian_identity] 改写 system prompt 失败：%r" % e)