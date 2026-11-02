# serve 输入合同 A 组验收

`verify.py` 复用固定 OpenAI SDK 和正式工具客户端，读取真实项目文档。
candidate plain：strict SSE 两轮、SDK null、指数整数，共 4 次生成；
candidate MTP：普通 auto JSON 两轮，共 2 次；安装健康 2 次。拒绝
请求 16 个 Chat、3 个取消。总计 8 生成 / 19 拒绝 / 3 启动，在冻结的
10 / 24 / 3 和 30 分钟上限内。没有自动重试或额外模型探测。

`stage.py` 先封存旧 pair / candidate / 构建、CPU、源码和模型身份，
复用已有安装 / 回退事务；HTTP 生命周期支架 5 项在同一 GPU 锁中先
执行，其中取消 registry 是生产实现，Chat/能力/健康 handler 为 stub。
支架不证明模型生成；随后真实模型、SDK、JSON/SSE 往返独立验收。
安装前源文件与 candidate 身份再次核对，安装健康失败回退已知 pair。

```bash
python3 -B tools/input_contract/stage.py prepare
/usr/bin/python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/input-contract-20261102/execution/lease -- \
  .q4t-work/application-service-20261102/sdk-env/bin/python -B \
  tools/input_contract/stage.py execute
python3 -B tools/input_contract/stage.py finalize
```

`test_stage.py` 为纯离线合同，检查原始数字文本、两种工具路径的真实
只读执行 / 回填、不完整工具不执行、预算和部署互斥；不加载模型。
完整边界见[修复记录](../../docs/SERVE_INPUT_CONTRACT_2026-11-02.md)。
