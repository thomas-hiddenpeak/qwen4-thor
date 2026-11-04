# 语法编译隔离验收

业务资源治理，最多12请求尝试/6拒绝/3启动/30分钟；冻结实际10尝试、
1拒绝、2启动。复用已验收的pair安装/回退驱动，整阶段持一把GPU锁。
仅暂停自建服务器的、同ELF且父PID核实的私有worker（pidfd），验证
编译超时；再验证编译取消、严格/普通混合、真实工具、shutdown和健康。
没有生产故障注入开关。限制是worker用户地址空间，不是整机RAM。

```bash
python3 -B tools/grammar_isolation/stage.py prepare
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/grammar-isolation-20261102-repair/execution/lease -- \
  /home/rm01/models/dev/qwen4-thor/.q4t-work/application-service-20261102/sdk-env/bin/python -B \
  /home/rm01/models/dev/qwen4-thor/tools/grammar_isolation/stage.py execute
python3 -B tools/grammar_isolation/stage.py finalize
```

[实现边界、冻结CPU清单及结果](../../docs/SERVE_GRAMMAR_ISOLATION_2026-11-02.md)。

首轮因本机无proc children接口在1次尝试/1启动后退出，原现场保留。
修复后仅剩2启动/11尝试上限；沿用第一次begin时间和30分钟总窗口。
