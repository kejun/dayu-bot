# Mini Agent Assistant：项目总结与排错记录

更新：**2026-10-18**。本文汇总 2026-10-17 的实现、部署及本机验收，并补充配套 Runner 的发布位置。当前入口见 [README](../README.md)，协议细节见[终端协议](protocol-v1.md)，历史安装记录位于本文后半部分及 [iCTRL 文档](ictrl-setup.md)。

## 当前产品与架构

对 ESP32 说出手机任务，Mac 上的 Bridge 调用千问完成识别、意图整理和逐步执行，通过 iCTRL 操作真实 iPhone App，再把问题或结果交给千问 TTS，在 ESP32 播放。

```text
ESP32 PCM → Bridge / Qwen ASR → 串行任务队列
                                     ↓
                             整理并校验任务规范
                                     ↓
                      Node / Pi Durable / 千问工具循环
                                     ↓
                    iCTRL worker → 加密 JSON-RPC → iPhone
                                     ↓
                       核验结果 / 追问 → Qwen TTS → ESP32
```

默认后端已迁移为 `pi`：常驻 Node / Pi 管理模型循环、SQLite 状态、上下文和用户记忆，Python 继续管理语音和有界手机工具。direct / Hermes 保留为显式回退，原有 cron 接入尚未完成。微信、邮件、外卖都通过手机 App 界面完成，没有新增业务专用接口。

## 2026-10-17 完成的改动

| 方面 | 当前实现 |
|---|---|
| 统一千问 | ASR、规划/执行模型、TTS 统一使用项目配置的千问服务；模型调用关闭思考 |
| 默认执行路径 | Bridge 与 Pi 常驻，以管道调用固定手机工具；所有模型仍为关闭思考的千问 |
| Pi 持久状态 | 任务去重、追问、回答对应关系、结果和设备隔离的明确偏好/事项；中断动作不重放 |
| iCTRL 提速 | 本机 Runner 支持 `launch_mode:foreground`；打开与首次读屏、动作与后续读屏合并 |
| 任务整理 | 纠错补全目标、根应用和入口、步骤、限制、假设及待确认项；回答后重新规划 |
| 应用入口 | “淘宝闪购”默认淘宝内闪购；工具层核对应用身份，保留明确独立 App 指令 |
| 计划格式修复 | 空辅助字段允许省略；关键错误最多修正一次，两次合计 8 秒请求预算 |
| 语音确认 | 暂停原任务 → 说出问题 → 实际播放回执 → 开启收音 → ASR → 继续同一任务 |
| 安全取消 | 任一录音键长按 2 秒或语音取消；丢弃旧音频与迟到转写，等待已派发调用收尾 |
| 启动失败分类 | 明确应用启动拒绝不再误触发“需要重启网关”；真正未知的动作/清理仍保留保护 |
| 出错后继续 | 已结束的 AX 查找失败交给 Pi 改用 OCR，以设备状态核验前台身份而不补读 AX 树；连续错误或无进展时重新规划剩余步骤，最多三轮 |
| 故障诊断 | 未确认动作冻结后续 UI，但允许 Pi 读取设备状态和 Runner 日志；本地调用未清理时不新增请求 |
| 动作结果核实 | Pi 和 Runner 在派发前保存唯一动作编号；超时查询原回执，确认成功后继续同一任务；同编号不重复执行 |
| 等待与恢复 | 暂未核实时保留任务进度；Pi 结束或询问前再核实；带编号的在途动作在 Node 中断后可经正式队列核实并接续 |
| 长任务摘要 | 普通执行仍 384 token；上下文摘要独立使用 3276 token，修复执行到一半因摘要截断而停止 |
| 快速收到反馈 | 完整 ASR 持久化入队后播报“我已经收到了”，预合成缓存与后台任务并行；两次实测上传完成到确认帧 378 / 560 ms |
| 拾音与停顿 | 固件 0.2.5：34.5 dB 增益、500/250 RMS 门槛、连续两帧起录、200 ms 句首预录；完整指令静音 1200 ms 提交 |
| 长任务预算 | 每轮 3000 步、21600 秒；模型请求 30 秒，手机基础 8 秒、翻页/读屏至少 18 秒，原动作回执核实 45 秒 |
| 设备恢复 | 固件正常 WebSocket 关闭后重连；Bridge 和 Runner 已重启/恢复，固件已烧录 |

## 当前配置与部署快照

这是 2026-10-17 的本机记录，不是持续在线保证。以后以 `.env`、启动日志和 `bridge doctor` 为准。

| 项目 | 本机配置 / 已验证环境 |
|---|---|
| Bridge 后端 | `pi`，Pi Durable 1.3.0 / Node v24.20.0 |
| Pi 数据 | `~/.dayu/pi/`，正常停服后备份整个目录；恢复流程见 [运行时说明](../host/pi/README.md) |
| 主模型 | `qwen3.8-flash`；代码/示例默认仍是 `qwen-flash` |
| ASR | `qwen3-asr-flash`，云端接口，16kHz PCM 在内存中包装 WAV |
| TTS | `qwen-audio-3.0-tts-flash`，当前音色 `longanfengyue`；代码/示例默认 `longanxiaoxin` |
| 执行预算 | `QWEN_MAX_STEPS=3000`，`AGENT_TIMEOUT=21600` |
| 规划预算 | 768 token / 8 秒，最多一次格式修正且共用预算 |
| 单次请求 | 模型 30 秒、iCTRL 基础 8 秒（`page` / `page_source` / `screen_text` 至少 18 秒）、ASR/TTS 各 15 秒；另有 45 秒回执核实窗口 |
| Bridge 地址 | 本机 `192.168.1.13:8765`，设备走 Wi-Fi WebSocket |
| iPhone Runner | 最近测试地址 `192.168.1.18:8200`，skill `1.98.12`，能力含 `operation_receipts`；地址受 DHCP 影响 |
| Runner 源码 | 独立 [iCTRL fork，50bcec4](https://github.com/kejun/iCTRL/tree/50bcec4)，本机 `/Users/haoyu/code/ictrl`；快速启动与动作回执需单独构建部署 |
| ESP32 | Waveshare ESP32-S3-Touch-AMOLED-2.16，16MB Flash / 8MB PSRAM |
| 显示/音频 | CO5300 480×480、LVGL 9、ES7210 / ES8311、16kHz 单声道 16bit PCM |
| 固件 | 握手版本 `0.2.5`，ESP-IDF 5.5.5 / Python 3.13 编译、烧录及连接确认通过 |

主机 `.env` 配置优先级低于已导出的环境变量和 `host/config.json`。Key、服务地址、Wi-Fi 密码和配对密钥不复制进文档；当前用户配置不会因示例默认值而被覆盖。

## Pi 正式迁移验收

正式 Bridge 已切换 `BRIDGE_AGENT_BACKEND=pi` 并重启；迁移验收时 ESP32 为 0.2.0，最新拾音优化后为 0.2.5。固件协议仍为 v1；规划与手机安全校验由 Python 承担，Pi 接管模型循环和持久状态。

| 用例 | 本机结果 |
|---|---|
| 记忆保存、重启召回、删除 | 真实千问每轮一次调用，约 1.2–1.8 秒；删除后不再召回旧偏好，测试在隔离用户和数据目录进行 |
| 实体设备澄清 | 实际问题播放完成后收音，Mac 扬声器回答由 ESP32 麦克风采集、千问识别后继续真实手机读屏；最终只问一次冷热 |
| Bridge 重启时仍在等回答 | 关闭并重启常驻 Bridge，重连重播相同轮次的问题，回答后沿用原 Pi conversation 完成 |
| 跨重启重复请求 | 约 11 毫秒返回同一结果和证据，不重跑手机 |
| 语音取消等待中的任务 | 上传“小D 小D，取消任务”录音至返回取消约 0.396 秒；在途 RPC 排空与失败保护由故障回归另验 |
| 简单语音读当前屏幕 n=3 | 3.763 / 3.525 / 3.269 秒，均两次执行模型请求、成功证据；已去掉这些表述的多余规划 |
| 语音打开淘宝并读屏 n=1 | 6.488 秒，两次执行模型请求；真实 App，当时淘宝已运行，未点击商品或交易 |

语音读屏/打开耗时从录音上传到结果帧，不含用户说话与结果播放；不能直接与只测模型执行的早期 2–3 秒数据相减。样本少，不构成 P95 或长期性能保证。完整咖啡选择、真实历史订单复购与 3000 步长任务仍未验收。

Pi 校验同时修复了已回答问题被再次规划为前置追问、读“当前手机页面”的语序漏过快捷规则，以及模型误用本任务快照编号导致结果未确认。快照编号只在匹配本轮、当前修改代次的真实成功回执时映射；其他不明证据保持 unknown。纯文字“已经记住”不能代替真正的记忆提交。

本地报告：`.runtime/pi-migration-final-report.json`（汇总）、`pi-final-phone-report.json`（最终手机复测）、`pi-migration-physical-final-report.json`（实体语音）、`pi-migration-cloud-report.json`（记忆）。`pi-production-report.json` 保留首次实机检查，包括触发修复的 unknown，不覆盖成成功记录。通道恢复、备份和显式回退见 [运行时文档](../host/pi/README.md)。

## 用户交互

空闲时按住左右任一录音键说话、松开上传，或“小D 小D”唤醒后下达任务；中间键短按开关屏幕，触屏不触发录音。

需要澄清或确认时，TaskManager 保持 `need_user` 并占用手机队列，精灵显示说话状态。设备实际播完问题、排空 I2S 语音尾部后发送 `speak_done`；主机核对任务、轮次、音频 ID 和设备归属，再发送 `listen`，精灵显示“请说话”。此时直接回答即可，无需再次唤醒。

VAD 检测到停顿后上传回答，转写成功后继续原任务；为空或失败时重新收音，不能默认同意。新一轮问题增加 `question_seq`，迟到回答和旧回执不会串入下一轮。播放期间不收音，物理键长按仍可取消。

当前设备 PTT 缓冲 30 秒，完整指令/回答最多 30 秒、静音 1200 毫秒后提交；空闲唤醒及取消监听保留 5 秒、500 毫秒短档。唤醒后 20 秒内开始采集，`command` 绑定本次唤醒会话并仅消费一次，避免长句识别返回时过期。截断的任务/回答提示重说，不执行其前半句；主机 60 秒接收上限不代表设备支持 60 秒连续录音。完整转写并持久化接收后先播报“我已经收到了”，后台继续执行，完成结果另行播报。

取消时先清旧播报、阻止新动作，再等已派发调用返回；已发生的操作不能撤销。远端动作未结束时保留其编号，后续手机任务先查询同一回执；本地进程无法清理时保留 `unknown` 和人工核对保护。结果已确认成功时不改报取消。

## 实测范围与证据

| 验证 | 结果 | 本机报告 |
|---|---|---|
| 语音读当前界面 | 真实 Bridge / ASR / 模型 / 手机，4.86 秒返回结果 | `.runtime/direct-e2e-report.json` |
| 语音打开淘宝并读屏 | 同链路 7.88 秒 | `.runtime/direct-open-report.json` |
| 执行期间语音取消 | 上传取消录音后 0.365 秒收到取消结果 | `.runtime/direct-voice-cancel-report.json` |
| 淘宝内闪购入口 | 启动淘宝、进入闪购并读屏，23.46 秒，未进入完整选购 | `.runtime/intent-entry-report.json` |
| 当前页面快捷整理 | 3.74 秒，额外规划模型调用为 0 | `.runtime/intent-fast-read-report.json` |
| 计划字段兼容 / 自动修复 | 真实模型兼容验证通过；缺关键字段故障注入后 4.37 秒修复 | `.runtime/plan-compat-report.json`、`plan-repair-live-report.json` |
| 明确应用拒绝后的通道恢复 | 应用普通失败后，下一次只读任务可成功 | `.runtime/launch-fix-report.json` |
| 两轮设备语音确认 | 实体设备播报回执后，经麦克风接收“热的”“可以”，同一任务完成 | `.runtime/clarification-hardware-report.json` |
| 正式模型与手机确认链路 | 问冷/热 → 麦克风回答“热的” → 原任务恢复，仅一次 `page_source` → 成功 | `.runtime/clarification-production-report.json` |
| 千问 TTS 设备播放 | 原生 PCM 合成与设备接收播放通过 | `.runtime/qwen-tts-device-report.json` |
| 失败任务对应的只读复验 | 股市打开/读屏 7.65 秒；微信精确联系人搜索 36.74 秒，未发送消息 | `.runtime/failure-20261017/real-report.json` |
| 0.2.2 长句声学链路 | Mac 播放含 1.2 秒停顿的 6.9 秒指令，设备录成 8.48 秒完整片段；保留禁止交易限制，仅生成一个任务并成功 | `.runtime/failure-20261017/physical-long-report.json` |
| Runner 看门狗超时后接续 | 实际翻页在 6 秒返回超时、7.274 秒完成；原动作只派发一次，核实后完成推荐流 5 屏浏览（29 次模型请求、一次成功摘要） | `.runtime/operation-live-20261017/report.json` |
| 快速语音确认与最终读屏 | 最终复测上传完成后 560 ms 收到“我已经收到了”确认帧，7.276 秒收到真实微信当前页结果 | `.runtime/operation-voice-final.json` |

表中耗时为相应测试记录口径，通常不含人说话、等待回答和结果播放。两轮确认使用测试 Agent 验证语音/设备状态；正式确认测试另外覆盖真实主模型和实体手机。报告与界面记录位于本地 `.runtime/`，不随 Git 分发。

快速确认测试向正式 WebSocket 上传合成语音，实际调用云端 ASR、手机及实体 ESP32 播放；378 / 560 ms 从录音上传结束计时，到收到确认帧为止，不包括句末检测、录音上传或扬声器开始发声。固件 0.2.5 的远距离及噪声环境拾音仍待标定，不能由该数字推断声学识别距离。

同一 `qwen3.8-flash` 和同一前台页面的少量对照：Hermes 路径 10.58 秒，direct 2.64 / 2.53 秒。对同一已运行淘宝 App，启动加首次读屏从普通模式 3.77 / 1.78 秒变为快速模式 1.15 / 1.03 秒。完整记录在 `.runtime/backend-benchmark.json`、`launch-benchmark.json`；不能推断所有 App、冷启动或完整点单都会相同比例提速。

最新故障修复后，本地主机 unittest 发现 **165 项（164 通过、1 项可选 Hermes 跳过）**，独立任务调度 9 项、正式 Node 运行时 37 项通过，合计 **210 项通过、1 项跳过**。覆盖真实 Python↔Node 管道、SQLite 恢复、SIGKILL 后原动作核实接续、取消排空、证据失效、预算、查找失败、输入框定位和语音会话边界。Runner 持久回执 Swift 检查及 iOS 构建通过；按键/VAD 原生策略及 0.2.5 构建烧录通过。早期 direct 性能数据保留为历史对照；Pi 不承诺自动缩短云端推理和手机耗时。

## 已知故障与对应修复

| 症状 | 已定位原因与处理 |
|---|---|
| 应用打开失败却播报重启网关 | 原生明确拒绝被归为未知；仅对方法、目标标识、错误码、固定拒绝消息完整匹配时按普通失败处理 |
| “任务整理失败” | 模型省略空辅助字段被严格字段校验拒绝；允许安全默认值，保留关键校验，必要时有界修正一次 |
| 问题播出后回答无效 | 旧语音分支只检查取消；已接入按任务与轮次绑定的回答通路 |
| 播报结束与收音没有衔接 | 增加 `speak_done` / `listen`，不再用主机发送完成代替设备播放完成 |
| 任务到 16 步中止 | 原 `QWEN_MAX_STEPS=16` 生效；已改 3000，并将每轮 180 秒放宽到 6 小时 |
| 设备在 Bridge 重启后不恢复 | 正常 WebSocket CLOSE 也启用约 3 秒重连；进程重启后不自动重跑旧任务 |
| 股票与微信任务在查找后失败、朗读错误 JSON | Pi 迁移误将明确元素未找到视为终止；已改为可恢复查找，连续失败先重新规划再追问。补齐股市别名及微信实际 StaticText/无标签 TextView 定位，未知动作不重试 |
| 长句被拆开、后半句丢失 | 旧 5 秒/900 毫秒策略拆句，忙碌中后半句被忽略；改为分档采集、截断保护及一次性唤醒会话，长句不再按 ASR 完成时间判过期 |
| 一次超时导致整个通道暂停、后续任务失败 | 远端动作未知被混同为本地进程清理失败，保护状态又被 Python FIFO 恢复；现以持久动作编号核实结果，未核实时保留进度，完成后接续，不再直接失败 |
| 长任务在上下文摘要阶段中止 | SDK 摘要误用普通执行的 384 token 上限；独立使用 3276 token，已在 29 次模型请求实机任务中成功压缩并继续 |

12:11 的咖啡任务 `w1af3b73f-6-cmd` 在回答“热的”后继续，确实在原第 16 步上限终止，未确认完成选购。提高预算后尚未把该完整任务重新验收为成功。

## 待验收与下一步

- 重新验证完整咖啡候选选择与结果核查；仅入口成功不代表点单完成，购物仍停在提交/付款之前。
- 语音微信发送、邮件读取总结、长任务/大步数稳定性和复杂自然停顿。
- 更系统的断网、Runner 授权失效、动作在途取消与恢复；已有单测/部分链路验证不能替代全部故障实机验收。
- `hermes cron` 或其他既有调度接入同一 Bridge 任务路径；当前只是 TaskManager 具备串行与合并能力。
- Runner 长期 UI 授权维持；当前依赖 Mac/Xcode 测试会话，尚无脱离 Mac 的长期运行验收。

U-Face 精灵状态动画已经接入设备，不再属于“桌宠未开始”；独立电脑桌宠不在当前范围。

## 维护入口与日志

| 位置 | 用途 |
|---|---|
| [README](../README.md) | 配置与服务启动命令 |
| [iCTRL 安装与恢复](ictrl-setup.md) | Runner 构建、授权恢复、配对和快速启动参数 |
| [固件构建](../firmware/ds-bot/README.md) | ESP-IDF、设备配置、串口互斥和烧录后恢复记录器 |
| `~/.dayu/logs/bridge.log` | ASR/TTS、模型阶段、手机 RPC、问题轮次和错误 |
| `~/.dayu/logs/serial.log` | 固件状态、上传、播放回执、VAD 与按键 |
| `~/.dayu/logs/ictrl-runner.log` | iPhone 测试会话 |
| `~/.dayu/pi/assistant.sqlite`、`plans/`、`traces/` | 正式任务与记忆、任务规范、手机快照及事件；位于同一 Pi 数据目录 |
| `.runtime/direct/<trace_id>/` | 显式 direct 回退任务的规范、完整快照和 `events.jsonl` |

重启 Bridge 先停止旧实例并等候安全退出，再启动新实例；不要依赖强杀后立即重跑未知手机动作。固件烧录先停对应串口记录器，完成后用带 `pyserial` 的 ESP-IDF Python 恢复。主机虚拟环境路径不要先解析到系统解释器，否则会绕过虚拟环境依赖。

## 历史排错记录（2026-10-16 至 2026-10-17 早期）

以下保留当时的工具、版本、网络与硬件故障。旧 Whisper / macOS `say` / Hermes 默认路径已被上文新实现替代；第三方版本和服务状态是当时记录，不能当作持续有效的安装建议。

### A. ESP32 / ESP-IDF

#### A1. 串口设备完全识别不到
- **现象**：`/dev/cu.*` 只有蓝牙，`ioreg` 无 USB 设备；但端口日志显示"通电了"。
- **根因**：用了 **USB-A ↔ Type-C 转换头**，只通电源不通数据（D+/D- 没接）。
- **日志特征**：`AppleUSBHostPort::cableChangeOccurred: powering on` 但**没有** `enumerateDeviceComplete`。
- **解决**：换**直连的 Type-C 数据线**。判断方法：`ioreg` 是否出现设备节点。

#### A2. ESP-IDF 工具链下载失败（SSL UNEXPECTED_EOF）
- **根因**：`github.com` 走本地代理不稳定。
- **解决**：`export IDF_GITHUB_ASSETS="dl.espressif.com/github_assets"`（乐鑫官方 GitHub 镜像）。
- 备用镜像：`dl.espressif.cn/github_assets`。

#### A3. ESP-IDF 子模块版本错乱（mbedtls 4.x → mbedcrypto 报错）
- **现象**：`CMake Error: Missing esp-mqtt submodule`；后来 `Cannot specify link libraries for target "mbedcrypto"`。
- **根因**：子模块被 clone 到了**分支最新**（`heads/master`、`heads/mbedtls-4.1.1-idf`），而不是 v5.5.5 记录的提交。
- **解决**：逐个把子模块拉回记录提交（网络不稳时用 `--depth 1` + 重试）：
  ```bash
  git submodule status --recursive | grep '^+'      # 找出不匹配的
  git -C <path> fetch --depth 1 origin <recorded-sha>
  git -C <path> checkout <recorded-sha>
  ```

#### A4. ESP-IDF 不支持 Python 3.14
- **现象**：`ERROR: ESP-IDF Python virtual environment "...idf5.5_py3.14_env" not found`。
- **解决**：做一个 shim 让 `python3` 指向 3.13：
  ```bash
  mkdir -p ~/.idf-python && ln -sf /opt/homebrew/bin/python3.13 ~/.idf-python/python3
  export PATH="$HOME/.idf-python:$PATH"    # 已写进 ~/.zshrc
  ```

#### A5. 烧录失败：串口被后台记录器占用
- **现象**：`A fatal error occurred: Could not open /dev/cu.usbmodem101, the port is busy`。
- **解决**：烧录前 `pkill -f serial_logger.py`。

#### A6. 串口日志会丢
- **现象**：事后读串口什么都读不到。
- **根因**：**没人在读时 USB CDC 的输出会被丢弃**。
- **解决**：起一个后台记录器 `~/.dayu/serial_logger.py` 持续写 `~/.dayu/logs/serial.log`。

---

### B. iCTRL 安装到 iPhone（历史排错）

> 完整排错见 `docs/ictrl-setup.md`。这里只列根因。

#### B1. iLoader 登录 Apple ID：`unknown session (-45025)`
- **根因**：anisette 数据获取失败。公用 anisette 服务器 `ani.sidestore.io` **时好时坏**。
- **解决尝试**：本地自建 anisette（Docker）：
  ```bash
  colima start
  docker run -d --name anisette -p 6969:6969 --restart unless-stopped dadoum/anisette-v3-server
  ```
  iLoader 自定义服务器**必须带 `http://` 前缀**（否则它会自动补 `https://`，连明文服务会失败）：
  `http://127.0.0.1:6969`

#### B2. iLoader `-80045`
- **根因**：Apple GSA 现在**拒绝第三方 anisette 服务器发来的 macOS client info**。
- **解决**：iLoader **2.3.8**（改用 Android 身份）。2.3.7 及更早会报 -80045。
- ⚠️ 2.3.8 起**禁用自定义 anisette 服务器**（所以本地 anisette 只在 ≤2.3.7 有用）。

#### B3. iLoader 2.3.8：`error sending request`（ani.jkcoxson.com）
- **根因**：内置 anisette 服务器 `ani.jkcoxson.com` **直连正常（0.7s）**，但**经 Clash 代理又慢又断**（3–5s，5 次里失败 1 次）。
- **解决**：**关掉系统代理**再登录（Apple 认证流量尤其敏感）。

#### B4. SideStore 登录：`-80045` / `-1005` / `503`
| 错误 | 根因 | 解决 |
|---|---|---|
| `-80045` | GSA 拒绝 macOS client info（含 `com.apple.dt.Xcode` 的会**秒回 503**） | Settings → Advanced → User Customizations → ANISETTE → **Client Info 改成 Android 身份**：`<Pixel 7> <Android;14;UQ1A.240205.002> <com.apple.AuthKit/1 (com.apple.akd/1.0)>`（**末尾不能有换行**，否则报 "empty response (0 bytes)"） |
| `-1005` | 默认 anisette 服务器 `ani.sidestore.io` 不稳 | 开 **On-Device Anisette** |
| `503` | Apple 边缘拦截（Xcode client info / 频率） | 换网络 / 等待 / 换 Apple ID |

- **SideStore 0.6.4 的 release 标题直接写着 "DO NOT USE — SIGN-IN IS BROKEN"**，要用 0.7.0-alpha。
- 相关 issue：#1657（修复，2026-10-15 才合并，尚未进任何发布版）、#1658/#1652（仍复现）。

#### B5. iLoader 装不了 runner（`Invalid bundle: Extension ... is not part of the main app bundle identifier`）
- **根因**：isideload 的 `update_bundle_id()` 要求**扩展 bundle id 是主 App id 的严格子前缀**，而 iCTRL runner 的 `PlugIns/iCTRLUITests.xctest` 与主 App **完全同名**。
- **权宜补丁**：改 `.xctest` 的 `CFBundleIdentifier` 为子 id 再重打包 → **装上了但一启动就崩**。

#### B6. runner 崩溃：`Library not loaded: @rpath/XCTest.framework/XCTest`
- **第一层原因**：**DDI 没挂**（`/System/Developer/Library/Frameworks/XCTest.framework` 不存在）。
- **第二层原因**：iOS 靠 **bundle id 的 `.xctrunner` 后缀**识别测试 runner 并注入 XCTest 环境；**isideload 把 team id 追加到末尾**（`...xctrunner.NBYSH5B496`），后缀被破坏 → 当成普通 App 启动 → 崩。
- **本机当次结论**：该重签名路线未跑通，最终采用保留 Runner 标识的 Xcode 路线。

#### B7. ✅ 最终成功路线：Xcode
```bash
brew install xcodegen
cd ~/code/ictrl
DEVELOPMENT_TEAM=NBYSH5B496 ./Scripts/setup-signing.sh   # 生成 Config/Local.xcconfig
xcodegen generate
xcodebuild build-for-testing -project iCTRL.xcodeproj -derivedDataPath build \
  -scheme iCTRL -destination "generic/platform=iOS" -allowProvisioningUpdates ARCHS=arm64
# 启动 runner（手机需 USB 连接）
xcodebuild test-without-building -project iCTRL.xcodeproj -scheme iCTRL \
  -derivedDataPath build -destination "id=<UDID>" -allowProvisioningUpdates
```
- 免费 Apple ID 就够，`-allowProvisioningUpdates` 会**自动创建证书**。
- 产物 bundle id：`com.openminis.nbysh5b496.ictrl.uitests.xctrunner`（✅ 以 `.xctrunner` 结尾）。
- ⚠️ `fetch-idevice.sh` 结尾的 Python 步骤若被 Xcode 许可错误打断，会缺 `Frameworks/IDevice.xcframework/Info.plist` → 构建报错，需手工补生成。

#### B8. DDI 下载失败（TLS 错误）
- **根因**：iCTRL 的 DDI 从 **GitHub** 下载（`doronz88/DeveloperDiskImage` 镜像），国内直连 `raw.githubusercontent.com` 会 TLS 报错。
- **iOS 27 需要 Cryptex 版 DDI**（5 个文件）。
- **解决**：用 **`afcclient` 的 HouseArrest** 直接写进 App 容器：
  ```bash
  # isideload 会改写 bundle id（后缀是 team id）
  BID=com.openminis.ictrl.NBYSH5B496
  printf 'ls\n' | afcclient --container "$BID" -u "$UDID"     # 先验证可访问
  { echo 'mkdir "Library/Application Support/DDI/Xcode_iOS_DDI_Cryptex"'
    for f in BuildManifest.plist Image.dmg Image.dmg.trustcache Image.dmg.cryptex_info Image.dmg.root_hash; do
      echo "put /path/to/ddi/$f \"Library/Application Support/DDI/Xcode_iOS_DDI_Cryptex/$f\""
    done
  } | afcclient --container "$BID" -u "$UDID"
  ```
- ⚠️ **afcclient 按空格切分参数，含空格的路径必须加引号**。
- 备选：Mac 上转发 `0.0.0.0:7893 → 127.0.0.1:7892` 给手机当代理（`~/.dayu/portfwd.py`）。

#### B9. runner UI 授权失效：`Not authorized for performing UI testing actions`
- **现象**：runner 进程还活着（`/schema` 返回 200），但所有 UI 调用（`page_source`/截图）失败；截图甚至只有 **2×2 像素**。
- **根因**：**UI 测试授权跟着 Mac 的 `xcodebuild` 测试会话走**，会话结束后过一阵就失效。
- **解决**：重跑 `xcodebuild test-without-building`（手机需 USB 连接）。
- ⚠️ **这是当前最大的架构限制**：还不能完全脱离 Mac。待研究（测试会话常驻 / WiFi 调试 / 其他启动方式）。

---

### C. 主机 bridge

#### C1. macOS「本地网络」权限拦截 python/node/ruby
- **现象**：同一台机器上 `curl http://192.168.1.6:8200/schema` 返回 **200**，而 `python3 -c "socket.connect(...)"` 报 **`[Errno 65] No route to host`**；node/ruby 同样失败，`curl`/`nc`/`ping` 正常。
- **根因**：macOS 的**本地网络隐私权限**——系统自带工具被豁免，其他运行时被拦。
- **解决**：`host/bridge/ictrl.py` **把 iCTRL 客户端的 HTTP 层换成 `curl`**（monkeypatch `_post`），加密逻辑不动：
  ```python
  mod._post = _curl_post     # 用 subprocess 调 curl --noproxy '*'
  ```
- **根治办法**：系统设置 → 隐私与安全性 → **本地网络** → 给终端 App 授权。

#### C2. `bridge pair` 报 `client_name is required`
- **根因**：CLI 的 `--name` 默认 `None` → JSON 里是 `"client_name": null`。
- **解决**：默认用主机名 `os.uname().nodename`。

#### C3. 旧 Hermes 默认路径慢到 300 秒超时（先迁至 direct，当前为 Pi）
- **现象**：一句话任务跑 5 分钟超时；`API call failed after 3 retries: Request timed out`。
- **根因**：`hermes prompt-size` 显示每次调用带 **~70KB** 上下文：
  ```
  系统提示 26.9 KB（其中 133 个 skill 的索引 12.4 KB）
  工具 schema 42.8 KB（21 个工具！）
  ```
- **解决**：限定工具集 + 预载 skill + 自动批准：
  ```bash
  hermes -t terminal --skills ictrl-phone-control --yolo -z "<指令>"
  ```
  → 工具 21→2 个、skill 索引 133→1 个，**同一任务 300s 超时 → 10.4s**。
- ⚠️ **参数顺序**：`-z` 后面紧跟 prompt，其他参数要放在 `-z` **之前**（否则 argparse 报 "expected one argument"）。

#### C4. 旧 Whisper 接口的 `'WhisperCpp' object is not callable`（当前已移除）
- **根因**：把 STT **对象**直接传给了 Bridge，但它需要**可调用对象**。
- **解决**：给 `WhisperCpp` 加 `__call__`。

#### C5. bridge 日志空
- **根因**：Python 输出到文件时**缓冲**。
- **解决**：`python -u`。

#### C6. 任务卡在「处理中」不下线
- **解决**：收到 `level=error` 的 `notice` 时把 UI 切到「失败」；prompt 里明确"授权失效立即 failed 不重试"。

---

### D. ds-bot 固件

#### D1. 黑屏 = 不是深度睡眠
- **现象**：板子连上但屏幕全黑，怀疑深度睡眠。
- **真相**：还在跑之前烧的 **`hello_world`**（它不驱动屏幕）。
- **排查方法**：读串口——`Project name: hello_world` / `Restarting in 10 seconds...`。

#### D2. 精灵显示太小
- **根因**：128px 画布里精灵**只占 82×70**（画布 64% 是透明边距）。
- **解决**：从 **480×480 源帧**下采样生成 **256px** 素材（精灵 ~160px，正好 2 倍且不模糊）：
  ```bash
  ./tools/build-uface-256.sh ~/Downloads/U-Face-LVGL-Sprites-Final
  ```
  工具链：ImageMagick 缩放 → 官方 `export-lvgl.cjs --lvgl 9`。
- 上限：源精灵原生 ~300×257 → 最大 480 画布 / 精灵 300px 无损失。

#### D3. 中文乱码 + 字号太小
- **根因**：LVGL 默认 Montserrat **无汉字**；内置 CJK 字体只有 **14/16px**。
- **解决**：用 `lv_font_conv` 从 `Arial Unicode.ttf` 生成 **32px 子集字体**：
  ```bash
  ./tools/build-cjk-font.sh 32
  ```
- ⚠️ **PingFang/Hiragino 是 TTC 格式，转换器不支持**（`Unsupported OpenType signature ttcf`），只能用单体 TTF。
- ⚠️ **字体是子集**：新文案出现新字要补进 `SYMBOLS` 再重跑。
- ⚠️ 商用发布前换成授权明确的开源字体（如思源黑体）。

#### D4. `…` 显示成方块
- **根因**：`…` 是 **U+2026（General Punctuation 0x2000–0x206F）**，而我只加了中文标点区 `0x3000–0x303F`。
- **解决**：字体生成加 `--range 0x2000-0x206F`。
- 常用区间：ASCII `0x20-0x7F`、General Punctuation `0x2000-0x206F`、CJK 标点 `0x3000-0x303F`、全角 `0xFF00-0xFF5F`。

#### D5. 音频没声音 / 音量太小
- **根因**：`esp_codec_dev_set_out_vol` 的音量曲线是 **1 = -49.5dB，100 = 0dB**，85 只有约 -7dB。
- **解决**：音量拉到 **100**，并加 **2× 软件增益**（源音频电平往往偏低）。
- 自检：开机播 880Hz 短音验证扬声器通路。

#### D6. 物理键：中间键不是 GPIO
- **真相**（来自原理图 KEYS 区）：
  | 丝印 | 连接 |
  |---|---|
  | Key2 | **GPIO0**（BOOT） |
  | Key3 | **GPIO18** |
  | Key1 | **PWRON → AXP2101**（中间那个键） |
- **读中间键**：过 I2C 访问 AXP2101（地址 `0x34`）：
  - `INTEN2 (0x41) bit3` = PWRKEY 短按中断使能
  - `INTSTS2 (0x49) bit3` = 状态（**写 1 清除**）
- ⚠️ **初始化时必须先清中断标志**，否则开机那一下会误触发（一上电就关屏）。
- ⚠️ **长按 PWRON 会关机**——PMIC 硬件行为，软件改不了。
- 参考实现：Waveshare 示例里的 `XPowersLib`（`AXP2101Constants.h` / `XPowersAXP2101.tpp`）。

#### D7. WiFi 初始化失败：`wifi osi_nvs_open fail`
- **根因**：**忘了 `nvs_flash_init()`**（WiFi 驱动依赖 NVS）。
- **现象**：`ESP_ERROR_CHECK(esp_wifi_init)` abort → **反复重启**。

#### D8. 内部 RAM 不够：`wifi:malloc buffer fail`
- **现象**：`Expected to init 10 rx buffer, actual is 9`；UI 之后内部 RAM 只剩 **51KB**。
- **根因**：`CONFIG_LV_ATTRIBUTE_FAST_MEM_USE_IRAM=y`（LVGL fast mem 占 IRAM）+ 默认 WiFi 缓冲数。
- **解决**：
  ```
  CONFIG_LV_ATTRIBUTE_FAST_MEM_USE_IRAM=n
  CONFIG_LV_DRAW_SW_DRAW_UNIT_CNT=1
  CONFIG_ESP_WIFI_STATIC_RX_BUFFER_NUM=8
  CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=16
  CONFIG_ESP_WIFI_DYNAMIC_TX_BUFFER_NUM=16
  ```
  → 内部 RAM 从 **51K → 168K**。
- 排查方法：`heap_caps_get_free_size(MALLOC_CAP_INTERNAL/SPIRAM)` 打点。

#### D9. hello JSON 被截断
- **现象**：主机报 `JSONDecodeError: Expecting ':' delimiter: line 1 column 160`。
- **根因**：`char hello[160]` 太小（hello JSON 约 200 字节）。
- **解决**：缓冲区改 **320**。

#### D10. 唤醒词：whisper 在短片段上幻觉
- **现象**：识别出 `'谢谢。'`、`'请使用简体中文。'`（initial prompt 泄漏）、`'有什么事我再听的。'`（**板子自己播报的声音被录进去**）。
- **对照实验**：**干净的「小弟小弟」whisper 完全认得出**（连 1 秒截断都行）→ 问题在**音频质量**。
- **相关**：麦克风增益 36dB 导致**削顶**（PTT 录音 `peak=32768`）；VAD 阈值过低会把环境噪声当语音。
- **标定数据**：安静时 `rms≈94`，有声音时 `rms≈2500`。
- **截至 2026-10-17**：已切换千问 ASR，播放期间暂停语音并持续排空麦克风缓冲；0.2.5 已加入 200 ms pre-roll，噪声及距离标定仍待验收。

#### D11. `audio_wake_start` 静默失败
- **根因**：它在 `audio_init()` **之前**被调用，此时没有 mic 句柄 → 返回 `ESP_ERR_INVALID_STATE`，**不打日志**。
- **教训**：初始化顺序很重要；关键初始化失败要打日志。

---
