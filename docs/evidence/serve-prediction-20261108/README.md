# A03 prediction原始证据

[实现与交付](../../SERVE_PREDICTION_2026-11-08.md)，[128份原件](index.json)。
源码73dbe96e，581来源核对；模型、ELF和私有Key/policy不入Git。

- [CPU](cpu-01/result.json)：27 C++、22输入/schema、5 Python；
  JS首轮夹具失败保留，[修复2项通过](cpu-02/result.json)。最终
  build-02零警告，build-01警告原件保留。
- [业务](business-01/result.json)：1启动/7Chat、42.512秒；目标验证/
  部分回滚、JSON/SSE、strict/bias、输出预算、取消后greedy MTP。
  [严格租约](business-01/lease/exit.json)43.832秒、42次NVML全OK；
  [清理](business-01/cleanup.json)模型退出0，端口/GPU锁释放。
- [审计](audit.json)核对源码、原件、声明、安装身份。原生两目标
  字节及44控制面文件不变，复用有效采样/TLS/恢复证据。
- [交付](release-01/decision.json)与[安装](delivery-01/result.json)：
  默认d61c98b7/b96c0f35，服务ee95045b/9e17f6c0，原ledger/Key/
  配额保持STOPPED，安装0/0。[回退](delivery-01/rollback/receipt.json)。

不宣称性能改善、全模型质量或长稳通过；A04等总计划继续。
