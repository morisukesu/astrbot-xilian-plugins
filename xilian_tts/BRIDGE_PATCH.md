# 让语气「全效」的最后一步：给桥加两个字段的透传

> **状态：已于 2026-09-18 应用完毕。**
> 改动落在 `D:\TTS WORK\bridge\senseaudio-bridge.py`（备份 `senseaudio-bridge.py.bak-20260918`），
> 桥已重启，`pitch` / `emotion` 两端已实测跑通。本文保留作为改动记录与回退依据。
> 2026-09-18 追加：`VALID_EMOTIONS` 纳入上游扩展档 `shyness`（害羞）/ `coquetry`（撒娇），
> 插件侧「撒娇」「害羞」两档已改用这两个值，实测上游收下且无报错。

插件已经把语气算好了，会随请求一起发出去：

```json
{
  "text": "...",
  "voiceId": "vc-VisW54vqfp2ruPMq7YvXPK",
  "speed": 1.07,
  "volume": 1.05,
  "pitch": 2,
  "emotion": "happy"
}
```

其中：

| 字段 | 桥现在会怎么处理 |
| :--- | :--- |
| `speed` | **已经转发**，改完立刻能听出语气差别 |
| `volume` | **已经转发**，同上 |
| `pitch` | 被忽略——桥里写死了 `"pitch": 0` |
| `emotion` | 被忽略——桥根本没读这个键 |

所以现在装上插件，语速和音量那一层已经生效了；`pitch` / `emotion`
这两层要等桥补上透传。它们是 MiniMax 协议族的标准字段（SenseAudio 同族），
AstrBot 自带的 MiniMax provider 用的就是 `voice_setting.emotion` / `voice_setting.pitch`，
上限 `-12 ~ +12`。

---

## 要改的文件

`D:\TTS WORK\bridge\senseaudio-bridge.py`（位于工作区之外，改动需你明确授权）

一共三处，改完不用重启 Cyrene，但**桥进程本身要重启一次**才会加载新代码。
另外顺手给桥的日志行加了 `pitch=` / `emotion=`，以后能直接从 `bridge.log` 确认字段有没有发出去。

### 一、`build_payload`：pitch 别再写死，顺手带上 emotion

```python
def build_payload(cfg, text, voice, speed, volume, pitch=0, emotion=""):
    voice_setting = {
        "voice_id": voice,
        "speed": speed if isinstance(speed, (int, float)) else 1,
        "vol": volume if isinstance(volume, (int, float)) else 1,
        "pitch": pitch if isinstance(pitch, int) else 0,
    }
    # 情绪名不在合法集合里就不要塞，免得上游犯嘀咕
    if emotion in {"happy", "sad", "angry", "fearful", "disgusted",
                   "surprised", "neutral", "shyness", "coquetry"}:
        voice_setting["emotion"] = emotion
    return {
        "model": cfg.get("model") or "sensenova-tts-2.0",
        "text": text,
        "stream": False,
        "voice_setting": voice_setting,
    }
```

### 二、`synthesize`：把新参数递下去

```python
def synthesize(cfg, text, voice, speed, volume, pitch=0, emotion=""):
    payload = build_payload(cfg, text, voice, speed, volume, pitch, emotion)
    # 下面原样不动
```

### 三、`Handler.do_POST`：从请求里读出来

```python
        data = synthesize(
            cfg,
            text,
            voice,
            req.get("speed"),
            req.get("volume"),
            req.get("pitch", 0),
            (req.get("emotion") or "").strip().lower(),
        )
```

---

## 改完怎么确认

1. 重启桥（`stop-bridge.cmd` → `start-bridge.cmd`，或用你原来的启动方式）。
2. 在 QQ 里发：

   ```
   /语音 状态
   /语音 平静 今天天气不错
   /语音 开心 今天天气真好呀
   /语音 低落 今天有点累
   /语音 语气
   ```

   同一条文本、换语气念一遍，能听出语速和起伏的差别就对了。
3. 桥那边的 `bridge.log` 里，每条合成都会记下 `voice=`……如果想知道
   emotion 有没有真的传上去，可以在改完之后额外确认：`bridge.log`
   的日志行本身不含情绪字段，最直接的判断还是耳朵。

---

## 关于「验证」的一句实话

合成本身带随机性——同一条文本连打两次，输出长度会差 ±20% 左右。
所以**不能靠比较音频文件大小来判断参数有没有生效**（踩过这个坑）。
语气效果最终要靠耳朵听：`/语音 平静 <文本>` 和 `/语音 开心 <文本>`
放在一起听，差别应该明显。
