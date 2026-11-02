# SRV-03b严格长响应背压验证

真实结果为未通过，见[报告](../../docs/SERVE_BACKPRESSURE_RESULT_2026-11-03.md)。
冻结驱动0ba39744、专用ELF903c4f77，默认5642eb68/6aca6a3e保持。
长schema响应未在1024 token内闭合，没有观察EAGAIN；不扩大原
6生成/8拒绝/2启动/1800秒预算，实际1/0/1。现有package/execution已经
封存，不重复运行或覆盖；新源码的取证修复不能追认原阶段通过。

- `prepare_payload.py`：真实tokenizer的CPU编码估计、机器合同和生产
  输入探针；243 tokens不是实际受约束生成的上界。
- `stage.py`：同源故障构建、CPU/身份门、只读默认、封包和整阶段共享
  GPU锁。`finalize`只接受完整通过；本轮没有执行接受finalize。
- `verify.py`：真实TCP小窗口、EAGAIN握手、strict完整校验后内容前缀、
  排队/取消/写截止及恢复。当前源码额外修复提前结束时的失败wire取证
  和无`task/*/children`内核的worker发现；这两项只做了CPU检查。
- `test_stage.py`：首次8项7通过/1构建门target错误，定向修复1通过。
  原日志保留，不能将首次失败删除后重新描述为全绿。
- `test_failure.py`：同请求终态、完整/截断失败wire不覆盖、PPid发现、
  本机实际闲置CPU子进程；3+2项定向通过，无模型/HTTP。

本机产物`.q4t-work/serve-backpressure-20261103`和
`docs/evidence/serve-backpressure-20261103`保存来源与seal。原冻结程序
可由Git0ba39744/source.bundle审查；当前工作树已包含后续修复，不能
用当前源码身份冒充原执行身份。无需重装相同默认程序或删除原证据。
