# -*- coding: utf-8 -*-
"""xilian_tts —— 让昔涟在 QQ 等平台上用 Cyrene 的音色说话。

语音不走 AstrBot 自带的那几种 TTS，而是直接复用 Cyrene 桌面端正在用的那套设置：
    Cyrene 设置里的 ttsEngine = custom-cloud
    →  本机桥接服务 POST http://127.0.0.1:8791/tts
    →  {"text": ..., "voiceId": "vc-VisW54vqfp2ruPMq7YvXPK",
        "speed": 1, "volume": 1, "pitch": 0, "emotion": "happy"}
    ←  {"audioBase64": "<base64 mp3>", "format": "mp3"}

插件做四件事：
1. 往 AstrBot 注册一个 TTS provider（类型名 xilian_tts），
   于是后台「TTS 设置」里能选到它，昔涟的回复可以自动转成语音；
2. 念之前先看这句话是什么语气，再按语气调语速 / 音高 / 音量 / 情绪，
   让语音跟着内容有起伏，而不是一个调子念到底；
3. 提供「分段语气合成」：给一段一段的文字，逐段判语气、逐段合成，
   再拼回一条完整的语音 —— 文字是一句一句发的，声音却是一个人在说，
   她说到哪句换了口气，那条语音里也听得出来；
4. 提供 /语音 指令，随时手动试听，也能直接指定语气，不用等模型回话。

分段合成是这么省下来的：
    桥每次合成约 1 秒，把十几段八字的碎片逐段送过去，会等到天荒地老。
    所以相邻的同语气段先并成一次合成（plan_tone_units），
    一条回复通常只剩 2～4 次调用 —— 只有语气真的换了，才会换调子。
    拼接口优先交给 ffmpeg（无损直拼），没有 ffmpeg 就退回字节拼接。

音色、语速、桥接地址都在 provider 配置里（cmd_config.json → provider → xilian_tts），
改完重启 AstrBot 即可。

改动记录：2026-09-18 新增 get_audio_segmented（分段语气合成 + 拼接），
老接口 get_audio 保持不变，单独念一句仍然走它。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import TTSProvider
from astrbot.core.provider.register import provider_cls_map, register_provider_adapter
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

PROVIDER_TYPE = "xilian_tts"
PROVIDER_DESC = "昔涟 TTS（复用 Cyrene 的本地语音桥）"
DEFAULT_ENDPOINT = "http://127.0.0.1:8791/tts"
DEFAULT_VOICE = "vc-VisW54vqfp2ruPMq7YvXPK"
DEFAULT_MODEL = "sensenova-tts-2.0"
TEMP_SUBDIR = "xilian_tts"

# 一次合成单元最多多少字。同语气的段落会并在一起，但也不能无限并下去。
MAX_UNIT_CHARS = 400

# ffmpeg 不在 PATH 里时，去这些地方再找找。找不到就退回字节拼接，功能不受影响。
FFMPEG_HINTS = (
    r"D:\Astrbot\_tool\ffmpeg\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
)

# 念不出来的装饰性符号，念出来只会变成噪音
_NOISE_RE = re.compile(r"[♪♬♩♡❤️✨～]+")
# markdown / 消息格式标记
_MARKUP_RE = re.compile(r"(\*\*|__|~~|`{1,3}|^#{1,6}\s*|^\s*[-*+]\s+)", re.MULTILINE)


def clean_text(text: str) -> str:
    """把要念的文本收拾干净：去掉装饰符号和 markdown 标记，压缩空白。"""
    s = (text or "").strip()
    if not s:
        return ""
    s = _NOISE_RE.sub("", s)
    s = _MARKUP_RE.sub("", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value, default: bool) -> bool:
    """配置里可能是真的布尔，也可能是 "true" / "false" / "on" 这种字符串。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "开", "开启"}:
        return True
    if text in {"0", "false", "no", "off", "关", "关闭"}:
        return False
    return default


# ── 语气 ──────────────────────────────────────────────────────────────────
#
# 一句话的情绪，决定了它该怎么念。这里先按标点和用词把语气分档，
# 再把档位换算成 TTS 能听懂的数字。
#
# emotion 用 MiniMax 协议族的取值（SenseAudio 同族）；
# pitch 的合法范围是 -12 ~ +12，这里只取中间一小段，免得听出电音味。
#
# 2026-09-18 记一笔：曾把各档铺开到 ±9、起跳强度提到 0.70，群里听着发尖、
# 有金属感（原话「锐化」），当天 09:35 全部收回原本的窄幅。这版音色对 pitch
# 很敏感，±3 以上就得逐档试听，别再一次性推那么远。


@dataclass(frozen=True)
class Tone:
    """一种语气对应的一组参数。倍率都是相对 provider 里基准值的偏移。"""

    key: str
    label: str
    emotion: str | None
    speed_mul: float
    pitch: int
    vol_mul: float


TONES: dict[str, Tone] = {
    t.key: t
    for t in (
        Tone("calm", "平静", None, 1.00, 0, 1.00),
        Tone("cheer", "开心", "happy", 1.07, 2, 1.05),
        Tone("gentle", "温柔", "neutral", 0.95, 1, 0.94),
        Tone("coax", "撒娇", "coquetry", 0.93, 3, 0.96),
        Tone("shy", "害羞", "shyness", 0.90, 1, 0.85),
        Tone("low", "低落", "sad", 0.92, -2, 0.90),
        Tone("surprise", "惊讶", "surprised", 1.10, 3, 1.05),
        Tone("annoyed", "生气", "angry", 1.04, 1, 1.05),
        Tone("tsundere", "傲娇", "tsundere", 1.03, 2, 1.00),
    )
}

# 手打的时候往往会直接说语气名，给它一套别名。
TONE_ALIASES: dict[str, str] = {
    "平静": "calm", "正常": "calm", "calm": "calm", "neutral": "calm",
    "开心": "cheer", "高兴": "cheer", "欢快": "cheer", "cheer": "cheer", "happy": "cheer",
    "温柔": "gentle", "轻声": "gentle", "gentle": "gentle",
    "撒娇": "coax", "撒个娇": "coax", "娇": "coax", "coax": "coax", "coquetry": "coax",
    "害羞": "shy", "害臊": "shy", "羞涩": "shy", "shy": "shy", "shyness": "shy",
    "低落": "low", "难过": "low", "失落": "low", "low": "low", "sad": "low",
    "惊讶": "surprise", "吃惊": "surprise", "surprise": "surprise",
    "生气": "annoyed", "嗔怪": "annoyed", "annoyed": "annoyed", "angry": "annoyed",
    "傲娇": "tsundere", "娇嗔": "tsundere", "嘴硬": "tsundere", "tsundere": "tsundere",
}

# 从上往下匹配，先命中的先赢。
# 顺序是有讲究的：
#   · 惊讶、傲娇、生气、低落带着明确的情绪词，比标点更该说了算，所以排前面；
#   · 傲娇排在生气前面：「讨厌啦」「才不要」本来也会被生气那条命中，
#     但它们更像嘴硬，先归傲娇；
#   · 害羞、撒娇的信号比温柔更具体，排在温柔前面，免得被「……」这类宽泛规则抢走；
#   · 「关心、叮嘱」在温柔里算最具体的，收进温柔，免得被标点抢走。
_TONE_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("surprise", re.compile(r"咦|诶|欸|哎呀|竟然|居然|真的假的|不会吧|[?？][!！]|[!！][?？]")),
    ("tsundere", re.compile(r"才不要|才不是|才不理|讨厌啦|讨厌嘛|谁稀罕|少来|别乱说|哼[，,]?才")),
    ("annoyed", re.compile(r"哼+|讨厌|过分|气死|不理你|坏蛋|别闹")),
    ("low", re.compile(r"难过|伤心|对不起|抱歉|呜+|舍不得|寂寞|失落|想哭|没关系的")),
    ("shy", re.compile(r"害羞|害臊|脸红|羞涩|羞答答|怕羞|捂脸|捂着脸|////|别看人家")),
    ("coax", re.compile(r"撒娇|人家嘛|不嘛|好嘛|嘛[~～]|好不好嘛|求求你|拜托啦|人家想要|抱抱|哄哄人家")),
    (
        "gentle",
        re.compile(
            r"不好意思|人家才|悄悄|轻轻|…{2,}"
            r"|早点睡|早点休息|好好休息|多喝热水|多穿点|记得吃|要好好"
            r"|别忘了|注意身体|小心点|别熬夜|按时吃饭"
        ),
    ),
)

# 开心要有实打实的开心词。只有「！」「♪」这类标点，只能算一丝上扬。
_CHEER_WORDS = re.compile(r"哈哈|嘿嘿|嘻嘻|开心|太好了|好耶|喜欢|真棒|耶|哇")
_CHEER_PUNCT = re.compile(r"[!！♪]")

# 波浪线不算兴奋。它是软化、拉长，关心句和撒娇尾巴都在用，
# 所以判成「温柔」，而不是「开心」——「早上好呀，老公～」不该是雀跃的调子。
_SOFT_PUNCT = re.compile(r"[~～]")


def detect_tone(text: str) -> tuple[str, float]:
    """看这句话是什么语气，顺带估个强度（0~1）。

    命中得越多，说明情绪越浓，强度越高；一句平铺直叙的话就是「平静」。
    """
    raw = text or ""
    for key, pattern in _TONE_RULES:
        hits = len(pattern.findall(raw))
        if hits:
            return key, min(1.0, 0.45 + 0.2 * hits)

    # 开心词排在波浪线前面，免得「太好了～」被波浪线抢成温柔。
    words = len(_CHEER_WORDS.findall(raw))
    if words:
        return "cheer", min(1.0, 0.45 + 0.2 * words)
    if _SOFT_PUNCT.search(raw):
        return "gentle", 0.65
    if _CHEER_PUNCT.search(raw):
        return "cheer", 0.65
    return "calm", 0.0


def tone_params(
    tone_key: str,
    strength: float,
    base_speed: float,
    base_volume: float,
    master: float = 1.0,
) -> tuple[float, float, int, str | None]:
    """把语气换算成 (speed, volume, pitch, emotion)。

    strength 是这句话的情绪浓度，master 是用户想整体收放多少（0~1）。
    两者都为 0 时，结果就是基准值本身——等于什么都没加。
    """
    tone = TONES.get(tone_key) or TONES["calm"]
    factor = max(0.0, min(1.0, float(strength))) * max(0.0, min(1.0, float(master)))

    speed = base_speed * (1.0 + (tone.speed_mul - 1.0) * factor)
    volume = base_volume * (1.0 + (tone.vol_mul - 1.0) * factor)
    pitch = int(round(tone.pitch * factor))

    speed = max(0.5, min(2.0, speed))
    volume = max(0.1, min(3.0, volume))
    pitch = max(-12, min(12, pitch))
    emotion = tone.emotion if factor > 0 else None
    return speed, volume, pitch, emotion


# ── 分段语气规划 ──────────────────────────────────────────────────────────
#
# 文字那边是一句一句发的（8 字左右一段），语音这边要合成「一条完整的」。
# 两件事怎么接上：拿到的每段文字各判一次语气，语气没变的地方并成一次合成，
# 语气变了才换一组参数 —— 于是那条语音里，她说到哪句换了口气，听得出来。

_SENT_END = "。！？!?…♪♬~～"
_CLAUSE_END = "，；："


def split_sentences(text: str, max_len: int = 24) -> list[str]:
    """把整段文字切成小句。

    只给「没有分段信息」的场合用（例如 /语音 整条 试听）：按标点切，
    没有标点又特别长的句子再按长度切开。分段合成真正的边界由调用方给。
    """
    out: list[str] = []
    buf = ""
    for ch in text or "":
        buf += ch
        if ch in _SENT_END:
            out.append(buf)
            buf = ""
        elif ch in _CLAUSE_END and len(buf.strip()) >= 6:
            out.append(buf)
            buf = ""

    if buf.strip():
        out.append(buf)

    final: list[str] = []
    for block in out:
        rest = block
        while len(rest) > max_len:
            final.append(rest[:max_len])
            rest = rest[max_len:]
        if rest.strip():
            final.append(rest)
    return [s for s in final if s.strip()]


def plan_tone_units(
    segments: Sequence[str],
    merge: bool = True,
    max_chars: int = MAX_UNIT_CHARS,
) -> list[tuple[str, str, float]]:
    """把分段文字整理成「合成单元」，返回 (文本, 语气, 强度)。

    相邻且语气相同的段会并成一次合成 —— 桥每次调用约 1 秒，
    一条回复切成十几段逐段去念，等到天荒地老也念不完。
    并到 max_chars 就断开，免得单次请求太长。
    """
    units: list[list] = []
    for raw in segments:
        spoken = clean_text(raw)
        if not spoken:
            continue
        key, strength = detect_tone(raw)
        if (
            merge
            and units
            and units[-1][1] == key
            and len(units[-1][0]) + len(spoken) <= max_chars
        ):
            units[-1][0] = units[-1][0] + spoken
            units[-1][2] = max(units[-1][2], strength)
        else:
            units.append([spoken, key, strength])
    return [(text, key, strength) for text, key, strength in units]


def describe_units(units: Sequence[tuple[str, str, float]]) -> str:
    """把合成单元列成一张能看的表（/语音 分段 用）。"""
    lines = []
    for i, (text, key, strength) in enumerate(units, 1):
        tone = TONES.get(key, TONES["calm"])
        flat = " ".join(text.split())
        if len(flat) > 32:
            flat = flat[:32] + "…"
        lines.append(
            "%d. [%s 强度%.2f] %d字 %s"
            % (i, tone.label, strength, len(text), flat)
        )
    return "\n".join(lines)


# ── 音频拼接 ──────────────────────────────────────────────────────────────


def find_ffmpeg() -> str | None:
    """找 ffmpeg。PATH 里没有就去几个常见位置碰碰运气。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for hint in FFMPEG_HINTS:
        if os.path.isfile(hint):
            return hint
    return None


def find_ffprobe() -> str | None:
    """找 ffprobe，找不到就从 ffmpeg 的路径旁边推。"""
    found = shutil.which("ffprobe")
    if found:
        return found
    exe = find_ffmpeg()
    if exe:
        guess = os.path.join(os.path.dirname(exe), "ffprobe.exe")
        if os.path.isfile(guess):
            return guess
    return None


def strip_id3(blob: bytes) -> bytes:
    """去掉 ID3v2 头与 ID3v1 尾。

    把几段 mp3 直接接起来时，中间那些 ID3 头会变成一段杂音；去掉更干净。
    只认标准写法，读不懂就原样返回。
    """
    if not blob:
        return blob
    start = 0
    if blob[:3] == b"ID3" and len(blob) >= 10:
        size = 0
        for b in blob[6:10]:
            size = (size << 7) | (b & 0x7F)
        start = 10 + size
        # start == len(blob) 是正常的（头后面就是全部内容）；只有超出才算坏头。
        if start > len(blob):
            start = 0
    end = len(blob)
    if end > 128 and blob[end - 128 : end - 125] == b"TAG":
        end -= 128
    return blob[start:end]


def concat_mp3_raw(blobs: Sequence[bytes]) -> bytes:
    """没有 ffmpeg 时的兜底：把 mp3 帧直接接起来。

    播放器基本都认，只是时长元信息可能不准。宁可这样，也不要因为
    少了 ffmpeg 就发不出语音。
    """
    out = bytearray()
    for i, blob in enumerate(blobs):
        out += blob if i == 0 else strip_id3(blob)
    return bytes(out)


async def _run_ffmpeg(exe: str, args: Sequence[str], timeout: float = 60.0) -> bool:
    """跑一次 ffmpeg，安静地返回成功与否。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[xilian_tts] 起不了 ffmpeg：%r", e)
        return False

    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib_suppress():
            proc.kill()
        logger.warning("[xilian_tts] ffmpeg 超时（%.0fs）", timeout)
        return False

    if proc.returncode != 0:
        logger.warning(
            "[xilian_tts] ffmpeg 退出码 %s：%s",
            proc.returncode,
            (err or b"")[:200],
        )
        return False
    return True


class contextlib_suppress:
    """只为包住 proc.kill() 的小上下文管理器，省得再 import contextlib。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


async def probe_duration(path: str) -> float | None:
    """读音频时长（秒）。读不到就返回 None，不打扰主流程。"""
    exe = find_ffprobe()
    if not exe:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=20)
    except Exception:  # noqa: BLE001
        return None
    try:
        return float((out or b"").decode("utf-8", "replace").strip())
    except ValueError:
        return None


def _write_bytes(path: str, blob: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(blob)


class XilianTTSProvider(TTSProvider):
    """把 Cyrene 的本地 TTS 桥包装成 AstrBot 的 TTS Provider。"""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.endpoint = (provider_config.get("api_base") or "").strip() or DEFAULT_ENDPOINT
        self.voice = (provider_config.get("voice") or "").strip() or DEFAULT_VOICE
        self.speed = _to_float(provider_config.get("speed"), 1.0)
        self.volume = _to_float(provider_config.get("volume"), 1.0)
        self.timeout = _to_float(provider_config.get("timeout"), 60.0)
        # 语气总开关，以及「整体收放多少」。默认开着、全量。
        self.tone_enabled = _to_bool(provider_config.get("tone_enabled"), True)
        self.tone_strength = _to_float(provider_config.get("tone_strength"), 1.0)
        # 分段合成：同语气是否并成一次调用、单次最多多少字。
        self.seg_merge = _to_bool(provider_config.get("seg_merge"), True)
        self.max_unit_chars = int(
            _to_float(provider_config.get("max_unit_chars"), float(MAX_UNIT_CHARS))
            or MAX_UNIT_CHARS
        )
        self.set_model(provider_config.get("model") or DEFAULT_MODEL)

        self._out_dir = os.path.join(get_astrbot_temp_path(), TEMP_SUBDIR)
        os.makedirs(self._out_dir, exist_ok=True)
        self._client: httpx.AsyncClient | None = None

    # --- 内部工具 ---------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self._client

    async def health(self) -> tuple[bool, str]:
        """探一下桥接服务在不在。返回 (是否可用, 说明)。"""
        base = self.endpoint.rsplit("/", 1)[0]
        try:
            resp = await self._get_client().get(base + "/health")
            if resp.status_code == 200:
                return True, "桥接在线"
            return False, f"桥接返回 HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            return False, f"连不上 {base}：{type(e).__name__}: {e}"

    def resolve_tone(self, text: str, tone: str | None = None) -> tuple[str, float]:
        """定下这句话用哪种语气。手动指定的优先级最高。"""
        if not self.tone_enabled:
            return "calm", 0.0
        if tone:
            return tone, 1.0
        return detect_tone(text)

    def _new_path(self, fmt: str, tag: str) -> str:
        return os.path.join(self._out_dir, f"{tag}_{int(time.time() * 1000)}.{fmt}")

    async def _synthesize_blob(
        self, spoken: str, tone_key: str, strength: float
    ) -> tuple[bytes, str]:
        """合成一段，返回 (音频字节, 格式)。"""

        speed, volume, pitch, emotion = tone_params(
            tone_key,
            strength,
            self.speed,
            self.volume,
            self.tone_strength,
        )

        payload: dict = {
            "text": spoken,
            "voiceId": self.voice,
            "speed": round(speed, 3),
            "volume": round(volume, 3),
            "pitch": pitch,
        }
        # 平静时不塞 emotion，交给上游自己发挥——这也是改动前的样子。
        if emotion:
            payload["emotion"] = emotion

        resp = await self._get_client().post(self.endpoint, json=payload)
        resp.raise_for_status()

        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"桥接返回的不是 JSON：{resp.text[:200]}") from e

        audio_b64 = data.get("audioBase64") or ""
        if not audio_b64:
            raise RuntimeError(f"桥接没有返回 audioBase64：{str(data)[:200]}")

        blob = base64.b64decode(audio_b64)
        if not blob:
            raise RuntimeError("桥接返回了空音频")

        fmt = str(data.get("format") or "mp3").strip().lower() or "mp3"
        logger.debug(
            "[xilian_tts] 一段合成完成：%d字 / %d字节（语气 %s，强度 %.2f，speed %.3f，pitch %d，emotion %s）",
            len(spoken),
            len(blob),
            TONES.get(tone_key, TONES["calm"]).label,
            strength,
            speed,
            pitch,
            emotion or "—",
        )
        return blob, fmt

    async def _concat_mp3(self, blobs: Sequence[bytes], out_path: str, fmt: str) -> str:
        """把几段音频接成一个文件。ffmpeg 优先，接不了就直接拼字节。"""
        if len(blobs) == 1:
            _write_bytes(out_path, blobs[0])
            return out_path

        if fmt != "mp3":
            # 非 mp3（wav 之类）交给 ffmpeg；它也不行就原样拼，至少不丢内容。
            if await self._concat_with_ffmpeg(blobs, out_path):
                return out_path
            _write_bytes(out_path, b"".join(blobs))
            return out_path

        if await self._concat_with_ffmpeg(blobs, out_path):
            return out_path

        _write_bytes(out_path, concat_mp3_raw(blobs))
        logger.warning("[xilian_tts] ffmpeg 没能拼接，已改用直接拼字节（时长显示可能不准）")
        return out_path

    async def _concat_with_ffmpeg(self, blobs: Sequence[bytes], out_path: str) -> bool:
        """用 concat 分离器拼接。先试无损直拼，不行再重编码。"""
        exe = find_ffmpeg()
        if not exe:
            return False

        part_dir = os.path.join(self._out_dir, f"_parts_{int(time.time() * 1000)}")
        try:
            os.makedirs(part_dir, exist_ok=True)
            paths = []
            for i, blob in enumerate(blobs):
                p = os.path.join(part_dir, f"p{i:03d}.mp3")
                _write_bytes(p, blob)
                paths.append(p)

            list_file = os.path.join(part_dir, "list.txt")
            with open(list_file, "w", encoding="utf-8") as f:
                for p in paths:
                    f.write("file '%s'\n" % p.replace("\\", "/"))

            base = [
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file,
            ]

            if await _run_ffmpeg(exe, base + ["-c", "copy", out_path]):
                if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                    return True

            # 参数不一致时 -c copy 会失败，重编码一次就稳了。
            if await _run_ffmpeg(
                exe, base + ["-c:a", "libmp3lame", "-b:a", "128k", out_path]
            ):
                if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                    logger.info("[xilian_tts] 直拼不成，已改用重编码拼接")
                    return True
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("[xilian_tts] 拼接时出错：%r", e)
            return False
        finally:
            shutil.rmtree(part_dir, ignore_errors=True)

    # --- AstrBot 需要的接口 -----------------------------------------------

    async def get_audio(self, text: str, tone: str | None = None) -> str:
        """合成一段话。tone 传语气 key 时按指定语气念，不传就自己判断。"""
        spoken = clean_text(text)
        if not spoken:
            raise ValueError("这条没有能念出来的内容")

        tone_key, strength = self.resolve_tone(text, tone)
        blob, fmt = await self._synthesize_blob(spoken, tone_key, strength)

        path = self._new_path(fmt, "xilian_tts")
        _write_bytes(path, blob)
        logger.info(
            "[xilian_tts] 合成完成 %s 字节 → %s（语气 %s，强度 %.2f）",
            len(blob),
            path,
            TONES.get(tone_key, TONES["calm"]).label,
            strength,
        )
        return path

    async def get_audio_segmented(
        self,
        segments: Sequence[str],
        tone: str | None = None,
        merge: bool | None = None,
    ) -> str:
        """把「一句一句」的文字合成成**一条**语音，句子之间的语气变化保留下来。

        segments 是已经切好的分段（谁切的不重要，xilian_splitter 就是这么喂进来的）。
        tone 指定时全场用同一种语气（等于不分段）；merge=False 则严格一段一合成。

        返回音频文件路径。任何一段失败都直接抛出去 —— 让调用方决定退回文字，
        总比发一条念了一半的语音好。
        """
        raw_segs = [str(s) for s in (segments or []) if str(s).strip()]
        if not raw_segs:
            raise ValueError("没有能念出来的内容")

        if tone:
            joined = "".join(clean_text(s) for s in raw_segs).strip()
            if not joined:
                raise ValueError("没有能念出来的内容")
            units = [(joined, tone, 1.0)]
        else:
            use_merge = self.seg_merge if merge is None else bool(merge)
            units = plan_tone_units(raw_segs, merge=use_merge, max_chars=self.max_unit_chars)

        if not units:
            raise ValueError("没有能念出来的内容")

        t0 = time.time()
        blobs: list[bytes] = []
        fmt = "mp3"
        for spoken, key, strength in units:
            blob, fmt = await self._synthesize_blob(spoken, key, strength)
            blobs.append(blob)

        path = self._new_path(fmt, "xilian_full")
        await self._concat_mp3(blobs, path, fmt)

        size = os.path.getsize(path) if os.path.isfile(path) else 0
        duration = await probe_duration(path)
        logger.info(
            "[xilian_tts] 整条合成完成：%d 段文字 → %d 个语气单元 → %s（%d 字节，%s，耗时 %.2fs）",
            len(raw_segs),
            len(units),
            path,
            size,
            ("%.1f 秒" % duration) if duration else "时长未知",
            time.time() - t0,
        )
        if duration and duration > 60:
            logger.warning(
                "[xilian_tts] 这条语音 %.0f 秒，超过多数平台 60 秒的语音上限，可能发送失败；"
                "可以把 /分段 段长 调大，或把 /分段 语音 设成 text",
                duration,
            )
        return path

    async def terminate(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


# 注册到 AstrBot 的 provider 表里。
# 需要在 provider_manager.initialize() 之前完成，插件的导入时机正好早于它。
if PROVIDER_TYPE not in provider_cls_map:
    register_provider_adapter(
        PROVIDER_TYPE,
        PROVIDER_DESC,
        provider_type=ProviderType.TEXT_TO_SPEECH,
        provider_display_name="昔涟 TTS",
        default_config_tmpl={
            "id": PROVIDER_TYPE,
            "type": PROVIDER_TYPE,
            "provider_type": "text_to_speech",
            "enable": False,
            "api_base": DEFAULT_ENDPOINT,
            "voice": DEFAULT_VOICE,
            "speed": 1,
            "volume": 1,
            "model": DEFAULT_MODEL,
            "timeout": 60,
            "tone_enabled": True,
            "tone_strength": 1.0,
            "seg_merge": True,
            "max_unit_chars": MAX_UNIT_CHARS,
        },
    )(XilianTTSProvider)


def _default_provider() -> XilianTTSProvider:
    """TTS provider 还没被选中时的兜底实例，让 /语音 永远可用。"""
    return XilianTTSProvider(
        {
            "id": PROVIDER_TYPE,
            "type": PROVIDER_TYPE,
            "api_base": DEFAULT_ENDPOINT,
            "voice": DEFAULT_VOICE,
            "speed": 1,
            "volume": 1,
            "model": DEFAULT_MODEL,
            "timeout": 60,
            "tone_enabled": True,
            "tone_strength": 1.0,
            "seg_merge": True,
            "max_unit_chars": MAX_UNIT_CHARS,
        },
        {},
    )


class XilianTTS(Star):
    """昔涟的语音。

    /语音 <文本>          —— 用昔涟的音色念出来，语气自动判断
    /语音 <语气> <文本>    —— 按指定语气念，例如 /语音 开心 今天真好呀
    /语音 分段 <文本>      —— 看看这段话会被分成几个语气单元（不合成、不花配额）
    /语音 整条 <文本>      —— 按语气分段合成**一条完整语音**，试听用
    /语音 状态            —— 看当前的音色 / 桥接地址 / 语气设置
    /语音 语气            —— 看有哪些语气可选
    """

    def __init__(self, context: Context):
        super().__init__(context)
        logger.info(
            "[xilian_tts] 已加载：类型 %s，默认桥接 %s，音色 %s（语气由 provider 配置的 tone_enabled 控制，"
            "分段合成 %s）",
            PROVIDER_TYPE,
            DEFAULT_ENDPOINT,
            DEFAULT_VOICE,
            "开" if find_ffmpeg() else "开（未找到 ffmpeg，将直接拼接）",
        )

    async def _pick_provider(self, event: AstrMessageEvent) -> XilianTTSProvider:
        """优先用 AstrBot 当前选中的 TTS provider，没有就用兜底实例。"""
        try:
            prov = await self.context.get_using_tts_provider_async(event.unified_msg_origin)
        except Exception:  # noqa: BLE001
            prov = None
        if isinstance(prov, XilianTTSProvider):
            return prov
        return _default_provider()

    @staticmethod
    def _tone_table() -> str:
        lines = []
        for tone in TONES.values():
            mark = "（默认）" if tone.key == "calm" else ""
            lines.append(
                f"· {tone.label}{mark}：语速×{tone.speed_mul:.2f}"
                f"　音高{tone.pitch:+d}　音量×{tone.vol_mul:.2f}"
                f"　情绪 {tone.emotion or '不指定'}"
            )
        return "\n".join(lines)

    @filter.command("语音", alias={"tts"})
    async def tts_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """让昔涟把一段文字念出来。用法：/语音 今天也要开心呀 ；/语音 开心 今天真好呀 ；/语音 状态"""
        arg = (arg or "").strip()
        provider = await self._pick_provider(event)

        if arg in {"状态", "status", "info"}:
            ok, msg = await provider.health()
            tone_state = (
                f"开启（整体强度 ×{provider.tone_strength:.2f}）"
                if provider.tone_enabled
                else "关闭"
            )
            merge_state = "开（同语气合并）" if provider.seg_merge else "关（逐段合成）"
            ffmpeg_state = "有" if find_ffmpeg() else "没有（改用直接拼接）"
            yield event.plain_result(
                "昔涟的语音设置——\n"
                f"桥接地址：{provider.endpoint}\n"
                f"音色：{provider.voice}　语速：{provider.speed}\n"
                f"模型：{provider.get_model()}\n"
                f"语气：{tone_state}\n"
                f"分段合成：{merge_state}　单次上限 {provider.max_unit_chars} 字\n"
                f"拼接用的 ffmpeg：{ffmpeg_state}\n"
                f"桥接自检：{'✅ ' if ok else '❌ '}{msg}"
            )
            return

        if arg in {"语气", "tone", "tones"}:
            yield event.plain_result(
                "人家现在能这样念——\n"
                f"{self._tone_table()}\n"
                "想指定就用：/语音 开心 今天真好呀"
            )
            return

        head, _, rest = arg.partition(" ")
        rest = rest.strip()

        # /语音 分段 <文本> —— 只看怎么切语气，不合成
        if head in {"分段", "units", "预览"}:
            sample = rest
            if not sample:
                yield event.plain_result(
                    "想看哪一段呀？这样写：/语音 分段 嗨♪好久不见！人家今天特别开心呢～"
                )
                return
            units = plan_tone_units(
                split_sentences(sample),
                merge=provider.seg_merge,
                max_chars=provider.max_unit_chars,
            )
            if not units:
                yield event.plain_result("这段没有能念出来的内容。")
                return
            yield event.plain_result(
                "%d 字 → %d 个语气单元：\n%s\n（合成时会按这个划分逐段调参数，再拼成一条语音）"
                % (len(sample), len(units), describe_units(units))
            )
            return

        # /语音 整条 <文本> —— 分段语气合成一条完整语音，直接试听
        if head in {"整条", "full"}:
            sample = rest
            if not sample:
                yield event.plain_result(
                    "想试哪一段呀？这样写：/语音 整条 嗨♪好久不见！人家今天特别开心呢～"
                )
                return
            try:
                path = await provider.get_audio_segmented(split_sentences(sample))
            except Exception as e:  # noqa: BLE001
                logger.error("[xilian_tts] 整条合成失败：%r", e)
                yield event.plain_result(f"这次没念出来……{type(e).__name__}: {e}")
                return
            yield event.chain_result([Record(file=path, url=path, text=sample)])
            return

        # /语音 开心 今天真好呀 —— 头一个词是语气名的话，就按它念
        tone_key = None
        if rest and head in TONE_ALIASES:
            tone_key = TONE_ALIASES[head]
            arg = rest
        elif arg in TONE_ALIASES:
            arg = ""

        if not arg:
            yield event.plain_result(
                "想让人家念什么呀？这样写：/语音 今天也要开心呀\n"
                "想指定语气：/语音 开心 今天真好呀\n"
                "看怎么切语气：/语音 分段 <文本>\n"
                "整条试听：/语音 整条 <文本>\n"
                "看看有哪些语气：/语音 语气"
            )
            return

        try:
            path = await provider.get_audio(arg, tone=tone_key)
        except Exception as e:  # noqa: BLE001
            logger.error("[xilian_tts] 语音合成失败：%r", e)
            yield event.plain_result(f"这次没念出来……{type(e).__name__}: {e}")
            return

        yield event.chain_result([Record(file=path, url=path, text=arg)])

    async def terminate(self) -> None:
        logger.info("[xilian_tts] 已卸载")
