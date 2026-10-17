# Mini Agent Assistant

通过 ESP32-S3 语音终端下达手机任务，由 Mac 上的 Bridge 调用千问理解和整理任务，再通过 iCTRL 操作 iPhone，最后由设备播报结果。

```text
ESP32 录音 → Bridge → 千问 ASR → 任务队列
                                  ↓
                 Pi Durable / SQLite → 千问工具执行循环
                                  ↓
                            iCTRL → iPhone
                                  ↓
                     核验结果 → 千问 TTS → ESP32
```

默认使用 `pi` 后端：Python Bridge 管理语音、任务队列及安全手机工具，常驻 Node 中的 Pi Durable 管理模型循环、上下文和 SQLite 持久状态。所有模型请求仍使用千问并关闭思考模式。`direct` / Hermes 保留为显式回退后端，定时任务尚未接入。

## 当前状态（2026-10-17）

- 千问云端 ASR、模型调用和 TTS 已接通；已实测语音读屏、打开淘宝进入闪购，以及语音澄清后继续原任务。
- 支持物理键长按取消和“小D 小D，取消任务”；取消先停止后续动作、完成安全收尾，再回报结果。
- 固件 `0.2.5` 待机时循环眨眼，支持语音澄清后继续原任务；完整指令和回答支持 30 秒录音、1.2 秒句末静音判定，截断片段不执行。
- 完整语音识别并持久化入队后立即播报“我已经收到了”，确认短语预合成缓存；后台继续规划和执行。
- Pi 会分析工具错误、诊断手机状态，连续失败或画面无进展时重新规划剩余步骤；结果未知的动作保持暂停，避免重复执行。
- 动作超时后先按唯一编号查询 Runner 的持久回执，确认完成便继续原任务；暂未确认时保留任务进度等待恢复。需要同时部署支持 `operation_receipts` 的 Runner，见 [运行时说明](host/pi/README.md)。
- 每轮执行上限已提高到 **3000 步、6 小时**，不含等待用户回答；已部署，尚未完成 3000 步长任务压力测试。
- Pi 保存任务、追问与明确要求记住的偏好/事项，重启后恢复；未确认的手机动作不会自动重放。迁移与运维见 [Pi 运行时](host/pi/README.md)。
- 完整咖啡选购、语音微信发送、邮件总结和定时检查仍需独立验收。详细记录见[项目总结](docs/PROJECT-SUMMARY.md)和[开发计划](docs/plan.md)。

## 文档与目录

| 路径 | 内容 |
|---|---|
| [项目总结](docs/PROJECT-SUMMARY.md) | 今日改动、实测结果、限制和排错记录 |
| [架构](docs/architecture.md) | 任务整理、工具执行、串行调度、确认和取消 |
| [终端协议](docs/protocol-v1.md) | WebSocket 控制帧、音频事务、播放回执 |
| [开发计划与验收](docs/plan.md) | 当前完成状态、变更记录和待验收项 |
| [iCTRL 安装与恢复](docs/ictrl-setup.md) | Runner 构建、启动、配对和快速启动扩展 |
| [固件说明](firmware/README.md) / [构建烧录](firmware/ds-bot/README.md) | 开发板、按键、音频和固件维护 |
| `host/bridge/` | 主机服务及千问、iCTRL 适配 |
| `host/pi/` | 正式 Pi Durable 运行时、依赖锁和测试 |
| `host/tests/` | 本地回归测试 |

## 主机快速开始

先启动 iPhone 上的 iCTRL Runner、开启 Remote Access，并确认手机已解锁且与主机同网段。安装和恢复步骤见 [iCTRL 文档](docs/ictrl-setup.md)。

在项目根目录创建 `.env`，新环境可复制 [.env.example](.env.example)，填写 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL` 和 `ICTRL_HOST`。

```bash
cd host
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
# 另需 Node >=22.19.0；安装正式运行时的固定版本依赖
npm --prefix pi ci --ignore-scripts
./.venv/bin/python -m bridge.cli doctor
# 首次使用或配对失效时执行；在手机通知中批准
./.venv/bin/python -m bridge.cli pair --host <手机IP>:8200
# 只整理任务，不操作手机
./.venv/bin/python -m bridge.cli plan "淘宝闪购订杯咖啡"
# 启动语音网关
./.venv/bin/python -u -m bridge.cli serve
```

文字诊断可用 `bridge ask "读取当前手机页面"`；先停止常驻 Bridge。Pi 数据目录有独占锁，同时启动 Bridge / CLI 会被拒绝。修改配置后，先停止旧 Bridge 并等待安全退出，再启动一个新实例。显式切换回退后端前也必须先停服，不能通过另设数据目录并行操作同一手机。

`doctor` 检查本地配置和手机连通性；云端模型权限需实际请求验证，手机 UI 授权也应以实际读屏验证。

## 使用流程

空闲时按住左右任一录音键说话，松开提交；也可说“小D 小D”唤醒后下达任务。中间电源键短按开关屏幕，屏幕触摸不触发录音。

唤醒后 20 秒内开始说出指令，一次唤醒提交一条完整任务。PTT、完整指令和回答最多录音 30 秒，达到上限会提示重说；执行中的取消监听仍保持短片段，避免降低取消响应速度。完整识别并持久化接收任务后，设备先播报“我已经收到了”，任务同时在后台执行；这句话表示收到指令，完成结果另行播报。

需要补充信息时，手机任务保持暂停，精灵进入说话状态播出问题。**等问题播完并显示“请说话”后直接回答**，无需再次唤醒或按键。说完停顿后上传识别，成功则继续原任务；识别为空或失败则重新收音。多轮追问重复此流程，等待期间不切换到其他排队任务。

任务处理中或等待回答时可持续按住左右任一录音键 **2 秒**取消，不用松手；也可在设备能够收音时说 **“小D 小D，取消任务”**，兼容“小弟小弟”“小滴小滴”等识别结果。设备播报期间暂停语音采集，此时可用物理键取消。

取消先停止识别、未派发动作和旧播报，再等待已派发操作安全返回。已发生的手机操作不会自动撤销；远端动作仍未确认时保留其编号，后续手机任务先核实同一回执。本地调用无法清理或旧版动作无法核实时保持保护，恢复步骤见 [Pi 运行时](host/pi/README.md)。已经确认成功的结果不会被改报为取消。

## 千问配置

以下是代码和 `.env.example` 的默认值；今天已部署的主模型为 `qwen3.8-flash`，TTS 音色为 `longanfengyue`，不会因更新文档被覆盖。

| 环境变量 | 用途 / 默认值 |
|---|---|
| `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` | 千问凭据与 OpenAI 兼容地址，必填，URL 填到 `/compatible-mode/v1` |
| `BRIDGE_AGENT_BACKEND` | `pi`；`direct` / `hermes` 为显式回退，无自动跨后端重试 |
| `BRIDGE_PI_NODE` / `BRIDGE_PI_DATA_DIR` | `node` / `~/.dayu/pi`；Node 路径和本地私有数据目录 |
| `QWEN_MODEL` | `qwen-flash`，模型调用固定 `enable_thinking=false` |
| `QWEN_MAX_STEPS` | 每轮执行最多 **3000 次模型执行请求**；一次工具可能合并多个手机 RPC |
| `AGENT_TIMEOUT` | 每轮执行 **21600 秒（6 小时）**，包含规划和执行，不含等待用户回答 |
| `QWEN_REQUEST_TIMEOUT` | 单次模型请求 30 秒 |
| `QWEN_MAX_OUTPUT_TOKENS` | 单次执行输出上限 384 token |
| `QWEN_PLAN_MAX_TOKENS` / `QWEN_PLAN_TIMEOUT` | 规划输出 768 token，总请求预算 8 秒；格式修正最多一次且共用预算 |
| `PHONE_RPC_TIMEOUT` | 手机请求基础超时 8 秒；`page` / `page_source` / `screen_text` 至少等待 18 秒 |
| `PHONE_RECOVERY_TIMEOUT` | 原动作回执核实窗口 45 秒；只查询，不重发动作 |
| `PHONE_SCREEN_CHARS` | 精简读屏上限 6000 字符；完整快照保留在本地 |
| `PHONE_FAST_LAUNCH` / `PHONE_READY_READS` | 默认 `true` / `3`，前台快速启动后读屏，加载时有界重读 |
| `QWEN_ASR_MODEL` / `QWEN_ASR_LANGUAGE` / `QWEN_ASR_TIMEOUT` | `qwen3-asr-flash` / `zh` / 15 秒 |
| `QWEN_ASR_API_KEY` / `QWEN_ASR_BASE_URL` | 可选，留空复用 `DASHSCOPE_*` |
| `QWEN_TTS_MODEL` / `QWEN_TTS_VOICE` / `QWEN_TTS_TIMEOUT` | `qwen-audio-3.0-tts-flash` / `longanxiaoxin` / 15 秒 |
| `QWEN_TTS_API_KEY` / `QWEN_TTS_BASE_URL` | 可选，留空复用主凭据和服务域名；可指定完整 `wss://.../api-ws/v1/inference` |
| `ICTRL_HOST` | iPhone Runner 地址，默认 `127.0.0.1:8200`，跨设备需修改 |
| `BRIDGE_BIND` / `BRIDGE_PORT` | `0.0.0.0` / `8765` |
| `BRIDGE_TOKEN` / `BRIDGE_TOKEN_FILE` | 与固件一致的共享密钥，或已有密钥文件路径 |

配置优先级：**`host/config.json` > 已导出的环境变量 > 项目根 `.env` > 代码默认值**。`.env` 从项目路径读取，不受启动目录影响。用户补充后重新规划并重新计算本轮步数和执行时限，任务 ID 保持不变。

ASR 将 16kHz PCM 在内存中包装成 WAV，直接请求千问云端；TTS 通过原生异步 WebSocket 输出同格式 PCM。当前链路不依赖 Whisper、macOS `say` 或 ffmpeg。模型、语音权限未配置时启动失败，不自动回退到其他服务。

## 意图整理与手机执行

复杂口语先整理为目标、根应用、应用内入口、步骤、限制、合理补全和待确认信息，通过校验后才调用 iCTRL。明确的当前界面只读任务走本地快捷规范，省去额外规划请求。

“淘宝闪购订杯咖啡”默认解释为 **淘宝 App → 闪购入口**；明确指定独立 App 时遵循新指令。位置、距离、商品和价格由手机页面核实，种类、冷热和预算缺失时询问。购物在提交订单和付款前停止。

可以说“记住我喜欢热美式，不加糖”或“还记得我的咖啡偏好吗”。明确的新要求优先于旧记忆，“今天”偏好只当天生效。复购只参考经过实际订单详情核验的历史订单；没有记录时先到手机查证或澄清，不会把购物车当作已下单。

打开应用合并首次读屏，点击、输入和翻页合并后续读屏；省略内容明确标注，必要时可补读本地完整快照。成功结果需要实际证据，工具层限制只读任务和根应用身份。具体约束见[架构](docs/architecture.md)。

## 部署、日志与验证

长句录音与唤醒会话修复要求 **Bridge 与固件至少 0.2.2 配套更新**，新的拾音策略使用固件 0.2.5。iCTRL 的 `launch_mode:"foreground"` 和 `operation_receipts` 已发布到独立的 [iCTRL fork（50bcec4）](https://github.com/kejun/iCTRL/tree/50bcec4)，需构建部署对应 Runner；仅更新本仓库不会更新手机 App。步骤见 [iCTRL 文档](docs/ictrl-setup.md)和[固件构建文档](firmware/ds-bot/README.md)。

- Bridge / 设备 / Runner 日志：`~/.dayu/logs/bridge.log`、`serial.log`、`ictrl-runner.log`。
- Pi 状态与记忆：`~/.dayu/pi/assistant.sqlite`；规划和完整界面记录位于同目录的 `plans/`、`traces/`。回退 direct 仍使用 `.runtime/direct/`。
- 今日验证报告：`.runtime/*report.json`，索引见[项目总结](docs/PROJECT-SUMMARY.md)。这些是当前机器的本地记录，不随 Git 分发。
- `.env`、`host/config.json`、`.runtime/` 和本地固件配置被 Git 忽略；文档不保存 API Key、Wi-Fi 密码或配对密钥。

本地回归不调用真实云端或手机：

```bash
cd host
./.venv/bin/python tests/test_tasks.py
./.venv/bin/python -m unittest discover -s tests -v
npm --prefix pi test
# 可选：真实 Hermes CLI 对接本地模拟模型
DAYU_TEST_HERMES=1 ./.venv/bin/python -m unittest discover -s tests -p test_hermes_qwen.py -v
```

固件原生策略测试：在 `firmware/ds-bot` 执行 `sh tools/test-input-policy.sh` 和 `sh tools/test-vad-policy.sh`。当前主机验证为 Python、调度与 Node 共 **210 项通过、1 项可选联调跳过**；Runner 持久回执检查、iOS 构建及实机超时恢复通过，详见[验收计划](docs/plan.md)。
