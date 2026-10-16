# iCTRL 安装与配对（iPhone 侧，一次性 + 每次重启）

来源：`OpenMinis/iCTRL`。iCTRL 是跑在 iPhone 上的 XCUITest runner，
对外提供 **JSON-RPC 2.0 over HTTP，端口 8200**，请求加密（X25519 配对 +
HMAC-SHA256 信封）。`GET /skill` 是操作手册，`GET /schema` 是方法清单。

> ⚠️ 安全：iCTRL 把手机的完全控制权交给持有配对密钥的一方。
> 仅在自有设备/账号上使用，只批准自己发起的配对，用完关闭远程访问。
> 不要让它无人监管地接触支付、消息等不可撤销操作。

## 前提（两件事都必须满足）

1. **能签名并安装 IPA**：免费 Apple ID 用最新版 **SideStore**；付费账号用
   `Scripts/sign-ipa.sh` 或 Xcode。（runner 内嵌 `PlugIns/iCTRLUITests.xctest`，
   必须从内到外单独签名，普通签名器会漏签导致启动即崩溃。）
2. **挂载 Developer Disk Image (DDI)**：XCUITest 依赖 `testmanagerd`，只有挂载
   DDI 时才存在。**每次重启都要重新挂载**。有 Mac+Xcode 可自动挂载；没有则用
   StikDebug 或 iCTRL App 在设备上挂载。

先开 **设置 → 隐私与安全性 → 开发者模式**，重启。

## 安装

- **免费 Apple ID**：iLoader 装 SideStore → App Store 装 **LocalDevVPN** 并连接
  → SideStore → My Apps → `+` → 选 `iCTRLUITests-Runner.ipa`
  → 设置 → 通用 → VPN 与设备管理 → 信任证书。
  （免费证书 7 天到期，需用 SideStore 刷新。）
- **付费账号**：`Scripts/sign-ipa.sh` 签名，或 Xcode 直接跑。

## 启动 runner

1. 在手机上启动 iCTRL runner（**真正干活的这个**）。
2. 允许**通知**和**本地网络**权限。
3. runner 的通知里长按可：✅ 批准配对 / 🌐 开启 Remote Access / 🛑 停止服务。

> 默认只监听 `127.0.0.1`。主机不在手机上时，必须打开 **🌐 Remote Access**，
> 且主机与手机在**同一 Wi-Fi**。蜂窝网络下 LAN 不可达。

## 主机侧配对

```bash
cd host
bridge pair --host <手机IP>:8200
# 手机弹出配对通知 → 点 Allow
bridge doctor            # 验证 device_info / skill_version
```

配对密钥缓存在 `~/.ictrl/token.json`，**按 host:port 存**（DHCP 换 IP 需要重配）。
密钥 30 天未用会过期；只有真的返回 `401` 才重新配对。

## 交给 Hermes

iCTRL 自带 skill，直接把它装成 Hermes skill（见 `host/bridge/cli.py:install-skill`），
然后对 Hermes 说：

> "我的 iPhone 在 iCTRL 上运行，地址 `<IP>:8200`，想办法驱动它。"

Hermes 会读 `GET /skill`、`GET /schema`，从设备下载客户端，发起配对。

## 排错

| 现象 | 处理 |
|---|---|
| `/schema` 打不开 | runner 没在跑，让用户启动（DDI 每次重启会丢） |
| `401` | 密钥失效，重新 `pair` |
| UI 调用连续超时 | `reset`，然后每几秒轮询 `logs` 直到卡住的调用结束（15–45s），别发长 `sleep` |
| 屏幕锁屏 | `unlock` 只点亮屏幕；需要密码时要用户处理 |
| 只有 `logs`/`config`/`reset` 有响应 | UI 队列被卡住的调用占住，正常——用它诊断 |

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

## 路线选择：SideStore 卡住时，用 iLoader 直接装 runner

当 SideStore 登录被 Apple 挡住（`-80045` / `-1005`，见症状 D）时，可以绕过它：

**iLoader → Import IPA → `iCTRLUITests-Runner.ipa`**（iPhone 用 USB 连接）。

为什么可行：isideload 的 `isideload/src/sideload/bundle.rs` 会把 **`PlugIns/` 下
的内容全部当作 app extension 签名**，而 runner 的嵌套测试包正好是
`PlugIns/iCTRLUITests.xctest`，因此会被一并签名。

代价：iLoader 没有 7 天自动续签，免费证书到期需重新 Import。

> 验证方法：装好后**立刻启动 runner**。能起来即签名正确；若报
> `mapped file has no cdhash` 则说明没签上，回退 SideStore。

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

再用 iLoader → Import IPA 装 `iCTRLUITests-Runner-patched.ipa`。

> 这是绕过 isideload 校验的权宜手段；iCTRL 官方路线仍是 SideStore。

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
