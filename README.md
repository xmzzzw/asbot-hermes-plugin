# AS-BOT Hermes 插件

给已有官方 Hermes Agent 增加 AS-BOT 截图与经济日历工具。公开仓库为 [xmzzzw/asbot-hermes-plugin](https://github.com/xmzzzw/asbot-hermes-plugin)，插件发行版为 `1.4.1`。这不是 Hermes 的分支、替代客户端或指定版本的 Hermes；安装器不会安装、升级、降级 Hermes，不改变模型、搜索、审批、微信登录或其他插件。`bin/plugin.py` 只是本插件的安装与认证助手；旧入口 `bin/client.py` 保留兼容。

固定发行版、完整源 SHA 和校验值都指 **AS-BOT 插件**，用于核对来源与恢复，与你的 Hermes 版本无关。基础接入按官方公开插件接口检测能力，不再比较微信适配器的源码指纹。未来 Hermes 若破坏公开接口仍可能需要插件适配；不能承诺任何未来版本永不受影响。

先由可信渠道取得发行包及**独立提供的 zip SHA-256**，核对整个 zip 后解压。包内 `release.json` 记录完整来源 SHA、插件版本（协议字段仍叫 `clientVersion`）和文件校验值；自带校验清单不能单独证明来源。不要复制私有 AS-BOT 仓库、Owner 工具或私密配置。

让你的 Agent 阅读 [AGENTS-client.md](AGENTS-client.md) 和 [安装与问题处理指南](AGENT-INSTALL.md)，使用你**已有的 Hermes Python 环境**运行：

```sh
python3 bin/plugin.py setup
python3 bin/plugin.py activate --wait 60
python3 bin/plugin.py status
python3 bin/plugin.py doctor
```

安装助手自动检测 `HERMES_HOME`，未设置时使用 `~/.hermes`，发现已有 Profile 和已批准微信私聊。只有一个时自动选择，多个时在本机选择；不要猜测或扩大已批准范围。路径含中文、空格时加引号。原始微信 ID 留在本机，不上传用作账号认证。

接入码只在隐藏终端提示中输入，不放在命令参数、环境变量或日志。直接回车也可先申请、等待管理员审核。会员按管理员要求在 Discord 使用 `/接入确认 配对码`，或运行 `activate --auth web` 登录 Discord。本人身份验证成功仍需管理员批准和资格检查。

状态依次区分安装、待审核、待认证、开通中、API 接入已确认和待真人收图。`ACTIVE`、接口检查及热加载成功不等于微信收图验收。无法热加载会显示 `PLUGIN_RELOAD_PENDING`，不会启动或重启 Gateway；需要重启时由设备主人安排空闲窗口。发生结果未知的写入，只保留原申请继续查询，不重新申请或重放。

## 官方接口模式与验收边界

新安装默认使用 `official` 模式。明确截图指令 `1`、`BTC1`、`截图1` 由插件在官方准入 hook 中接管；后台直接调用 AS-BOT，再通过现有微信连接的公开发图接口交付。**这些明确指令不进入模型，不消耗模型 Token。** 普通聊天、图片分析以及自然语言让 Agent 选择工具时，仍可能调用用户自己的模型。

平台连接回调需要绑定现有微信连接。首次安装或热加载后未绑定时，明确截图指令返回通道未就绪提示，不退回模型。由设备主人在空闲窗口确认后重启现有 Gateway 一次，安装器不会擅自重启、启动第二个微信消费者或重新登录微信。插件不修改 Hermes 源码或适配器字段，也不自行实现微信上传协议。

发送“截图帮助”会列出当前账号允许查看的模板编号与描述；原生回图也附上对应模板说明。帮助与明确截图指令都不调用模型。

同模板需要新图时，请分条发送 `1 #A01`、`2 #A02`、`1 #A03`。这仍是明确的零模型指令，编号只让文本不同；不要用新编号自动重试结果未知的任务。官方微信端可能在插件收到消息前过滤完全相同文本，所以原样重复数字 `1→2→1`、忙时独立旁路及任意旧图引用恢复仍须分别验证，不作基础保证。不会为这些能力放宽权限、修改上游去重或延长全局 hook 超时。

完成连接后，请实际发送明确指令并确认原生图片，核对未启动模型回合，再核对日历与旧图引用。`ACTIVE`、API 成功及热加载成功都不能代替真实收图；未有回执前保持 `qualified:false`。详见 [Agent 处理步骤](AGENT-INSTALL.md)。

## 升级、恢复与卸载

取得新插件包、独立核对 zip 校验值后，使用原 Hermes Python 和同一状态目录运行：

```sh
python3 bin/plugin.py update
python3 bin/plugin.py uninstall
```

只升级本安装回执拥有的插件文件；保留原始备份、账号、额度、图片账本和恢复密钥。发现后续用户改动时停止，不覆盖。旧 `1.0.0` 安装需明确核对传输模式后迁移，不能静默切换功能。凭据轮换用同一状态目录再次运行 `activate`；结果未知时只查询当前凭据版本。

为保留恢复记录，历史状态目录不改名：macOS `~/Library/Application Support/asbot-client`，Linux `${XDG_STATE_HOME:-~/.local/state}/asbot-client`，Windows `%LOCALAPPDATA%\asbot-client`。可用 `--state-dir` 指定独立私密目录，必须在仓库和发行包之外；每个安装实例独立凭据，不要复制给另一设备。POSIX 私有权限或 Windows 当前用户 ACL 无法验证时拒绝继续。Windows 实机资格仍待验。

卸载仅恢复本安装的原配置并移除本插件；服务端撤销由管理员操作。不要发送 `enrollment.json`、`bridge.json` 或私有备份，排查只需申请编号、阶段和错误码。
