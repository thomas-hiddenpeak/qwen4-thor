# A03采样子组：原始证据

[实现与交付](../../SERVE_SAMPLING_2026-11-08.md)，[180份原件索引](index.json)。
源码94c0fc65，模型、ELF、私有Key/policy不入Git；源码用Git和摘要核对。

- [CPU首轮](cpu-01/result.json)及[受影响项修复](cpu-02/result.json)：
  56 C++、66 schema、53 Python、41 JS；3驱动和1tokenizer见cpu-04，
  显式choice执行负控见[cpu-05](cpu-05/result.json)。失败原件保留。
- [业务](business-01/result.json)：1启动/9Chat、55.480秒；JSON/SSE
  多choice、seed、bias、strict、真实资料工具、取消后greedy MTP通过。
  [严格租约](business-01/lease/exit.json)55.777秒、53次NVML全OK。
  [清理](business-01/cleanup.json)模型退出、端口与锁释放。
- [审计](audit.json)：576来源、最终构建、声明与安装身份；
  原生入口/Guardian及44控制面文件不变，复用有效恢复/TLS证据。
- [交付决定](release-01/decision.json)与[安装](delivery-01/result.json)：
  7d157168/b3dde202、服务78b76a7b/456fb5bb；STOPPED及原ledger、
  Key/配额不变，安装0启动/0Chat。[回退](delivery-01/rollback/receipt.json)。

不宣称性能或全模型质量验收。prediction与其余总计划继续实现。
