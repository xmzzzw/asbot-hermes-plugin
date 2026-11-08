# 从自己的 Hermes 接入 AS-BOT：邀请、安装和微信收图

这份教程给收到 AS-BOT 邀请的人。你使用自己的电脑、官方 Hermes Agent 和微信。AS-BOT 插件提供获准的截图与经济日历，普通聊天、搜索和图片分析仍使用你自己的 Hermes。

先看邀请内容：**收到一段接入码**，走下文「使用接入码」；**管理员明确通知你可用 Web 自主管理**，才走「使用 Web 自主管理」。管理页显示「自主管理暂未开放」时，普通邀请仍按接入码流程使用。仅拿到管理页地址不会自动获得资格。

## 1. 确认你的 Hermes 微信私聊已经能用

如果你的 Hermes 已经能在微信私聊里正常回复，直接复用原来的 Profile（例如 `default`）和那条已批准的私聊，跳到下一节。

1. 第一次使用 Hermes，按 [Hermes 官方安装指南](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart) 安装。用自己的模型服务跑通一次普通对话。**AS-BOT 插件不负责安装 Hermes。**
2. 按 [Hermes 微信设置指南](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/weixin) 运行 `hermes gateway setup`，选择 **Weixin**，用自己的手机微信扫描并确认。通过自己的原有方式启动该 Profile 的 Gateway；已经在运行就复用它。
3. 在目标微信私聊发一条普通消息，确认能收到回复。插件需要一条由 Hermes **已批准**的本地私聊。如果还没批准，在对应 Profile 查看 `hermes pairing list`；如果出现微信待批准记录，核对是本人后用 `hermes pairing approve weixin <请求编号或微信给出的码>`。多 Profile 时先确保命令作用在正确的 Profile。不要为了让插件找到私聊而放开所有用户。

微信扫码登录、Hermes 私聊批准、AS-BOT 设备授权是三件不同的事。原始微信会话 ID 留在本机，不必发给管理员。

## 2. 下载并校验 AS-BOT 插件

目前对接的公开发行版是 [AS-BOT Hermes 插件 v1.4.1](https://github.com/xmzzzw/asbot-hermes-plugin/releases/tag/v1.4.1)，文件名为 `asbot-hermes-plugin-v1.4.1.zip`。`v1.4.1` 是**插件版本**，不需要把 Hermes 改装成同一个版本。使用邀请指定的正式 ZIP；GitHub 的 “Source code (zip)” 不是该安装包。

先在下载目录核对 ZIP 的 SHA-256，与管理员通过可信渠道提供的校验值一致后再解压。macOS 示例：

```sh
shasum -a 256 asbot-hermes-plugin-v1.4.1.zip
```

本发行版的 ZIP SHA-256 是 `2b0c9517e1115023ad262f078fe8d1d2394055523ccd1d386823f95634985cd4`；完整源码提交为 `9bb11d52bf0ddc78d2083a4f0d8b96ee900927cd`。如果管理员给的是另一个正式发行版，按那个版本的独立校验值核对，不沿用这里的值。

进入解压后**直接包含 `bin/` 的目录**。后面的 `python3` 指的是你现有 Hermes 环境里的 Python。如果终端里 `python3` 指向系统 Python，请改用 Hermes 的 Python 完整路径。比如，只有在你的安装实际使用这个路径时，才用 `"$HOME/.hermes/hermes-agent/venv/bin/python"` 代替 `python3`。请自己的 Agent 帮忙也可以：先让它阅读解压包中的 `AGENTS.md`、`AGENT-INSTALL.md`，本人完成需要的登录和设备确认。

## 3A. 使用接入码：收到旧式邀请说明时

1. 在解压目录运行：

   ```sh
   python3 bin/plugin.py setup
   ```

   安装器会识别已有的 Hermes Profile 和已批准的微信私聊。如果有多个，选你要收图的那一条。出现隐藏输入提示时，输入管理员私发的**接入码**；输入不回显是正常的。接入码通常 24 小时内可用，一次只领取一台安装。若管理员让你先提交申请，可以直接回车，再把**申请编号**给管理员审核。
2. **亲友**：接入码领取成功、账号获准后，继续第 4 节即可，无需 Discord。**会员**：如果管理员要求本人验证，用与会员账号绑定的 Discord 在 AlphaStudies 执行 `/接入确认`，填写安装器输出里 `/接入确认` 后面的 **12 位设备配对码**。核对设备后确认。也可以从原安装目录运行 `python3 bin/plugin.py activate --auth web --wait 120` 完成旧流程的 Discord 网页验证。Discord 验证成功仍以管理员审批和会员资格为准。

## 3B. 使用 Web 自主管理：管理员已通知开放时

1. **亲友**：在支持 Passkey 的浏览器打开管理员私发的一次性邀请链接，点「绑定 Passkey」，按系统提示完成确认。以后从 [Hermes 自主管理页](https://asbot-connect.zhouzhou.edu.kg/connect/manage) 点「用 Passkey 登录」。**会员**：在同一个管理页用已经绑定的 Discord 登录。Passkey 邀请链接用于登录，不是终端接入码。
2. 如果管理员提供支持 `setup --manage` 的插件（从 v1.5.0 起），运行 `python3 bin/plugin.py setup --manage`，复制输出中的 `pairingCode`。使用 v1.4.1 时运行 `python3 bin/plugin.py setup`，在终端选定一个 Profile 和私聊，接入码提示直接回车，再复制 `identityCommand` 中 `/接入确认` 后的 12 位码。配对码通常 10 分钟有效。
3. 在网页填入配对码，点「核对设备」。核对设备名、系统和 Hermes 版本。新版只找到一个微信绑定时会默认勾选；找到多个时，可同时勾选想接入的配置。网页只显示配置名和私聊序号，原始微信标识与登录凭据留在本机。点「确认配对」后，回到安装器执行第 4 节。这里不用传 `--auth web`；那个参数属于旧流程的 Discord 验证。
4. 激活后回网页点「刷新状态」。选中的配置都显示在同一台设备下，共享账号额度。换机或丢失设备时可以撤销该设备，设备下所有配置会一起失去 AS-BOT 接入权限。「已停用，待清理」表示该设备已不能调用 AS-BOT，外部资源仍由管理员核对。

## 4. 激活、检查和确认微信收图

在同一个解压目录，用同一个 Hermes Python、同一份本地安装状态执行：

```sh
python3 bin/plugin.py activate --wait 120
python3 bin/plugin.py status
python3 bin/plugin.py doctor
```

如果你在 `setup` 用过 `--state-dir`，后面每条命令也传同一个目录。安装器自动领取当前设备的凭据，不需要手填 Cloudflare 或 AS-BOT Token。

1. `ACTIVE` 和 `doctor` 授权成功表示设备可以访问 API，还不能证明微信能收到图片。
2. 到选定的微信私聊发「截图帮助」查看获准的编号。动态全部账号会自动纳入后续新增的已登记截图指令，无需逐个勾选。多位编号需要 v1.5.1 或更新插件；旧安装应使用自己的状态目录按指南 `update`，不要重新配对或覆盖已有配置。只发一次允许的编号，例如 `0`（账号必须开放截图能力）。确认手机收到实际图片及对应模板说明。明确的数字截图指令不调用模型；图片分析和普通对话仍可能调用你的模型。
3. 如果要验证经济日历，请先确认账号开了该权限，再让自己的 Agent 使用日历工具查询。截图回执只证明截图，未执行的项目不要记成通过。

## 常见卡点

| 现象 | 处理方式 |
| --- | --- |
| 我不知道设备配对码在哪 | 在这次 `setup` 的输出中找 `/接入确认` 后面的 12 位码。已经过期时，在原状态目录运行 `status` 刷新，再核对。它和管理员接入码、微信配对码不同。 |
| 显示待审核、待开通，或者断网 | 在原目录运行 `status`，按当前阶段继续原申请。若显示结果未知或待对账，停止重复提交，请管理员查同一个申请编号。 |
| `PLUGIN_RELOAD_PENDING` / `PLATFORM_REFERENCE_PENDING` | 确认原 Gateway 在运行、正确微信 Profile 已连接。若确实需要重启，由设备主人选空闲窗口重启**现有** Gateway 一次，再检查或继续 `activate`。不要启动第二个微信消费者。 |
| 该 Profile 已有 AS-BOT 插件 | 这是已有安装；使用它自己的状态目录和 `update`，或请维护者先核对归属。不要删除原配置来绕过安装保护。 |
| 图片还没到 | 查「截图状态」或联系管理员核对原任务。结果未知时不要用新编号重发。只有想要一张**新的**图时，才可用不同编号的明确命令，如 `0 #A02`，避免上游折叠完全相同的文本。 |
| Passkey 丢失、设备换机或访问被拒绝 | 联系管理员核实本人、恢复登录或撤销旧设备。每个账号最多两台启用设备，共享额度；凭据和状态目录不能复制给另一台。 |

反馈时给管理员**申请编号、状态和错误码**即可。不要贴接入码、Passkey 邀请、设备码、恢复凭据、`enrollment.json` 或 `bridge.json`。这份教程与已发布 ZIP 分开维护；安装与故障细节可再看 [Agent 安装与处理指南](AGENT-INSTALL.md)。
