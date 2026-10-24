# Nsight 阶段终态与首个 fixture 离线复核

阶段为 **ENV_ABORT**，资源监控失败后在 5.229048 秒停止。0 模型加载、0 HTTP；
只启动 direct-stop，已留下导出成功的 SQLite。在线 fixture group、负控、其余三个策略和模型对照均未完成。

本次按授权仅调用一次已冻结 `fixture_audit.audit`，以 PID 3189205 绑定现有 SQLite/服务日志，
并作只读 SQL 复核；没有运行负控副本、测试、HTTP、GPU 或重新采集。

## 局部结果

- 已知工作量闭合：`True`；局部 fixture 核心合同：`True`。
- 32 次独立输出校验全部零 mismatch，显式 release/main-return receipt 为0；直接模式退出记录为 pidfd 退出观察和 launcher 0，不冒充 waitpid 子进程状态。
- 应用记录：{"application_launches": 32, "application_kernels": 32, "application_copy_apis": 64, "application_copies": 64, "application_memset_apis": 32, "application_memsets": 32, "fixture_nvtx": 33, "mapped_steps": 32, "other_thread_apis": 0}。
- 固定非零 memset coverage：`COMPLETE`，完整标志 `True`；expected bytes分组 {"512": 16, "16384": 16}，observed {"512": 16, "16384": 16}，unmatched {}。
- 目标进程 Warning 2 条；未知 severity 0 条；fixture auditor errors []。
- 本地负控未运行；没有 candidate gate，也不补造在线完成收据。

## 环境与推断边界

监控对同身份 fixture 进程读取 `io` 得到 EACCES，随后 pidfd 有界等待未观察到退出；
原 ENV_ABORT 判定保留。外层 lease 的 COMMAND_FAILED 是驱动退出码口径，不覆盖
state/postflight 的 ENV_ABORT。完整环境/清理结论仍须绑定独立阶段审计。

本 fixture 的512/16384-byte memset结果仅适用于这一固定形状、direct-stop策略与这份记录。
它不能证明旧6381个未匹配 memory API 是零长度、合法无GPU活动或真正丢失，也不能给出白名单。
旧6216=74×84与小N cuBLASLt投影次数吻合，仍只是源码候选关联。

当前没有策略对照、8K/200K GPU归因或新性能结论；两种head重复收益只复用上一阶段原有效范围。
工程决定是保留现有交付，归档本次候选和失败证据，不自动追加采集。

## 身份

- Plan SHA256：`a995b1e6526da86dddaaca9870c1bebe875bc3f69666bf2ae1a31c0dd7d41a8a`。
- Frozen fixture auditor SHA256：`e0ccc3fba1a6dd6ff3785c4ee98e9f7b4229ab1c2aa222590ad62c9e0ed3dd8e`。
- SQLite SHA256：`ae86ef105a44e615369cf0186b2db1fc304a0504ff34fa20742927e983447dbb`。
- JSON分析 SHA256：`7720f878919505a13acd8a903f2f8b0e31a0c7e2ba2872891302db4a7da9138d`。
- run/files.json中全部 22 项在读取前后SHA一致；未写任何raw或冻结文件。
