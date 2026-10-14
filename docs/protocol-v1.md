# 终端协议 v1（ESP32 ⇄ 主机）

固定一种协议版本（`v1`），只覆盖 PRD §7 允许的五类消息：语音、任务状态、
补充/确认、取消、结果。不做通用设备平台。

## 传输

- WebSocket（`ws://<host>:<port>/ws`），二进制/文本混用：
  - **控制消息**：UTF-8 JSON 文本帧
  - **音频数据**：二进制帧（PCM 16kHz/mono/int16，或 Opus，`hello` 中声明）
- 认证：连接后第一条必须是 `hello`，带共享 token；失败即断开。

## 消息（控制帧，均为 JSON）

字段 `t` = 类型。**所有消息带 `v":"1"`**。

### ESP32 → 主机

| t | 字段 | 说明 |
|---|---|---|
| `hello` | `token`, `device_id`, `fw`, `audio`{`codec`,`rate`,`channels`,`bits`} | 握手认证，声明音频格式 |
| `audio_start` | `request_id`, `seq` | 开始一段录音（单次 ≤60s） |
| `audio_end` | `request_id`, `seq`, `duration_ms` | 录音结束，提交执行 |
| `audio_abort` | `request_id` | 无声/断开，不提交（PRD §5.1） |
| `answer` | `request_id`, `text` | 对追问的补充回答（纯文本） |
| `cancel` | `request_id` | 取消当前任务后续动作 |
| `mute` | `on` | 静音/停止播报（与取消分开，PRD §5.1） |
| `ping` | — | 心跳 |

> 音频帧本身不带 `request_id`：本连接内同一时刻只有一段录音。

### 主机 → ESP32

| t | 字段 | 说明 |
|---|---|---|
| `welcome` | `v`, `session`, `server_audio`{...} | 认证通过 |
| `status` | `request_id`, `state` | `idle`/`recording`/`processing`/`need_user`/`offline` |
| `question` | `request_id`, `text`, `expect`(`text`\|`audio`) | 追问（屏幕+语音） |
| `result` | `request_id`, `kind`, `text`, `evidence?` | `kind`: `success`/`failed`/`cancelled`/`unknown` |
| `speak` | `audio_id`, `text`, `format` | 随后二进制帧为该段 TTS 音频 |
| `notice` | `level`, `text` | 离线提示等 |
| `pong` | — | 心跳应答 |

`audio_id` 为 0 表示纯语音播报（无文本）；二进制音频帧紧随对应 `speak`。

## 幂等与去重（PRD §5.3）

- `request_id` 由 ESP32 生成（单调递增或 UUID），**一次提交一个**。
- 主机对同一 `request_id` 的处理结果缓存；重复 `audio_end` 直接回已有状态，
  **不重跑手机任务**。
- 主机崩溃重启后，不自动从"发送按钮"继续执行。
- 连接断开重连后，ESP32 不重发已确认的旧录音。

## 固定约定

- 协议版本：`v1`（`hello` 与 `welcome` 双向校验，不一致拒绝）。
- 音频：录制 **16 kHz / 单声道 / int16 PCM**（默认）；TTS 回传 16 kHz PCM。
- 单次录音上限 **60 秒**，超时自动 `audio_end`。
- 文本一律 UTF-8。
