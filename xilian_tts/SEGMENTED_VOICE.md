# 分段语气合成：文字一句一句，声音只有一条

> 2026-09-18 落地。xilian_tts 1.4.0 / xilian_splitter 1.1.0。

## 要解决的事

xilian_splitter 把长回复拆成 8 字左右的小段发出去，读起来像真人在聊天。
但这台机器的 TTS 是 100% 触发 + 双输出的，于是：

- 按段配音 → 一条回复变成十几条语音，像在念清单；
- 干脆不配音 → 声音和文字对不上，只有半截。

想要的是：**文字照旧一句一句，语音只有一条完整的；而且她哪句换了语气，那条语音里听得出。**

## 怎么做的

分成两半，「切」在 splitter，「念」在 xilian_tts。

```
splitter：把一条回复切成 N 段文字
          └─ 把 N 段文字整份交给 provider.get_audio_segmented()
                          │
xilian_tts：逐段判语气 → 同语气并成一次合成 → 逐次调桥 → 拼成一条 mp3
```

### 一、语气单元（plan_tone_units）

桥每调一次约 1 秒（实测：短句 1.10s、中句 1.08s、长句 0.99s）。16 段文字逐段去念
就要 16 秒，用户等到天荒地老。所以：

- 相邻且语气相同的段**并成一次合成**，中间不换调子；
- 只有语气真的变了，才换一组参数重新合成；
- 单次合成单元封顶 400 字（`max_unit_chars`），免得一次请求过长。

实测一条 5 段的回复 → 3 个语气单元 → 4.2 秒产出 12.2 秒的音频。
`merge=False` 时同一份输入是 5 个单元 / 5.9 秒 —— 省下来的就是合并的功劳。

### 二、拼接（_concat_mp3）

桥返回的是带 ID3v2 头的 mp3。三级回退：

1. `ffmpeg -f concat -safe 0 -i list.txt -c copy` —— 无损直拼，最快；
2. 同上但 `-c:a libmp3lame -b:a 128k` —— 参数不一致导致直拼失败时重编码；
3. 纯 Python 字节拼接（`concat_mp3_raw`，去掉后续片段的 ID3 头 / ID3v1 尾）。

第 3 级保证「没有 ffmpeg 也能出声」，只是时长显示可能不准。
这台机器上 ffmpeg 在 `D:\Astrbot\_tool\ffmpeg\ffmpeg.exe`，也在 PATH 里。

拼接完成后用 ffprobe 读一次时长写进日志；超过 60 秒会额外警告一句
（多数平台语音上限 60 秒，约 340 字以上会碰到）。

## 接口

```python
await provider.get_audio_segmented(segments, tone=None, merge=None) -> str
```

- `segments`：已经切好的文字段（谁切的都行）；
- `tone`：指定时全场同一语气（等于不分段）；
- `merge`：默认跟随 provider 配置的 `seg_merge`；
- 返回音频文件路径；任何一段失败直接抛出，让调用方退回去只发文字。

`get_audio(text, tone=None)` 老接口不动，单独念一句仍然走它。

## 配置（cmd_config.json → provider → xilian_tts）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `seg_merge` | `true` | 相邻同语气是否并成一次合成。关掉 = 严格一段一合成（更慢、更贴分段） |
| `max_unit_chars` | `400` | 单个合成单元的字数上限 |
| `tone_enabled` | `true` | 语气总开关（原本就有） |
| `tone_strength` | `1.0` | 语气整体强度（原本就有） |

## 指令

```
/语音 状态            看音色 / 桥接 / 语气 / 分段合成 / ffmpeg
/语音 分段 <文本>      看这段会被分成几个语气单元（不合成、不花配额）
/语音 整条 <文本>      按分段语气合成一条完整语音，直接试听
```

`/语音 分段` 的输出长这样：

```
12 字 → 2 个语气单元：
1. [开心 强度0.85] 8字 嗨♪好久不见！
2. [温柔 强度0.75] 4字 你呢～
```

## 验证

- `D:\Astrbot\_tool\test_xilian_tts.py`：纯逻辑 11 项（切句、同语气合并、字数上限、
  ID3 剥离、字节拼接）+ 真实桥接 5 项（单段、整条、不合并、指定语气、时长对照）。
  带 `logic` 参数只跑前半段，不碰桥。
- `D:\Astrbot\_tool\test_xilian_splitter_voice.py`：full / seg / text / 合成失败四条投递路径，
  带 `real` 参数时真连一次桥。
- 两个脚本都要用 AstrBot 的 `.venv\Scripts\python.exe`，并设 `ASTRBOT_ROOT`。

## 已知边界

- **合成是串行的**（按语气单元逐个调桥）。单元通常 2～4 个，splitter 那边又是在
  发文字的同时后台合成，用户感知的等待很短。真要再快，可以在 provider 里改成
  并发 gather —— 桥是 ThreadingHTTPServer，能并发，但上游是否限流没验证过。
- **超长语音**：60 秒上限靠日志提醒，不阻断。想稳一点就把 `/分段 段长` 调大。
- **语气判定靠规则**（关键词 + 标点），不是模型。判不准的地方会退化成「平静」，
  听感上就是没起伏，不会出错。
