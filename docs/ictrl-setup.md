# iCTRL：安装、动作回执与恢复

更新：2026-10-18。本项目当前通过 **Xcode 构建和常驻测试会话**运行 iPhone Runner，主机使用加密 JSON-RPC 2.0 访问 `:8200`。下方 SideStore / iLoader 内容是历史排错记录，不代表当前验证路线。

## 当前部署边界

Runner 源码位于独立的 [OpenMinis/iCTRL 仓库](https://github.com/OpenMinis/iCTRL)，本机路径 **`/Users/haoyu/code/ictrl`**，不在本仓库中。本次增加的 `launch_mode:"foreground"` 和 `operation_receipts` 需要构建并部署该仓库的修改，`skill_version` 相同不代表一定包含这些扩展。

本次可构建版本已推送到 [kejun/iCTRL 的 `feat/dayu-operation-recovery` 分支](https://github.com/kejun/iCTRL/tree/feat/dayu-operation-recovery)，对应提交 [50bcec4](https://github.com/kejun/iCTRL/commit/50bcec4)。当前账号没有上游写权限，这些改动尚未合入 OpenMinis 的 `main`；新环境应使用上述 fork 提交构建。

动作恢复还要求 Runner 的 `device_info.capabilities` 包含 `operation_receipts`。主机为动作附带唯一 `_operation_id`，通过非 UI 通道 `operation_status{operation_id}` 查询 queued/running/succeeded 等状态和真实结果。回执按认证客户端隔离并存到手机本地，同一编号与相同参数不会重复派发；重启前未结束的动作标为未知，不自动重放。不能拿历史日志中同名动作的成功记录冒充当前动作回执。

最近本机测试地址为 iPhone `192.168.1.18:8200`，早期为 `192.168.1.6:8200`，地址受 DHCP 影响。Bridge 在项目 `.env` 配置 `ICTRL_HOST`，设备换 IP 后也需重新确认按 host:port 保存的配对记录。

## 构建与启动（已验证路线）

开启手机开发者模式，连接并信任 Mac，确认 Xcode 能识别设备；手机启动时保持解锁。已有签名配置不必重新生成，首次环境再配置自己的开发团队。

```bash
cd /Users/haoyu/code/ictrl
# 仅初次配置：填写自己的团队 ID
DEVELOPMENT_TEAM=<团队ID> ./Scripts/setup-signing.sh
xcodegen generate
xcodebuild build-for-testing -project iCTRL.xcodeproj -scheme iCTRL \
  -derivedDataPath build -destination "generic/platform=iOS" \
  -allowProvisioningUpdates ARCHS=arm64
xcodebuild test-without-building -project iCTRL.xcodeproj -scheme iCTRL \
  -derivedDataPath build -destination "id=<设备UDID>" -allowProvisioningUpdates
```

本机 2026-10-17 已恢复并部署此测试会话。Runner 的 UI 操作授权依赖测试会话，不能只因 App 存活或 `/schema` 可访问就认定 UI 已就绪。不要并行启动多个测试会话控制同一手机。

开发者镜像 DDI 是运行条件，手机重启后需要确认仍已挂载；Xcode 或设备端挂载路径见下方历史排错。Runner 标识需保留 `.xctrunner` 后缀，历史重签名改写后缀曾导致 XCTest 加载失败。

## 配对与 UI 核查

在 Runner 允许通知和本地网络权限，通过通知打开 **Remote Access**，主机与手机保持网络可达。

```bash
cd /Users/haoyu/dayu-dot/host
./.venv/bin/python -m bridge.cli pair --host <手机IP>:8200
# 手机通知批准配对后核查
./.venv/bin/python -m bridge.cli doctor
./.venv/bin/python -m bridge.cli phone page_source '{"max_elements":40}'
```

仅首次或确实返回配对失效时重新 `pair`；配对记录位于 `~/.ictrl/token.json`，不要写入项目文档。`doctor` 检查配置和健康接口，实际 `page_source` 成功才能验证 UI 调用权限。读屏诊断前确认没有其他正在执行的手机任务。

## 动作回执与超时恢复

动作编号是 16–80 位 ASCII 字母、数字、下划线或连字符。主机在本地提交编号及参数，Runner 再按认证客户端保存回执，成功持久化后才派发。`operation_status` 不进入 UI 队列，在 UI 调用卡住时仍可查询；认证客户端身份由服务端注入，不能通过参数冒充。

| 回执状态 | 主机处理 |
|---|---|
| `queued` / `running` | 查询同一编号，等待原动作结束，不补发动作 |
| `succeeded` / `failed` | 读取原结果或错误；成功后核对新画面，再继续剩余目标 |
| `unknown_after_restart` | Runner 重启前尚在途；保留未知状态并核对实际手机，不能自动重放 |
| `completed_result_expired` | 原动作已结束但载荷过期；编号仍保留，不能把它当成功结果或重放依据 |
| `not_found` / `storage_unavailable` | 缺少可靠完成凭据；保留任务等待并诊断，不能据此重复派发 |

回执位于 Runner 沙盒 `Library/Application Support/iCTRL/operation-receipts.json`，目录/文件权限为 0700/0600。最近 512 条完整结果保留，旧结果只移除载荷、不删除编号；最多保存 50,000 个编号，达到上限拒绝新增动作，不能靠清空记录恢复旧请求。动作已发生、结果未落盘时仍可能未知，因此回执不承诺崩溃期间能自动判定所有外部结果。

Bridge 的基础 RPC 超时为 8 秒，`page` / `page_source` / `screen_text` 至少等待 18 秒；Runner 的 6 秒或 15 秒看门狗仍可能先返回。之后最多用 `PHONE_RECOVERY_TIMEOUT=45` 秒查询原编号，Pi 可再核实、诊断及重新规划，暂不能确认时保留进度。主机代码与手机 Runner 必须配套部署。

独立 Runner 仓库的本地回执检查（不访问手机）：

```bash
cd /Users/haoyu/code/ictrl
swiftc iCTRLKit/RPCTypes.swift iCTRLKit/AuthManager.swift \
  iCTRLKit/SecureEnvelope.swift iCTRLKit/TokenFileStore.swift \
  iCTRLKit/KeychainStore.swift Scripts/test-operation-ledger.swift \
  -o /private/tmp/dayu-ledger-test
/private/tmp/dayu-ledger-test
```

已验证持久化、相同编号去重、客户端隔离、参数绑定及重启未知状态。实机翻页在看门狗 6 秒超时后于 7.274 秒完成，主机查询原编号成功，未再次派发；随后完成推荐流 5 屏浏览，记录见 [验收计划](plan.md)。

## 打开应用提速

本机 Runner 的 `launch_app` 增加参数：

| `launch_mode` | 行为 |
|---|---|
| `ready` | 默认，保持原有前台切换、弹窗检查和首屏稳定观察 |
| `foreground` | 前台切换和启动弹窗检查后返回，省去首屏稳定等待；必须再读屏核查 |

Bridge 默认 `PHONE_FAST_LAUNCH=true`，使用 `foreground` 后立即读屏；确实仍在加载或空白时最多按 `PHONE_READY_READS=3` 有界重读。该优化不跳过任务确认、页面核验或未知结果处理。

2026-10-17 对同一已运行淘宝 App 的少量交替样本：普通启动 2.42 / 1.23 秒，快速启动 0.54 / 0.48 秒；启动加首次读屏分别为 3.77 / 1.78 秒与 1.15 / 1.03 秒。记录在 `.runtime/launch-benchmark.json`，不是冷启动或所有 App 的延时保证。

## 默认 Pi 与显式回退后端

默认 `BRIDGE_AGENT_BACKEND=pi`，常驻 Node / Pi Durable 管理千问循环与持久状态，Python 的固定手机工具使用官方加密客户端；不需要每个任务读取 `/skill` 或启动 Hermes。正式安装和恢复见 [Pi 运行时](../host/pi/README.md)。`direct` / `hermes` 仅供显式回退，不能与 Bridge 并行控制同一手机。官方客户端保留在 `host/vendor/ictrl_rpc.py`，本机局域网 HTTP 由 curl 适配。

显式设 `BRIDGE_AGENT_BACKEND=hermes` 时才使用 Hermes CLI。需要安装技能可运行：

```bash
cd /Users/haoyu/dayu-dot/host
./.venv/bin/python -m bridge.cli install-skill
```

该命令是可选 Hermes 后端的准备步骤，不是默认 Pi 路径的前置条件。

## 恢复与排错

| 现象 | 当前处理 |
|---|---|
| `/schema` 或 `/skill` 不可达 | 检查地址、Remote Access、网络、Runner 测试会话和 DDI |
| Runner 恢复后旧地址仍不可达 | 核对 Runner 启动日志中的当前 IP，更新 `.env` 的 `ICTRL_HOST`；配对记录按 host:port 保存，需核查新地址的配对，再实际读屏 |
| 健康接口通但 `Not authorized for performing UI testing actions` | 恢复 `xcodebuild test-without-building` 会话，再实际读屏验证 |
| 手机锁屏或需要密码 | 解锁后继续；不能把点亮屏幕当成已完成解锁 |
| 返回 `401` / 配对失效 | 确认 host:port，重新配对并在手机批准 |
| UI 调用超时、操作结果未知 | 先以原编号查询 `operation_status`；成功后核对新画面继续，未确认则保留进度。`logs` 辅助诊断，`reset` 不能取消在途动作 |
| `find_element` 明确返回控件树无响应且已结束 | 保持本任务继续，改用 OCR 与设备状态核验前台身份；不补读故障 AX 树，仍可能执行中的超时请求不适用此恢复 |
| 明确 `Could not open '<bundle_id>'...` | 任务按普通启动失败结束，检查应用是否安装及标识是否正确；不因该固定拒绝消息要求重启 Bridge |
| Bridge 报手机通道已暂停 | 有编号时先让 Pi 核实原回执；遗留无编号或本地收尾故障按 [Pi 恢复流程](../host/pi/README.md)停服核查并执行 `pi-recover`，仅重启不会解除持久保护 |
| Python 访问 LAN 报 `No route to host`，curl 正常 | 检查 macOS 本地网络权限；项目保留 curl 适配，见历史记录 |

本机日志为 `~/.dayu/logs/ictrl-runner.log`、`bridge.log`；正式任务详情位于 `~/.dayu/pi/` 的 SQLite、`plans/` 和 `traces/`，显式 direct 回退使用 `.runtime/direct/<trace_id>/`。桥接服务重启与 Runner 重启是不同步骤，不应把所有应用失败都归结为重启网关。

## 历史安装与网络排错记录（2026-10-16）

以下保留本机当时遇到的版本、服务器、签名和 DDI 问题；其中第三方服务与版本状态可能变化。当前已验证部署方式以本文前半部分为准。

## iLoader 登录 Apple ID 失败

**先确认版本：必须用 `2.3.8` 或更新。**
`2.3.7` 存在已知 bug，登录时会对 `gsa.apple.com` 返回
`503 Service Temporarily Unavailable`；`2.3.8` 已修复（见其 release notes）。

- `2.3.8` 起 **自定义 anisette 服务器不再可用**，会自动改用内置的两个之一。
- `2.3.8` 登录会**要求重新做一次 2FA**：受信任设备 2FA 和未开 2FA 的账号正常，
  **短信（SMS）2FA 仍不可用**。

### 症状 A：`unknown session (-45025)` / `Anisette provisioning failed`

公用 anisette 服务器不稳定（`ani.sidestore.io` 时会 502）。
**仅 `2.3.7` 及更早**可用下面方式自建本地服务：

```bash
colima start                     # 重启后需重新启动 colima
docker run -d --name anisette -p 6969:6969 \
  --restart unless-stopped dadoum/anisette-v3-server
```

iLoader → Settings → **Anisette Server** → 自定义填 **`http://127.0.0.1:6969`**
（带 `http` 前缀会被原样使用，否则 iLoader 会补 `https://`）→ 再点
**Reset anisette state** → 重新登录。

### 症状 B：`HTTP 503 ... gsa.apple.com/grandslam/GsService2`

- **首选：升级到 iLoader `2.3.8`**（专门修这个）。
- 若仍失败，检查是否走了代理：`gsa.apple.com` 经机房/VPN 出口 IP 会被 Apple 拒。
  临时关掉系统代理再登录，或把 `*.apple.com` 加入代理例外。
- 连续失败可能触发 Apple 临时限流，等 10–15 分钟再试。

### 症状 C：`2.3.8` 报 `Failed to request /v3/client_info` / `error sending request`

`2.3.8` 默认用内置 anisette 服务器 `ani.jkcoxson.com`（另一个是
`ani.stikstore.app`）。实测这台机器上该服务器**直连稳定（0.7–2s）**，
但**经 Clash 代理又慢（3–5s）又间歇性失败（连接直接断）**，导致
`error sending request`。

→ **关掉 Clash 系统代理**（iLoader 从 Finder 启动，读的是 macOS 系统代理），
或把 `ani.jkcoxson.com`、`ani.stikstore.app` 加进代理例外。

验证命令：
```bash
for i in 1 2 3; do
  curl -s -o /dev/null -w "直连 %{http_code}/%{time_total}s\n" --noproxy '*' https://ani.jkcoxson.com/v3/client_info
  curl -s -o /dev/null -w "代理 %{http_code}/%{time_total}s\n" -x http://127.0.0.1:7892 https://ani.jkcoxson.com/v3/client_info
done
```

> 备选公用 anisette 服务器（不稳定）：`ani.stikstore.app`、`ani.sidestore.zip`、
> `ani.xu30.top`、`anisette.wedotstud.io`。

### 症状 D：SideStore 登录 Apple ID 报 `-80045`（0.7.0-alpha 及更早）

Apple 的 GSA 现在**拒绝第三方 anisette 服务器发来的 macOS client info**，返回
`-80045`。iLoader 2.3.7+ 与 SideStore 的修复都是**改用 Android 身份**
（SideStore PR #1657，2026-10-15 才合并，尚未进入任何已发布版本）。

**手动修复（0.7.0-alpha 自带该设置页）：**

SideStore → **设置 → 高级（Advanced）→ Anisette** → 把 **`Client Info`**
（headerKey `X-Mme-Client-Info`）改成：

```
<Pixel 7> <Android;14;UQ1A.240205.002> <com.apple.AuthKit/1 (com.apple.akd/1.0)>
```

- **末尾不要有空格或换行**——粘贴带换行会让认证接口返回空响应。
- 保存后重启 SideStore，再登录。

> 旧值（会被 GSA 拒绝，若看到就是它）：
> `Constants.Anisette.defaultClientInfo`、
> `<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>`。
>
> 相关 issue：#1657（修复）、#1658 / #1652（仍复现）。

## 历史失败路线：iLoader 直接安装 Runner

本机曾尝试 iLoader 导入 IPA及修改嵌套测试包标识；先遇到 bundle ID 校验，绕过后又出现 XCTest 加载和启动问题。这条路径未完成可用 Runner 验收，下面仅保留当时排错记录，不作为当前部署步骤。当前使用本文开头的 Xcode 路线。

### iLoader 报 `Invalid bundle: Extension ... is not part of the main app bundle identifier`

isideload 的 `update_bundle_id()` 要求扩展 bundle id 是主 App id 的**严格子前缀**
（`starts_with(main) && len > len(main)`），而 iCTRL runner 的
`PlugIns/iCTRLUITests.xctest` 与主 App **同名**，因此被拒。

**权宜修复**：把 `.xctest` 的 `CFBundleIdentifier` 改成子 id 再重打包：

```bash
rm -rf /tmp/runner_patch && mkdir -p /tmp/runner_patch && cd /tmp/runner_patch
unzip -q ~/Downloads/ictrl/iCTRLUITests-Runner.ipa
X="Payload/iCTRLUITests-Runner.app/PlugIns/iCTRLUITests.xctest/Info.plist"
/usr/libexec/PlistBuddy -c "Set CFBundleIdentifier com.openminis.ictrl.uitests.xctrunner.iCTRLUITests" "$X"
zip -q -r -X ~/Downloads/ictrl/iCTRLUITests-Runner-patched.ipa Payload
```

历史尝试中曾导入 `iCTRLUITests-Runner-patched.ipa`，但未解决最终启动问题。

> 修改包标识只能绕过当时的校验，不代表 XCTest Runner 已具备运行条件。

### 症状 E：SideStore 登录报 `-1005`（kCFURLErrorNetworkConnectionLost）

SideStore 默认走远程 anisette 服务器 `ani.sidestore.io`
（`AppConstants.Anisette.Servers.defaultServerURL`），而它**时好时坏**
（实测反复出现 000 / 502 / 200），连不上就报网络中断 `-1005`。

**修复：改用设备端 anisette（不依赖任何远程服务器）。**

```
SideStore → 设置 → Advanced → User Customizations → ANISETTE
  → 打开「On-Device Anisette」("Run ADI emulation directly on device
     instead of remote servers") → 确认重启
  → 需要时再点「Reset adi.pb」清掉 keychain 里的坏 provisioning 数据
  → 重启 SideStore 后重新登录
```

配合症状 D 的 Client Info（Android 身份），两个错误一起解决：
`-80045` 是 client info 被拒，`-1005` 是 anisette 服务器连不上。

## DDI 下载失败（TLS 错误）——用 afcclient 直接写入 App 容器

iCTRL 的 DDI 从 **GitHub** 下载（`doronz88/DeveloperDiskImage` 镜像），
国内直连 `raw.githubusercontent.com` 会 TLS 报错。

**iOS 27 需要 Cryptex 版 DDI**（5 个文件）：
```
BuildManifest.plist  Image.dmg  Image.dmg.trustcache
Image.dmg.cryptex_info  Image.dmg.root_hash
```
来源：`https://github.com/doronz88/DeveloperDiskImage/raw/refs/heads/main/PersonalizedImages/Xcode_iOS_DDI_Cryptex/`

### 绕过办法：直接把文件写进 App 容器

App 存放路径（`DeveloperDiskImageStore`）：
```
<App容器>/Library/Application Support/DDI/Xcode_iOS_DDI_Cryptex/
```

用 `afcclient` 的 HouseArrest 访问容器（注意 isideload 会改写 bundle id，
后缀是 team id，可用 `ideviceinstaller -l` 或崩溃日志确认真实 id）：

```bash
UDID=$(idevice_id -l | head -1)
BID=com.openminis.ictrl.<TEAMID>          # 例如 com.openminis.ictrl.NBYSH5B496
printf 'ls\n' | afcclient --container "$BID" -u "$UDID"     # 验证可访问

{ echo 'mkdir "Library/Application Support/DDI/Xcode_iOS_DDI_Cryptex"'
  for f in BuildManifest.plist Image.dmg Image.dmg.trustcache Image.dmg.cryptex_info Image.dmg.root_hash; do
    echo "put /path/to/ddi/$f \"Library/Application Support/DDI/Xcode_iOS_DDI_Cryptex/$f\""
  done
} | afcclient --container "$BID" -u "$UDID"
```

> afcclient 按空格切分参数，**含空格的路径必须加引号**。

### 备选：让手机走 Mac 的代理下载

Clash 只监听 `127.0.0.1` 时，可在 Mac 上转发 `0.0.0.0:7893 → 127.0.0.1:7892`
（见 `~/.dayu/portfwd.py`），再把手机 Wi-Fi 代理设为 `192.168.1.13:7893`。
（注意：`curl --noproxy '*'` 会覆盖 `-x`，测试时别一起用。）

## 主机侧连不上手机（python/node 报 No route to host）

macOS 的**「本地网络」权限**会拦截 node/python/ruby 等运行时对局域网设备的连接
（报 `EHOSTUNREACH`），但系统自带的 `curl`/`nc`/`ping` 被豁免。现象：
同一台机器上 `curl http://<手机>:8200/schema` 返回 200，而 `python3 -c "socket..."` 报
`[Errno 65] No route to host`。

**处理**：`host/bridge/ictrl.py` 把 iCTRL 官方客户端的 HTTP 层换成 `curl`
（monkeypatch `_post`），加密逻辑不变；健康和技能读取也经 `IctrlClient` 的 curl 适配。这样 Bridge 与可选 Hermes 后端都能访问手机。

> 根治办法：系统设置 → 隐私与安全性 → **本地网络** → 给终端 App 授权。
