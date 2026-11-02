# 聊天业务验收证据

状态：**CPU_PASS_GPU_ADMISSION_ENV_ABORT**。源码bef9d24，候选
830694ce。CPU五项共157个检查通过；真实进程故障及模型HTTP未执行。
详见[结果与剩余工作](../../CHAT_BUSINESS_ACCEPTANCE_2026-11-02.md)。

- `frozen-plan.json`：完整源码/产物/配置身份及原预算。
- `cpu-01/`：97 host、22客户端、13恢复、4编排、21模板的原始命令、
  日志与退出记录。`cpu.py`及`cpu-01.log`记录独立CPU执行过程。
- `preflight/`：两次只读vLLM指标及摘要，不能证明采样间隔无业务。
- `admission-result.json`：实际锁持有者及外部恢复标记，ENV_ABORT /
  LEASE_BUSY；服务变更、模型启动和生成请求均为0。
- `lifecycle.py`、`lifecycle-identity.json`、`lifecycle.log`：原拟
  临时停机的脚本与首次获取锁失败现场。锁之后的代码未执行，
  同容器恢复假设已不适用，禁止把它当作已验证的交接入口重试。
- `result.json`：结果和结束时产物身份，默认程序及sidecar保持。
- `sealed-files.json`：34份原始文件SHA256，不包含本README及封条自身。

这不是完整业务接受或部署证明。真实故障8项与23生成/10负例/3启动
仍待GPU服务交接，后续复用身份未变的CPU证据，不无故补跑。
