# 终端协议 v1（ESP32 ⇄ Bridge）

更新：2026-10-17。WebSocket 地址为 `ws://<主机>:8765/ws`，控制帧为 UTF-8 JSON，音频为二进制 PCM。问题播放回执与语音回答扩展要求固件 `0.2.0`，协议字段 `v` 仍为字符串 `"1"`。

## 传输与握手

每条控制消息都包含 `{"v":"1","t":"<类型>"}`。首条必须是 `hello`；主机校验协议版本、消息类型，以及已配置的共享 token，失败则关闭连接。固件 token 与主机 `BRIDGE_TOKEN` / `BRIDGE_TOKEN_FILE` 的值一致；主机 token 留空仅适用于本地受控测试。

当前链路固定 **16 kHz、单声道、16bit 小端 PCM（`pcm_s16le`）**，没有 Opus 编解码或格式协商实现。音频二进制帧不自带 ID，同一连接同一方向只处理一段音频事务。主机可在上传或后台识别时接收取消控制帧。

## 设备 → Bridge

| `t` | 字段 | 当前行为 |
|---|---|---|
| `hello` | `token`, `device_id`, `fw`, `audio` | 认证并声明设备与音频格式，固件当前为 `0.2.0` |
| `audio_start` | `request_id`, `mode?`, `answer_to?`, `question_seq?` | 新录音；`mode` 为 `task`、`wake` 或 `answer`，缺省为 `task` |
| `audio_end` | `request_id`, `mode?`, `duration_ms?` | 结束当前录音，模式以 `audio_start` 保存的值为准；时长为诊断信息 |
| `audio_abort` | `request_id` | 放弃当前录音，不提交新任务 |
| `answer` | `request_id`, `text`, `question_seq?` | 文本补充，校验发起设备、任务是否等待回答，以及传入的轮次 |
| `speak_done` | `request_id`, `question_seq`, `audio_id`, `ok` | 问题实际播放结束回执，`ok` 为布尔值 |
| `cancel` | `request_id?` | 取消指定任务；省略时取消本设备的未完成任务和待处理录音 |
| `mute` | `on` | 控制该连接后续播报，不取消手机任务；不能代替 `stop_speak` |
| `ping` | — | 返回 `pong` |

`request_id` 在音频消息中是录音 ID，在问题和结果中是原任务 ID。语音回答的录音 ID 独立生成，`answer_to` 指向原任务，`question_seq` 指向具体问题。任务归属不能仅根据用户提供的任意 ID 推断。

## Bridge → 设备

| `t` | 字段 | 当前行为 |
|---|---|---|
| `welcome` | `session`, `server_audio` | 握手通过，告知主机音频格式 |
| `status` | `request_id`, `state` | 录音/处理中状态；当前含 `recording`、`processing`、`recognizing`、`cancelling`、`idle` 等 |
| `question` | `request_id`, `question_seq`, `text`, `expect` | 原任务暂停并进入问题播报流程，当前 `expect:"audio"` |
| `speak` | `audio_id`, `text`, `format`, `bytes`, `request_id?`, `question_seq?` | 随后发送恰好 `bytes` 字节完整 PCM；问题音频附原任务与轮次 |
| `stop_speak` | — | 丢弃接收中的旧 TTS、清空队列并中止当前播放 |
| `listen` | `request_id`, `question_seq` | 仅匹配中的问题允许开启回答收音 |
| `result` | `request_id`, `kind`, `text`, `evidence` | 任务终态；`kind` 为 `success`、`failed`、`cancelled` 或 `unknown` |
| `wake` | — | 唤醒亮屏提示 |
| `notice` | `level`, `text` | 配置、连接、播报等提示，设备显示能力取决于当前 UI |
| `pong` | — | 心跳应答 |

`need_user` 是任务管理器状态，在线路中通常通过 `question` 表达；终态通过 `result` 表达。`audio_id` 是音频匹配号，不表示成功、播放进度或任务 ID。

## 录音事务

一段录音按 `audio_start → 二进制 PCM → audio_end` 串行发送。设备用发送锁防止 PTT 与 VAD 音频互相交错，`cancel` 可穿插；上传失败可发送 `audio_abort`。主机只接收与当前录音 ID 匹配的结束或放弃消息。

- `task`：普通任务录音，后台转写成功后以录音 ID 提交任务。
- `wake`：用于唤醒词、后续指令以及执行中的语音取消；普通执行阶段的无关语音不生成新任务。
- `answer`：只续接当轮原任务，不作为新任务执行。必须匹配已经允许听取的问题，不能用旧回答恢复下一轮。

主机接收缓冲默认上限为 60 秒（`BRIDGE_MAX_RECORD_MS`）；这不是当前设备的实际录音时长。当前固件 PTT 缓冲为 10 秒，VAD 单段最多约 5 秒，连续静音 900 毫秒后提交。仅提高主机上限不会扩大固件缓冲。

## 语音澄清与确认

```text
Agent 返回问题
  → TaskManager 保持 need_user，占住手机队列
  → question + speak + PCM
  → 设备说话动画，暂停收音
  → 设备实际播完，排空 I2S 最后语音，发送 speak_done
  → 主机校验任务、轮次、audio_id 与设备归属
  → listen
  → 设备显示“请说话”，采集回答
  → audio_start(mode=answer) + PCM + audio_end
  → status(recognizing)，暂停继续采集
  → 千问 ASR → 原任务回答历史 → status(processing) → 下一轮执行
```

示例控制帧（每行一条消息，音频数据省略）：

```jsonl
{"v":"1","t":"question","request_id":"r-demo-1","question_seq":1,"text":"要冷的还是热的？","expect":"audio"}
{"v":"1","t":"speak","audio_id":12,"request_id":"r-demo-1","question_seq":1,"text":"要冷的还是热的？","format":"pcm_s16le","bytes":64000}
{"v":"1","t":"speak_done","request_id":"r-demo-1","question_seq":1,"audio_id":12,"ok":true}
{"v":"1","t":"listen","request_id":"r-demo-1","question_seq":1}
{"v":"1","t":"audio_start","request_id":"w-demo-2","mode":"answer","answer_to":"r-demo-1","question_seq":1}
{"v":"1","t":"audio_end","request_id":"w-demo-2","mode":"answer","duration_ms":1200}
```

回答不要求唤醒词。主机不能用“音频发送完毕”或预计播放时长代替设备回执。空转写或识别失败会再次发送本轮 `listen`；合成失败、静音或明确播放失败时给出提示后进入听取，手机任务继续暂停。

`question_seq` 每次追问递增，旧回执、取消后的回答及其他设备的回答不能恢复当前任务。文本回答保留兼容性，传入 `question_seq` 可防止串轮；新固件语音回答必须携带轮次。当前播放期间不收音，物理键长按可取消正在播报的问题。

## 取消与安全收尾

物理录音键长按 2 秒发送 `cancel`。设备可收音时，“小D 小D，取消任务”由主机优先匹配，兼容“小弟小弟”“小滴小滴”等识别；完整控制短语不交给模型决策，否定或引用不直接当成取消。

接受取消后，主机先发 `stop_speak`、`status:cancelling`，阻止旧转写、未派发动作和旧 TTS 生效。direct 后端等待已派发的有界手机调用返回，Hermes 后端等待本地进程树清理；多目标全部收尾后才发终态和播报。

取消只停止后续动作，已完成或已派发的外部动作不承诺撤销。已经取得成功结果时保留成功；无法确认清理结束时返回 `unknown` 并暂停手机通道，人工核对后重启 Bridge。`unknown` 本身不等于一律暂停，明确启动拒绝也不应误分类为通道故障。

## 幂等、重连与恢复

设备以随机启动前缀和递增序号生成录音 ID。主机在本进程内缓存任务；重复上传或结束消息返回已有状态，不重跑手机动作，也不打断同一问题的当前播放。已经听取时可重发 `listen`。

普通连接断开后固件约 3 秒重连。主机仍运行且原任务尚在等待时，重连会重新播报未解决的问题；设备不重发旧录音。进程重启会丢失内存任务和去重记录，不会自动从旧点击或发送动作处继续。网络断开与重试间隔受底层错误和网络状态影响。
