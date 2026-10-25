# 给用户私人 Agent 的操作约束

先读取 CLIENT-README.md 和可信渠道提供的 clientRelease，核对 zip SHA-256、完整源 SHA 与 release.json 文件校验。只使用 bin/client.py 的 setup、activate、status、doctor、update、uninstall 命令。

- 使用用户已有 Hermes 与 Python 环境。客户端版本不兼容时报告阻塞；不得静默安装或升级 Hermes、改模型/搜索/审批、重新登录微信、清空状态或启动新 Gateway。
- 只选择 Hermes 已批准的本地微信私聊。有多个 Profile 或 peer 且用户未指定时让用户在本地选择；不得猜测、扩大批准范围或把原始 peer ID 上传服务器。
- 邀请码由用户在隐藏终端提示输入。不要向用户索要恢复密钥、Access Secret、Bearer，不读取或复制私密配置内容进对话，不把秘密放入 argv、日志、环境变量或截图。反馈只需要阶段、错误码和申请 ID。
- 中断后用同一状态目录继续；不得删除恢复文件、生成新申请或重发结果未知的写入。申请不确定、过期、撤销或已拒绝时联系管理员。
- 安装只管理本插件。已有插件归属冲突、后续配置变更、权限或源码指纹校验失败时保留现场并报告具体错误。重启现有 Gateway 必须由用户明确确认并安排空闲窗口。
- `ACTIVE` 和 doctor 是 API/配置证据。没有真实微信收图、引用及忙碌命令通道回执时保持 qualified:false，不能宣称生产或设备资格通过。
