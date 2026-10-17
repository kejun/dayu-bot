# iCTRL：安装、恢复与快速启动

更新：2026-10-17。本项目当前通过 **Xcode 构建和常驻测试会话**运行 iPhone Runner，主机使用加密 JSON-RPC 2.0 访问 `:8200`。下方 SideStore / iLoader 内容是历史排错记录，不代表当前验证路线。

## 当前部署边界

Runner 源码位于独立仓库 **`/Users/haoyu/code/ictrl`**，不在本仓库中。今天增加的 `launch_mode:"foreground"` 需要构建并部署该仓库的修改，`skill_version` 相同不代表一定包含本机扩展。

本机曾在 iPhone `192.168.1.6:8200` 验证，地址受 DHCP 影响。Bridge 在项目 `.env` 配置 `ICTRL_HOST`，设备换 IP 后也需重新确认按 host:port 保存的配对记录。

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

本机今天已恢复并部署此测试会话。Runner 的 UI 操作授权依赖测试会话，不能只因 App 存活或 `/schema` 可访问就认定 UI 已就绪。不要并行启动多个测试会话控制同一手机。

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

## 今天的打开应用提速

本机 Runner 的 `launch_app` 增加参数：

| `launch_mode` | 行为 |
|---|---|
| `ready` | 默认，保持原有前台切换、弹窗检查和首屏稳定观察 |
| `foreground` | 前台切换和启动弹窗检查后返回，省去首屏稳定等待；必须再读屏核查 |

Bridge 默认 `PHONE_FAST_LAUNCH=true`，使用 `foreground` 后立即读屏；确实仍在加载或空白时最多按 `PHONE_READY_READS=3` 有界重读。该优化不跳过任务确认、页面核验或未知结果处理。

2026-10-17 对同一已运行淘宝 App 的少量交替样本：普通启动 2.42 / 1.23 秒，快速启动 0.54 / 0.48 秒；启动加首次读屏分别为 3.77 / 1.78 秒与 1.15 / 1.03 秒。记录在 `.runtime/launch-benchmark.json`，不是冷启动或所有 App 的延时保证。

## 默认 direct 与可选 Hermes

默认 Bridge 直接调用千问，并通过固定手机工具使用官方加密客户端；不需要每个任务读取 `/skill` 或启动 Hermes。官方客户端仍保留在 `host/vendor/ictrl_rpc.py`，本机局域网 HTTP 由 curl 适配。

显式设 `BRIDGE_AGENT_BACKEND=hermes` 时才使用 Hermes CLI。需要安装技能可运行：

```bash
cd /Users/haoyu/dayu-dot/host
./.venv/bin/python -m bridge.cli install-skill
```

该命令是可选 Hermes 后端的准备步骤，不是默认 direct 路径的前置条件。

## 恢复与排错

| 现象 | 当前处理 |
|---|---|
| `/schema` 或 `/skill` 不可达 | 检查地址、Remote Access、网络、Runner 测试会话和 DDI |
| 健康接口通但 `Not authorized for performing UI testing actions` | 恢复 `xcodebuild test-without-building` 会话，再实际读屏验证 |
| 手机锁屏或需要密码 | 解锁后继续；不能把点亮屏幕当成已完成解锁 |
| 返回 `401` / 配对失效 | 确认 host:port，重新配对并在手机批准 |
| UI 调用超时、操作结果未知 | 停止重复派发，检查手机和 Runner 日志；必要时使用诊断 `logs` / `reset`，清理后核对实际结果 |
| 明确 `Could not open '<bundle_id>'...` | 任务按普通启动失败结束，检查应用是否安装及标识是否正确；不因该固定拒绝消息要求重启 Bridge |
| Bridge 报手机通道已暂停 | 本地收尾或动作结果无法确认；核对实际手机状态、处理 Runner 故障，再安全重启 Bridge；不自动重跑旧任务 |
| Python 访问 LAN 报 `No route to host`，curl 正常 | 检查 macOS 本地网络权限；项目保留 curl 适配，见历史记录 |

本机日志为 `~/.dayu/logs/ictrl-runner.log`、`bridge.log`，任务详情位于 `.runtime/direct/<trace_id>/`。桥接服务重启与 Runner 重启是不同步骤，不应把所有应用失败都归结为重启网关。

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
