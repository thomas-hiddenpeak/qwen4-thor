# R06 已验版本回退交付

2026-11-12。16 条实际 CLI 命令首次全部通过，48.926 秒，0 新增 GPU
启动。默认发布 revision 64、STOPPED，包 `a3bb4ec8`、generation
`409f63a6`，模型 `96577e45` 及其发布元数据字节保持。完整身份在
[原始结果](result.json)，原始文件哈希在 [清单](files.sha256.json)。

依次检查原版、恢复包与候选，登记旧控制根保留锚点，发布新版本，
登记恢复周期，然后实际执行 STOPPED 旧根回退（revision 62）及
继续升级（revision 64）。最终 check/status/observe/profiles-status/
version-status/publish-status 全部通过。原 Key、用量、核心启动预算
与 profile 状态字节不变；注册后的版本状态与不可变注册也保持。
恢复周期 `ca5419bc-9700-4224-99a7-aebbb171b2b2` 为 revision 0、
primary、未消费、无 pending、无保护。交付没有启动模型。

复用 [实际整树回退业务](../serve-version-business-20261112/README.md)：
2 模型启动、10 POST、1 CUDA probe；固定 Guardian 切换到已验旧服务
payload，工具 SSE、原 Chat/Batch/File、RPM 用量及清理通过。
两个服务 payload 使用同一已验模型二进制，控制源码不同；不将其
描述为两个不同模型二进制的 GPU 验收。实测后唯一封包源码差异是
STOPPED 显式修复助手，运行 prepare 函数 AST 保持，另有
[5 项针对性 CPU](../serve-version-legacy-root-repair-20261112/README.md)
及本次真实停机回退/升级验证，不重复模型业务。

自动回退 payload 为 `10155da3`，来自已验 R05 包，只适配容量历史
reader。完整旧控制根手工回退锚点为 generation `f07eead5`，不声称
旧根具有新 R06 自动恢复能力。保留包、凭据与具体命令模板见
[恢复锚点](recovery-anchor.json)。周期登记不清除既有核心隔离保护，
已消费周期不能通过重复请求或仅换 UUID 获得新的自动重启额度。

实际命令、退出码及 stdout/stderr 均归档；准备审核在 [review](review.json)，
交付决定在 [decision](decision.json)。默认配置容量、原生 TLS 可选性
及内存策略保持，视觉候选保留，长稳由用户运行。本阶段未合 main；
继续 R07 及总计划所有剩余任务，不将本次交付当作总目标完成。
