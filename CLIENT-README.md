# AS-BOT Hermes 插件

给已有官方 Hermes Agent 增加 AS-BOT 截图与经济日历工具。公开仓库为 [xmzzzw/asbot-hermes-plugin](https://github.com/xmzzzw/asbot-hermes-plugin)，插件发行版为 `1.3.1`。这不是 Hermes 的分支、替代客户端或指定版本的 Hermes；安装器不会安装、升级、降级 Hermes，不改变模型、搜索、审批、微信登录或其他插件。`bin/plugin.py` 只是本插件的安装与认证助手；旧入口 `bin/client.py` 保留兼容。

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

新安装默认使用 `official` 模式。截图和经济日历通过官方工具注册、已准入会话上下文及 Hermes 的正常 `MEDIA:` 发图流程工作，由用户自己的 Agent 调用工具；AS-BOT 不替换微信适配器、不改写内部方法、不自行上传微信图片。图片分析继续使用用户自己的 Agent。

以下增强能力不属于基础接入保证：连续重复数字 `1→2→1`、忙时独立截图旁路、任意旧图引用恢复、所有截图指令完全不调用模型。官方微信端可能在插件收到消息前过滤重复文本，官方准入 hook 还有执行时限；不能通过放宽权限或延长全局超时掩盖这些限制。原有 `legacy` 增强仅供明确选择且通过对应 Hermes 兼容检查的安装，不会自动启用；Hermes 升级后可能失效。既有个人安装不会由新安装助手自动覆盖。

同模板需要新的截图时，分条发送不同编号的消息，例如“请调用 AS-BOT 截图 1，编号 A01”“请调用 AS-BOT 截图 2，编号 A02”“请调用 AS-BOT 截图 1，编号 A03”；不要重复整句，也不要用新编号自动重试结果未知的旧任务。详见 [Agent 处理步骤](AGENT-INSTALL.md)。

完成连接后，请在已批准微信私聊要求 Agent 调用 AS-BOT 截图工具，确认原生图片，再核对日历。连续数字、忙时旁路和旧图引用分别记录结果，不能用接口成功代替。未有真实回执前，状态保持 `qualified:false` 和 `PENDING_REAL_RECEIPT`。

## 升级、恢复与卸载

取得新插件包、独立核对 zip 校验值后，使用原 Hermes Python 和同一状态目录运行：

```sh
python3 bin/plugin.py update
python3 bin/plugin.py uninstall
```

只升级本安装回执拥有的插件文件；保留原始备份、账号、额度、图片账本和恢复密钥。发现后续用户改动时停止，不覆盖。旧 `1.0.0` 安装需明确核对传输模式后迁移，不能静默切换功能。凭据轮换用同一状态目录再次运行 `activate`；结果未知时只查询当前凭据版本。

为保留恢复记录，历史状态目录不改名：macOS `~/Library/Application Support/asbot-client`，Linux `${XDG_STATE_HOME:-~/.local/state}/asbot-client`，Windows `%LOCALAPPDATA%\asbot-client`。可用 `--state-dir` 指定独立私密目录，必须在仓库和发行包之外；每个安装实例独立凭据，不要复制给另一设备。POSIX 私有权限或 Windows 当前用户 ACL 无法验证时拒绝继续。Windows 实机资格仍待验。

卸载仅恢复本安装的原配置并移除本插件；服务端撤销由管理员操作。不要发送 `enrollment.json`、`bridge.json` 或私有备份，排查只需申请编号、阶段和错误码。
