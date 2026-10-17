# 固件：Waveshare ESP32-S3-Touch-AMOLED-2.16

更新：2026-10-17。实际工程位于 [ds-bot/](ds-bot/)，已完成显示、音频、物理键、Wi-Fi/WebSocket、任务取消和语音澄清；当前握手固件版本为 **0.2.0**。

## 硬件与环境

| 项目 | 项目使用的配置 |
|---|---|
| 主控 | ESP32-S3，双核 Xtensa LX7，Wi-Fi / BLE |
| 存储 | 16MB Flash，8MB Octal PSRAM |
| 屏幕 | CO5300，**480×480** AMOLED / QSPI；以当前 Waveshare BSP 的分辨率定义为准 |
| 触摸 | CST9217 / CST9220，屏幕不用于触发录音 |
| 音频 | ES7210 麦克风 ADC、ES8311 扬声器 codec，16kHz / 单声道 / 16bit PCM |
| 按键 | Key2 GPIO0、Key3 GPIO18；中间 Key1 为 AXP2101 PWRON |
| 开发环境 | ESP-IDF **5.5.5**、Python 3.13、Waveshare BSP、LVGL 9、`esp_codec_dev` |

## 当前交互

- 空闲时左右录音键按住说话、松开上传；中间键短按开关屏幕。
- 任务活跃时左右键转为取消入口，持续按住 2 秒即可发送取消，松手不另起任务。
- VAD 音频上传主机后识别唤醒词和取消；设备不在本地运行千问或专用唤醒词模型。
- 需要确认时显示说话动画并播报，实际播放完成后发回执，收到 `listen` 才显示“请说话”并接收回答。
- 播放期间不收语音，持续排空麦克风缓冲；取消可中断旧播报和已排队音频。
- 结果区分完成、失败、取消和未确认；精灵动画跟随真实状态，不能表示任意执行百分比。

当前 PTT 缓冲为 10 秒，VAD 单段最多约 5 秒、静音 900 毫秒后提交。主机允许接收 60 秒不代表固件具备 60 秒录音缓冲。

## 工程结构

```text
firmware/
├── README.md
└── ds-bot/
    ├── CMakeLists.txt / partitions.csv / sdkconfig.defaults
    ├── main/
    │   ├── main.c                 # UI、任务身份、PTT、取消、播放回执
    │   ├── audio.c / audio.h      # 采集、VAD、播放与取消代次
    │   ├── input_policy.c / .h    # 两键归属、长按阈值、任务切换保护
    │   ├── net.c / net.h          # Wi-Fi、WebSocket、串行音频上传
    │   ├── buttons.c / buttons.h
    │   ├── axp2101.c / axp2101.h
    │   └── Kconfig.projbuild
    ├── components/uface/          # 精灵素材与 32px 中文子集字体
    ├── tests/test_input_policy.c
    └── tools/                    # 素材、字体和原生按键测试
```

构建、配置和串口记录器维护见 [ds-bot README](ds-bot/README.md)，线路消息见[协议 v1](../docs/protocol-v1.md)。本次实机验证和仍待验收项见[开发计划](../docs/plan.md)。
