# A04选择/增量子组原始证据

[实现与交付](../../SERVE_TOOL_STREAM_2026-11-08.md)，[133份原件](index.json)。
源码9d2324b9，581来源核对；不收模型、ELF和私有Key/policy。

- [CPU首轮](cpu-01/result.json)，[能力夹具修复](cpu-02/host.result.json)
  和[Python路径修复](cpu-02/python.result.json)：31 C++、28输入/
  schema、9 Python、4实际JS SDK；[3驱动检查](cpu-03/result.json)。
  build-01路径准备失败保留，build-02/03及原生构建零警告。
- [业务](business-01/result.json)：1启动/7Chat、61.929秒；allowed
  required/auto、严格/双调用增量、实际资料与引用、截断/取消后
  不执行、同PID普通MTP。工具流15片段，双调用15/17片段。
  [严格租约](business-01/lease/exit.json)62.504秒，59次NVML全OK。
- [清理](business-01/cleanup.json)模型退出0，无遗留，端口/GPU锁
  释放；[来源/交付审计](audit.json)复用未改控制面与原生证据。
- [交付](release-01/decision.json)与[安装](delivery-01/result.json)：
  默认945a05ea/6c167898，v4服务d2cc6b9b/dda2ef99；原ledger/
  Key/配额保持STOPPED，安装0/0。[回退](delivery-01/rollback/receipt.json)。

A04的legacy/custom与其余全部总计划继续；不宣称性能或长稳通过。
