# AS-BOT Hermes 客户端

这是给你的私人 Agent 使用的公开客户端，版本 `1.0.0`。它使用你已有的 Hermes、模型、微信登录和已批准私聊，只增加 AS-BOT 截图与经济日历插件。账号能力与额度由 AS-BOT 服务端决定。安装不会启动 Gateway、重新登录微信、安装或升级 Hermes。

先由可信渠道取得公开版本包和**独立提供的 zip SHA-256**，核对整个 zip 后解压。不要把私有仓库、Owner 工具或私人配置转发给其他人。包内 `release.json` 记录完整 40 位源提交 SHA、客户端版本和每个文件的 SHA-256；单独核对自带 manifest 不能证明包的来源。接入邀请必须同时标明 `clientRelease` 的版本、完整源 SHA、zip 下载位置与 zip SHA-256。仅运行匹配该记录的版本包。

请让你的 Agent 先阅读 [AGENTS-client.md](AGENTS-client.md)，使用你**已有的 Hermes Python 环境**执行下面命令。PyYAML 来自该环境；不要为此重装 Hermes。macOS/Linux 用该环境的 `python3`，Windows 用其 `python.exe`。路径含空格时加引号。

```sh
python3 bin/client.py setup
python3 bin/client.py activate --wait 60
python3 bin/client.py status
python3 bin/client.py doctor
```

`setup` 自动读取 `HERMES_HOME`，未设置时读取 `~/.hermes`。只发现有 `config.yaml` 和 Hermes 已批准微信私聊的既有 Profile。只有一个可选项时自动选择，多个时在本机交互选择；无人值守时用 `--profile default --peer "准确的已批准私聊ID"` 指定。可用 `--hermes-root "/已有/Hermes目录"` 明确指定。原始微信 ID 只保存在本机，绝不上传用于账号身份认证。

邀请码只在隐藏终端提示中输入；不要放在命令参数、环境变量、聊天记录、截图或日志。直接回车可使用管理员审批方式。管理员可能要求你在 Discord 发送客户端显示的 `/接入确认 配对码`；也可用 `activate --auth web` 打开本机浏览器完成 Discord 绑定。配对码是短期公开挑战码，不是恢复密钥。只有本人确认身份且管理员批准、资格检查通过后才会下发独立设备凭据。

阶段包含 `AWAITING_OWNER`（待审核）、`AWAITING_IDENTITY`（待身份）、`AWAITING_ELIGIBILITY`（待资格）、`PROVISIONING`（开通中）、`READY`（凭据待本机确认）、`ACTIVE`（API 接入已确认）。`status` 只轮询当前申请，并会让服务端推进后台开通；过期的短期身份挑战会在同一申请中更新。`UNCERTAIN` 需要管理员核对原申请，`EXPIRED`、`DENIED`、`REVOKED` 不能继续。不要删除状态或重复创建申请绕过这些结果。

`activate` 将凭据私密保存到本机、检查原生命令源码指纹、安装本插件，通过 Hermes 既有控制接口尝试插件热加载，再读取经过认证的 `/me`，成功后才确认凭据接收。无法热加载时显示 `PLUGIN_RELOAD_PENDING`，不会宣称插件已加载或确认凭据。中断后重新执行 `status`、`activate`，会保留原申请 ID、恢复密钥和本机绑定；不会自动重发结果未知的写入。如果确认写入结果未知，向管理员提供申请 ID 和阶段，**不要发送 enrollment.json、bridge.json 或私有备份**。

完成热加载后，在新会话中发 `0`，确认原生图片，再发 `1 → 2 → 1` 检查顺序。热加载待定或已有原生 factory 仍未生效时，请在空闲时由你确认重启现有 Gateway；客户端不会启动或重启 Gateway。接口成功、配置安装和 `ACTIVE` 均不等于真人收图验收；客户端始终显示 `qualified:false`、`PENDING_REAL_RECEIPT`，直到另有真实验收记录。

管理员轮换此设备凭据后，`status` 会显示新的 `credentialVersion` 和待激活状态。使用原状态目录再次运行 `activate`：客户端会重新读取新版本凭据，只更换本安装回执拥有的桥接认证配置，并保留 Profile、插件、账本及恢复密钥。配置被后来改动时停止；轮换写入中断只按精确文件哈希恢复，确认响应丢失仅查询当前版本，不再次提交确认。

默认私密状态位置：macOS 为 `~/Library/Application Support/asbot-client`；Linux 为 `${XDG_STATE_HOME:-~/.local/state}/asbot-client`；Windows 为 `%LOCALAPPDATA%\asbot-client`。每个设备独立申请和凭据，不要复制该目录到另一台机器。可用 `--state-dir "/独立/私密目录"` 管理同一设备的另一安装，但目录必须位于仓库和解压包之外。POSIX 要求当前用户的私有权限；Windows 使用受保护、仅当前 SID 的 ACL，不能验证时拒绝继续。

取得新的可信公开包、核对独立 zip 哈希后，用同一 Hermes Python 和同一 `--state-dir` 执行：

```sh
python3 bin/client.py update
python3 bin/client.py uninstall
```

升级只替换本安装回执拥有的插件文件，保留原始备份；若安装后有人改过 Profile、桥接配置或插件，会停止并要求核对，不覆盖后续改动。已有旧版个人共享配置或其他安装占用本插件时也会拒绝覆盖。卸载只恢复本次安装的原配置并移除本插件，保留私密恢复记录、图片账本和缓存；服务端资格撤销由管理员处理。卸载后要重新开通请联系管理员，不要复制他人 Token。
