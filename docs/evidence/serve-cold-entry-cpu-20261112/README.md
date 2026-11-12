# 原生公开冷入口：针对性 CPU 验收

12 不同用例全部首过，累计 16.037 秒。9 入口/合同用例与 3 封包用例，命令、输出、时间及默认身份在原始记录中。

资源绑定 `device-domain enter` 通过所选 root 保护宿主入口启动；`host-entry start` 和设备入口 `--activate` 显式改变启动意图，普通 `enter` / 自动 `ensure` 保留人工 STOPPED。实际私有 CPU 租约通过两次 root exec 和永久降权仍保持同一 inode/OFD，错误 FD、所有者、路径、manifest 和任意命令被拒绝。

真实 root/普通 UID、前台/后台、核心持久意图和撤销清理已验证。设备保持 unarmed，启动测试确认零角色派生；sealed 回执与只读一次性管道使用真实内核 FD，适配测试的两个 BPF FD 明确是普通文件夹具，不代表设备准入验收。

0 模型 / 0 GPU / 无宿主根设备规则，不改变默认 rev64 或既有服务。issuer 进程失能恢复、实际冷业务与组合发行及全部总任务继续。[审查](review.json)。
