# Mini Agent Assistant —— 项目总结与踩坑记录

> 版本：2026-10-17
> 项目路径：`/Users/haoyu/dayu-dot`
> 一句话：**对 ESP32 语音终端说话 → 主机 Agent 理解 → iCTRL 操作 iPhone → 结果回传显示/播报**

---

## 1. 产品目标（来自 PRD v1.2）

核心闭环只有一条：

```
用户说话 → ESP32 采集语音 → 主机 Agent 理解任务 → iCTRL 操作 iPhone → ESP32 显示/播报结果
```

**关键约束**：
- 微信、邮件**不是独立业务模块**，统一通过 **iCTRL 操作手机 App** 完成。
- 不引入 Gmail API / IMAP / 邮箱授权页 / 自研 Agent Runtime / 独立调度器 / 插件平台。
- 复用现有 Agent（Hermes）、现有调度（`hermes cron`）、现有技能体系。
- 主机 Agent 长期无人值守操作手机；结果必须**如实**（"结果未确认" ≠ "成功"）。

---

## 2. 最终架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        Mac（主机）                                │
│                                                                   │
│  ESP32-S3 ──WiFi(ws://192.168.1.13:8765/ws)──► host/bridge        │
│  (语音/状态)                                    ├─ whisper.cpp STT │
│                                                 ├─ macOS say TTS   │
│                                                 ├─ 任务串行闸门     │
│                                                 └─ hermes -z ──┐   │
│                                                                 │   │
│                                       ┌─────────────────────────┘   │
│                                       ▼                             │
│                                  iCTRL JSON-RPC :8200               │
└───────────────────────────────────────┼─────────────────────────────┘
                                        ▼
                              iPhone（XCUITest runner）
```

**数据流（语音指令）**：

```
按住物理键 或 说「小弟小弟」
  → ESP32 录 16kHz/单声道/16bit PCM
  → WebSocket 二进制帧上传
  → 主机 whisper.cpp 识别成文字（简体）
  → 任务闸门（串行/去重/取消）
  → hermes -z -t terminal --skills ictrl-phone-control --yolo "<指令>"
  → Hermes 调 iCTRL 客户端操作 iPhone
  → DONE: success|failed|cancelled|unknown; <结果>
  → 结果回传 ESP32（精灵表情 + 中文标签）+ macOS say 语音播报
```

---

## 3. 软硬件清单

| 项 | 具体 |
|---|---|
| 开发板 | Waveshare **ESP32-S3-Touch-AMOLED-2.16** |
| 芯片 | ESP32-S3 (QFN56) rev v0.2，16MB Flash + 8MB Octal PSRAM |
| 屏幕 | 2.16" 480×480 AMOLED，**CO5300** QSPI |
| 触摸 | **CST9217/CST9220** 电容触摸（I2C） |
| 音频 | **ES7210** ADC（麦克风）+ **ES8311** codec（扬声器） |
| 电源/姿态 | **AXP2101** PMIC + QMI8658 IMU |
| 物理键 | Key2=**GPIO0**、Key3=**GPIO18**、Key1=**PWRON**（接 AXP2101） |
| iPhone | iPhone 13 mini（iPhone14,4），iOS 27.0.1，UDID `00008110-001C7C113A22801E` |
| 主机 | MacBookPro18,1（M1 Pro），macOS 27.0.1 |
| ESP-IDF | **v5.5.5**（`~/esp/esp-idf`） |
| LVGL | 9.x（Waveshare BSP 带） |
| Xcode | 27.1（构建 iCTRL runner） |
| STT | **whisper.cpp** + `ggml-small.bin`（`~/.dayu/models/`） |
| TTS | macOS `say -v Tingting` → ffmpeg → 16kHz PCM |
| Agent | Hermes（`~/.local/bin/hermes`），模型 `deepseek-v4.1-flash` @ `opencode-go` |

---

## 4. 仓库结构

```
dayu-dot/
├── README.md
├── docs/
│   ├── architecture.md            # 架构
│   ├── plan.md                    # 开发计划与验收清单
│   ├── protocol-v1.md             # ESP32 ⇄ 主机 终端协议 v1
│   ├── ictrl-setup.md             # iCTRL 安装 + 全部排错（很有价值）
│   ├── reference-ictrl-SKILL.md   # iCTRL 官方 skill（参考）
│   └── PROJECT-SUMMARY.md         # 本文档
├── host/                          # 主机连接程序
│   ├── pyproject.toml
│   ├── vendor/ictrl_rpc.py        # iCTRL 官方 Python 客户端（未改动）
│   ├── skills/ictrl-phone-control/SKILL.md
│   ├── bridge/
│   │   ├── config.py              # 配置（env + config.json）
│   │   ├── ictrl.py               # iCTRL 封装（HTTP 层走 curl）
│   │   ├── agent.py               # Hermes 适配 + ASK/DONE 解析
│   │   ├── speech.py              # whisper.cpp STT
│   │   ├── tts.py                 # macOS say TTS
│   │   ├── tasks.py               # 任务串行闸门（核心业务逻辑）
│   │   ├── protocol.py            # 协议 v1 消息构造
│   │   ├── server.py              # WebSocket 网关
│   │   └── cli.py                 # bridge doctor/pair/phone/ask/install-skill/serve
│   └── tests/test_tasks.py        # 7 项单测
└── firmware/ds-bot/               # ESP32 固件
    ├── CMakeLists.txt / partitions.csv / sdkconfig.defaults
    ├── main/
    │   ├── main.c                 # UI 状态机 + PTT + 唤醒 + TTS 播放
    │   ├── audio.c/.h             # ES7210/ES8311 + VAD 唤醒采集
    │   ├── buttons.c/.h           # GPIO18/GPIO0 按键
    │   ├── axp2101.c/.h           # 极简 AXP2101 驱动（PWRKEY 短按）
    │   ├── net.c/.h               # WiFi + WebSocket 客户端
    │   └── Kconfig.projbuild      # WiFi/主机/token 配置
    ├── components/uface/          # U-Face 精灵素材 + 32px 中文字体
    └── tools/
        ├── build-uface-256.sh     # 生成 256px 精灵素材
        └── build-cjk-font.sh      # 生成中文字体子集
```

---

## 5. 实施时间线

| 阶段 | 内容 | 结果 |
|---|---|---|
| 1 | ESP32 开发环境（ESP-IDF v5.5.5 + 工具链） | ✅ |
| 2 | **iCTRL 安装到 iPhone**（最长的一段，见 §6.B） | ✅ |
| 3 | 主机 bridge 实现（任务闸门/Agent/iCTRL/协议） | ✅ |
| 4 | 阶段 1 验收：文字指令驱动 Agent + iCTRL | ✅ 微信发送跑通 |
| 5 | ds-bot 固件：显示 → 精灵 → 字体 → 触摸 → 音频 → 按键 → 联网 | ✅ |
| 6 | TTS 语音播报 + 唤醒词「小弟小弟」 | 🟡 通路通，唤醒识别在调 |
| 7 | 定时任务（`hermes cron`）、桌宠、验收用例 3–5 | ⬜ |

---

## 6. 踩坑记录（重点）

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

### B. iCTRL 安装到 iPhone（本项目的最大坑区）

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
- **结论**：**iLoader/isideload 这条路走不通**，必须用保留 bundle id 的方式。

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

#### C3. Hermes agent 慢到 300 秒超时
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

#### C4. `'WhisperCpp' object is not callable`
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
- **待办**：阈值/增益精调、加 pre-roll、唤醒期间屏蔽 TTS 回采。

#### D11. `audio_wake_start` 静默失败
- **根因**：它在 `audio_init()` **之前**被调用，此时没有 mic 句柄 → 返回 `ESP_ERR_INVALID_STATE`，**不打日志**。
- **教训**：初始化顺序很重要；关键初始化失败要打日志。

---

## 7. 关键命令备忘

### ESP32 固件

```bash
export PATH="$HOME/.idf-python:$PATH"          # python3 → 3.13
. ~/esp/esp-idf/export.sh
cd ~/dayu-dot/firmware/ds-bot
export SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local"

pkill -f serial_logger.py                       # ⚠️ 烧录前必须先停记录器
idf.py build
idf.py -p /dev/cu.usbmodem101 flash
```

### 主机 bridge

```bash
cd ~/dayu-dot/host
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export no_proxy="localhost,127.0.0.1,192.168.1.0/24,*.local"
export ICTRL_HOST=192.168.1.6:8200

./.venv/bin/python -m bridge.cli doctor        # 自检
./.venv/bin/python -m bridge.cli pair --host 192.168.1.6:8200
./.venv/bin/python -m bridge.cli install-skill
./.venv/bin/python -m bridge.cli ask "打开手机微信，给大宇发送：哈喽"
BRIDGE_TOKEN=<token> ./.venv/bin/python -u -m bridge.cli serve    # 网关
```

### iCTRL runner（手机需 USB）

```bash
cd ~/code/ictrl
xcodebuild test-without-building -project iCTRL.xcodeproj -scheme iCTRL \
  -derivedDataPath build -destination "id=00008110-001C7C113A22801E" -allowProvisioningUpdates
```

### 手机侧诊断

```bash
cd ~/.ictrl/bin
python3 ictrl_local.py --host 192.168.1.6:8200 call device_info
python3 ictrl_local.py --host 192.168.1.6:8200 call reset     # UI 通道卡住时
python3 ictrl_local.py --host 192.168.1.6:8200 call logs '{"max_elements":15}'
```

---

## 8. 当前状态（2026-10-17）

### 已完成

| 环节 | 状态 |
|---|---|
| ESP-IDF 环境 | ✅ v5.5.5 + 工具链 + Python 3.13 |
| iPhone iCTRL | ✅ 运行中（Xcode 构建，bundle id 以 `.xctrunner` 结尾）|
| DDI | ✅ 已挂（Cryptex，afcclient 直写容器）|
| Remote Access | ✅ `192.168.1.6:8200` |
| bridge 配对 + skill | ✅ `tok` 已缓存，skill 已装进 Hermes |
| 阶段 1 验收用例 1 | ✅ 文字路径：给「大宇」发「哈喽」并截图核查 |
| ds-bot 显示 | ✅ CO5300 AMOLED + LVGL 9 |
| U-Face 精灵 | ✅ 6 组表情 / 18 帧 / 256px |
| 中文字体 | ✅ 32px 子集 |
| 触摸 | ✅ CST9217/9220（按用户要求已取消屏幕录音）|
| 音频 | ✅ ES7210 麦克风 + ES8311 扬声器（回放可听清）|
| 物理键 | ✅ GPIO18/GPIO0 按住说话；中间键短按开关屏幕 |
| 联网 | ✅ WiFi + WebSocket（协议 v1）握手稳定 |
| STT | ✅ whisper.cpp（`ggml-small`，简体）|
| TTS | ✅ macOS `say` → ESP32 播放 |
| 唤醒词通路 | 🟡 「小弟小弟」→ 应答 + 亮屏，**识别准确率在调** |

### 未完成 / 待办

- ⬜ **唤醒词识别准确率**（VAD 阈值/增益/回采屏蔽）
- ⬜ **UI 授权持久化**（目前依赖 Mac 的 `xcodebuild` 测试会话）
- ⬜ 阶段 1 验收用例 2–5（邮件查看、追问、取消、断线、定时任务）
- ⬜ 阶段 2–5（ESP32 语音闭环实测、追问取消、桌宠、`hermes cron`）
- ⬜ 定时任务接入（PRD §6）
- ⬜ 桌宠动画（PRD §5.1，用 `hermes pets`）

---

## 9. 经验教训

### 方法论

1. **先量后调**：内存不足就 `heap_caps_get_free_size` 打点；模型慢就 `hermes prompt-size`；音频不识别就先量 RMS 和存片段离线分析。**别猜**。
2. **串口/日志要"常驻读取"**：没人读时日志会丢——起后台记录器。
3. **对照实验定位**：`curl` 通而 `python` 不通 → 锁定进程级权限；直接给命令 25s vs 读 skill 300s → 锁定上下文过大。
4. **读原理图/源码而不是猜**：中间键是 PWRON（不是 GPIO）就是靠读原理图 KEYS 区确认的；AXP2101 的寄存器位是从 `XPowersLib` 找到的。
5. **官方 issue/PR 是最好的文档**：SideStore 的 `-80045`/`503` 根因和修复值都来自 issue #1657 的 diff。

### 具体教训

6. **代理是万恶之源**：Apple 认证、GitHub 下载、anisette 服务器——都被 Clash 代理坑过。涉及 Apple/局域网时优先**直连**。
7. **macOS 的隐私权限**会拦非系统二进制的局域网访问，但放行 `curl`。
8. **bundle id 的后缀有语义**：`.xctrunner` 决定 iOS 是否注入 XCTest；`.appex`/`.xctest` 必须是主 App id 的子前缀（isideload 的校验）。
9. **LVGL 的坑**：字体是子集（要覆盖用到的标点区）、图片画布有透明边距（精灵显小）、`use_psram` 之外的 IRAM 选项会吃内部 RAM。
10. **初始化顺序**：`nvs_flash_init` 必须在 WiFi 前；`audio_wake_start` 必须在 `audio_init` 后；关键初始化失败要打日志，否则静默失败极难查。
11. **ESP32 的 USB-Serial/JTAG 与烧录工具互斥**：任何占着串口的后台进程都会让烧录失败。
12. **别用转换头**：USB-A↔Type-C 转换头是"通电不通信"的经典元凶。

### 架构性风险（需要正视）

13. **UI 测试授权依赖 Mac**：iCTRL runner 的授权跟着 `xcodebuild` 会话，与 PRD 的"无人值守长期操作"目标冲突。这是**当前最需要解决的架构问题**。
14. **本地 STT/TTS 的实时性**：whisper small 对短片段易幻觉；唤醒词最好用**专用 KWS**（ESP-SR WakeNet 需要向乐视定制「小弟小弟」模型）而不是通用 ASR。
15. **Agent 的上下文成本**：LLM agent 的每次调用都带全部工具 schema + skill 索引；**限定工具集/预载 skill** 是必要的工程优化，不是可选。

---

## 10. 快速上手（给下一个人）

```bash
# 1) 环境
export PATH="$HOME/.idf-python:$PATH"; . ~/esp/esp-idf/export.sh   # ESP32
cd ~/dayu-dot/host && ./.venv/bin/python -m bridge.cli doctor      # 主机自检

# 2) 手机（需要时）
#    - 确认 runner 在跑：curl http://192.168.1.6:8200/schema
#    - 授权失效就重跑 xcodebuild test-without-building（手机插 USB）
#    - DDI 丢了：iCTRL App → Settings → Runtime requirements → Re-mount

# 3) 网关
cd ~/dayu-dot/host
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export no_proxy="localhost,127.0.0.1,192.168.1.0/24,*.local"
BRIDGE_TOKEN=$(cat ~/.dayu/bridge_token) ICTRL_HOST=192.168.1.6:8200 \
  ./.venv/bin/python -u -m bridge.cli serve

# 4) 板子
#    左/右键按住说话；中间键短按开关屏幕；说「小弟小弟」唤醒
```

---

## 11. 相关文档索引

| 文档 | 内容 |
|---|---|
| `docs/architecture.md` | 架构与角色划分 |
| `docs/plan.md` | 开发计划、验收清单、当前进度 |
| `docs/protocol-v1.md` | 终端协议 v1（消息格式、去重、固定约定）|
| `docs/ictrl-setup.md` | **iCTRL 安装 + 全部排错**（最有价值）|
| `docs/reference-ictrl-SKILL.md` | iCTRL 官方 skill 原文 |
| `firmware/ds-bot/README.md` | 固件构建/烧录/进度 |
| 本文档 | 全局总结与踩坑记录 |
