# A01/A02消息子集：原始证据

结论及边界见[实施记录](../../SERVE_CHAT_INPUT_2026-11-08.md)。
[index.json](index.json)逐文件登记原始字节摘要；保留首次准备/夹具
和审计失败，不改写为成功。凭据、模型和可执行文件不入Git。

- [CPU初次](cpu-01/result.json)：33项C++、71个形状/语义用例、
  19个提示/tokenizer、1项实际SDK；媒体feature-gate夹具分类和生成
  来源摘要失败另行修正。[单项修复](cpu-02/shapes/result.json)复用70项。
  [驱动负控](cpu-02/driver.log)3项通过。
- [业务结果](business-01/result.json)：1启动/6Chat，47.483秒；
  [严格租约](business-01/lease/exit.json)48.191秒、46次NVML全OK。
  实际Node SDK的JSON/SSE、名字区分、refusal、无user及资料工具
  标题/引用闭环通过，同模型PID；[清理](business-01/cleanup.json)。
- [独立核对](audit.json)：567来源与271运行时来源、各进程退出及
  锁/端口释放。新构建CUDA探针与旧包不同，保留差异记录并继续用
  已验原探针；新探针没有被交付或宣称通过，没有为此追加GPU验证。
- [交付决定](release-01/decision.json)、[安装回执](delivery-01/result.json)：
  当前默认引擎47d33690/元数据ef9183ed，v4服务4e4e1872/元数据d05b470a。
  44个控制面运行文件相同，原策略、Key、配额及ledger字节保持；
  安装0启动/0Chat，服务仍STOPPED。[回退](delivery-01/rollback/receipt.json)。

本组不是完整OpenAI兼容、性能或全模型质量验收。剩余总计划继续，
旧有效证据只复用身份与依赖未改变的部分。
