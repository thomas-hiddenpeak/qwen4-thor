# N07 真实容量与生命周期接受

2026-11-09：修复后短验通过；首容量传播失败保留在
[失败与修复目录](../serve-lifecycle-capacity-fix-20261109/README.md)。
两轮累计 2 次启动、6 次实际 Chat（含两次自检）、206.699 秒严格
lease，194 次 NVML 全 OK；最后正常 STOPPED、自有模型/端口/租约清理。

实际 context 4096、prefill 1024、默认 output 128、inflight 6、queue 4、
deadline 180000ms 与模型/独立入口一致。JSON/SSE 均得 42；真实读取
项目 docs/MODEL.md 前三行并返回精确引用；限额热更新 live_applied，
保持同一模型 PID/出生/generation。生产 CLI handler run/check/status/
reload/stop 在只计数的验证包裹器中执行；C++ 实际分派和发布回退
采用先前针对 CPU。未执行未包裹 ELF run，不夸大该条证据。

默认程序尚未替换。随后封装已接受发行、实际原生 ELF check/status
及默认代发布/真实旧代回退；全部总计划继续，不标记总目标完成。
未运行完整性能/全模型质量或用户负责的长期稳定验证。
