# ds-bot 固件

Mini Agent Assistant 的 ESP32-S3 终端（Waveshare ESP32-S3-Touch-AMOLED-2.16）。

## 构建 / 烧录

```bash
export PATH="$HOME/.idf-python:$PATH"     # 让 python3 指向 3.13
. ~/esp/esp-idf/export.sh
cd firmware/ds-bot
idf.py build
idf.py -p /dev/cu.usbmodem101 flash
```

> ⚠️ 如果开着后台串口记录器（`~/.dayu/serial_logger.py`），**烧录前先 `pkill -f serial_logger.py`**，
> 否则端口被占用会失败。烧完再启动它。

## 当前进度

| 阶段 | 状态 |
|---|---|
| 显示（CO5300 AMOLED + LVGL9） | ✅ |
| U-Face 精灵（6 组表情） | ✅ 256px |
| 中文字体 | ✅ 32px 子集（`tools/build-cjk-font.sh`） |
| 触摸（CST9217/9220） | ✅ 按住录音/松手回放 |
| 音频（ES7210 + ES8311） | ✅ 16kHz 单声道 |
| 联网（WiFi → 主机 bridge） | ⬜ |

## 素材与字体

- 精灵：`tools/build-uface-256.sh`（从 U-Face 素材包的 480px 源帧下采样到 256）
- 字体：`tools/build-cjk-font.sh [字号]`（改字号或加字后重跑）
