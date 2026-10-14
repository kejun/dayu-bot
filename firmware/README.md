# 固件：Waveshare ESP32-S3-Touch-AMOLED-2.16

## 硬件

| 项 | 值 |
|---|---|
| 芯片 | ESP32-S3（双核 + LP 核，Wi-Fi + BLE5） |
| 板上 | **16MB Flash + 8MB PSRAM**（实测 `esptool flash_id`） |
| 屏幕 | 2.16" AMOLED，368×448（QSPI 屏驱） |
| 触摸 | 电容触摸（I2C） |
| 音频 | 麦克风 + 扬声器，ES8311 codec（I2S） |
| 其他 | AXP2101 电源管理、SD 卡槽、RTC 等 |

> 实测记录：`Chip type: ESP32-S3 (QFN56) rev v0.2`，`Embedded PSRAM 8MB`，
> `Detected flash size: 16MB`。

## 固件职责（PRD §4）

只做端侧该做的：按住录音、上传、状态显示、结果播放。**不理解语义、不直接控制手机。**

- 屏幕 5 态：`idle / recording / processing / need_user / result`
  （结果区分 `success / failed / cancelled / unknown`）
- 按住按键录音，松开提交，单次 ≤60s；开始录音时停止播报
- 通过 WiFi 用 `docs/protocol-v1.md` 的 v1 协议连主机
- 桌宠动画只是状态的表现（第 5 阶段）

## 在 ESP-IDF 下的依赖

- ESP-IDF **v5.5.5**（已装：`~/esp/esp-idf`，`get_idf` 加载）
- 屏幕/触摸/音频驱动优先复用 **Waveshare 官方示例**：
  `waveshare/ESP32-S3-Touch-AMOLED-2.16`（BSP 或 examples），避免自己点屏
- 音频走 ESP-IDF 自带 `esp_driver_i2s` + ES8311 驱动

> 注意：`esp-dev-skill` 现有 recipes 覆盖 ESP-IDF / esp-agents-firmware /
> esp-claw 等，**未包含 Waveshare 该型号**，因此固件以 Waveshare 官方仓库为准。

## 目录规划（待建）

```
firmware/
├── README.md
├── CMakeLists.txt
├── sdkconfig.defaults
├── partitions.csv          # 16MB
├── main/
│   ├── app_main.c
│   ├── board.c/.h          # 屏幕/触摸/音频/按键初始化
│   ├── audio.c/.h          # 录音 + 播放
│   ├── ui.c/.h             # 5 状态界面 + 桌宠
│   ├── net.c/.h            # WiFi + WebSocket v1 客户端（录音上传/状态接收）
│   └── Kconfig.projbuild
└── components/             # 如需
```

## 下一步

1. 拉取 Waveshare 官方 2.16 示例，确认 `idf.py build/flash` 在 v5.5.5 下通过
2. 拆分出 board bring-up（显示 + 触摸 + 音频）
3. 接入 `net.c` 与主机 `host/bridge` 的 v1 协议
