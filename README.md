# AstrBot xilian 插件集

昔涟专用插件集合，为 AstrBot 提供丰富的功能扩展和个性化体验。

## 插件列表

| 插件 | 描述 | 版本 |
|------|------|------|
| `xilian_agent` | 昔涟的管家，提供 Agent 能力与执行面板 | 2.0.0 |
| `xilian_tts` | 语音插件，支持 Cyrene 的本地语音桥 | 1.0.0 |
| `xilian_splitter` | 分段回复插件，支持语音配合 | 1.0.0 |
| `xilian_memory` | 记忆管理插件 | 1.0.0 |
| `xilian_debounce` | 防抖插件 | 1.0.0 |
| `xilian_favour` | 收藏管理插件 | 1.0.0 |
| `xilian_identity` | 身份识别插件 | 1.0.0 |
| `xilian_stickers` | 贴纸插件 | 1.0.0 |
| `xilian_psd2live` | PSD2Live 直连插件（昔涟专用） | 2.0.0 |

## 安装说明

### 1. 克隆仓库

```bash
git clone https://github.com/YOUR_USERNAME/xilian-plugins.git
cd xilian-plugins
```

### 2. 复制插件

将需要的插件目录复制到 AstrBot 的 `data/plugins/` 目录：

```bash
cp -r xilian_agent /path/to/astrbot/data/plugins/
```

### 3. 依赖要求

- AstrBot >= 4.16
- Python >= 3.9

## 配置说明

每个插件有自己的配置方法，详见各插件目录下的 README.md。

## 许可协议

MIT License - 自由使用、修改和分发，保留原始版权声明。

## 反馈与支持

在 GitHub 仓库中提出 Issue。

---

**作者**: Mori-森亜ミミカ  
**日期**: 2026-09-25
