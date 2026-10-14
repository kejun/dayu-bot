# Mini Agent Assistant

通过 ESP32-S3 语音终端，向主机（Mac mini / 本机）上的 Agent 下达**手机操作任务**，
Agent 用 **iCTRL** 操作 iPhone，结果回到 ESP32 显示/播报。

```
用户按住说话 ──► ESP32 采集音频 ──► 主机连接程序 (host/)
                                        │
                        ┌───────────────┴───────────────┐
                        ▼                                ▼
                   语音识别 STT                    Hermes Agent（复用现有）
                        │                                │
                        └──────────► 任务文本 ──────────►│
                                                         ▼
                                                  iCTRL JSON-RPC:8200
                                                         ▼
                                                    iPhone (微信 / 邮件)
```

核心只有一条闭环：**说一句话 → 手机执行 → 返回结果**。

## 目录

| 路径 | 内容 |
|---|---|
| `docs/` | 架构、开发计划、协议、iCTRL 安装、验收用例 |
| `host/` | 主机连接程序（Python）：iCTRL 客户端封装、Agent 调用、任务串行、ESP32 网关 |
| `firmware/` | ESP32-S3 固件（Waveshare ESP32-S3-Touch-AMOLED-2.16） |

## 快速开始（主机侧，文字路径）

```bash
cd host
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

# 检查依赖是否就绪
bridge doctor

# 与 iPhone 上的 iCTRL 配对（手机端会弹出通知，点允许）
bridge pair --host 192.168.1.20:8200

# 纯文字驱动一次手机任务（第 1 阶段验收）
bridge ask "打开手机微信，给张三发送：今晚8点开会"
```

## 前置条件

- **iPhone 已安装并运行 iCTRL**（签名 + DDI + 配对）：见 `docs/ictrl-setup.md`
- **Hermes 已配置可用模型**：`hermes -z "你好"` 能正常返回
- **ESP32-S3 开发板**：见 `firmware/README.md`

## 开发顺序（PRD §8）

1. 主机文字指令 → Hermes + iCTRL，跑通微信/邮件两个场景
2. ESP32 录音/播放/联网，音频到达主机并回放
3. 串起语音 + Agent + 手机
4. 追问、取消、最少状态
5. 桌宠 + 现成调度

当前进度见 `docs/plan.md`。
