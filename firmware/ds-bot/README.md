# ds-bot 固件

Waveshare ESP32-S3-Touch-AMOLED-2.16 语音终端，ESP-IDF 5.5.5 / LVGL 9。当前握手固件版本 **0.2.5**，更新记录日期为 2026-10-17。

## 配置、构建与烧录

本机已验证 Python 3.13 的 ESP-IDF 环境；主机 Bridge 的虚拟环境与固件环境分开使用。

```bash
export PATH="$HOME/.idf-python:$PATH"
. "$HOME/esp/esp-idf/export.sh"
cd /Users/haoyu/dayu-dot/firmware/ds-bot
# 初次配置时，在“ds-bot 配置”填写 Wi-Fi、Bridge 地址、端口与 token
idf.py menuconfig
idf.py build
# 停止占用串口的记录器后再执行
idf.py -p /dev/cu.usbmodem101 flash
```

`CONFIG_DSBOT_BRIDGE_HOST` 必须是设备可访问的主机局域网地址，默认端口 8765；token 与主机 `.env` 对应。Wi-Fi 密码和 token 保存在本地 `sdkconfig` 或 `sdkconfig.defaults.local`，不写入共享文档。复制工程到其他电脑时重新确认端口和网络地址。

如果 `~/.dayu/serial_logger.py` 正在运行，烧录前先检查并停止对应进程：

```bash
pgrep -fl serial_logger.py
kill <记录器PID>
```

烧录完成、工具释放串口后，恢复本机记录器（使用带 `pyserial` 的 ESP-IDF Python）：

```bash
nohup "$HOME/.espressif/python_env/idf5.5_py3.13_env/bin/python" \
  "$HOME/.dayu/serial_logger.py" \
  >> "$HOME/.dayu/logs/serial-logger-stderr.log" 2>&1 &
```

记录器脚本属于本机维护工具，不随仓库分发。USB CDC 无读取端时可能丢失日志，排错时应持续记录。不要同时启动多个记录器占用同一串口。

## 已实现能力

| 能力 | 当前实现 |
|---|---|
| 显示 | CO5300 480×480 AMOLED，U-Face 256px 精灵，待机为方形黑眼睛，约每 3 秒平滑眨眼，32px 中文子集字体 |
| 音频 | ES7210 / ES8311，固定 16kHz 单声道 16bit PCM |
| 输入 | 左右键 PTT / 长按取消；中间电源键短按开关屏幕；屏幕触摸不录音 |
| 联网 | Wi-Fi STA + WebSocket v1；普通连接关闭后约 3 秒重连 |
| 上传 | PTT 与 VAD 音频事务串行，取消帧可穿插，旧上传通过代次失效 |
| 播放 | 完整 PCM 入队播放，取消清理接收缓冲、队列和在播旧音频 |
| 追问 | 说话动画 → 实际播放完成回执 → 收到 `listen` → “请说话” → 回答上传 |
| 终态 | 成功、失败、取消、取消未确认等真实状态提示 |

PTT 缓冲 30 秒。VAD 分两档：空闲唤醒与执行中取消监听保持最多 5 秒、静音 500 毫秒提交；唤醒后的指令及问题回答改为最多 30 秒、静音 1200 毫秒提交，允许句中自然停顿。麦克风增益 34.5 dB，起录 RMS 阈值 500、静音判断阈值 250；连续两帧才触发，并保留 200 毫秒句首音频。环境标定以 `audio.c` 和 `vad_policy.h` 为准。

唤醒后 20 秒内开始收音即可，录音过程中不切换回短片段策略。指令以 `mode:"command"` 上传，Bridge 在收到录音时绑定本次唤醒，避免长句识别完成时超过唤醒窗口而被丢弃。一次唤醒只提交一条完整指令。达到缓冲上限的片段标记 `truncated:true`，主机提示重说，不执行被截断的任务或回答。

## 语音澄清与取消

问题携带 `request_id` 和 `question_seq`，其 `speak` 音频再携带 `audio_id`。设备在问题期间暂停 VAD，播完后写入静音尾部排空 I2S，发送 `speak_done`；匹配的 `listen` 才会开启回答收音。回答以 `mode:"answer"` 和 `answer_to` 绑定原任务，无需唤醒词。

VAD 停顿后上传回答，设备显示处理中并暂停重复采集；识别为空或失败时由 Bridge 重新发 `listen`。播放期间持续排空麦克风缓冲，避免恢复收音时提交残留播报。旧任务、旧问题或已取消播放的回执不能打开新问题的收音。

任务活跃时，任一录音键长按 2 秒发送 `cancel`，松手不产生新任务。设备能够收音时还可说“小D 小D，取消任务”；播报期间使用物理键取消。最终取消结果以主机安全收尾后的消息为准。

## 本地验证与实机范围

```bash
cd /Users/haoyu/dayu-dot/firmware/ds-bot
sh tools/test-input-policy.sh
sh tools/test-vad-policy.sh
```

原生测试覆盖长按阈值、松手抑制、两键归属、任务切换、时钟回绕，以及两档 VAD 的停顿和长度上限。0.2.5 已通过 ESP-IDF 构建并烧录，Bridge 已确认该版本连接。此前两轮实机问答及回答后手机只读核查已通过；最新声学复测范围见[开发计划](../../docs/plan.md)。

本次 Bridge/固件协议扩展需配套更新；仅修改主机模型步数或 API Key 不需要重新烧录。只改中文文案且出现字库外字符时，需重新生成字体再构建。

## 素材与字体

- `tools/build-uface-256.sh`：从 U-Face 原始素材生成 256px 精灵资源。
- `tools/build-cjk-font.sh [字号]`：生成中文子集字体；增加文案先补充 `SYMBOLS`，再重新生成。
- [硬件与目录](../README.md)、[终端协议](../../docs/protocol-v1.md)。
