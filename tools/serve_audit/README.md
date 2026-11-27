# serve 完备性审计工具

当前能力入口为[应用服务矩阵](../../docs/APPLICATION_SERVICE.md)，
最新静态复核见[2026-11-04合同复核](../../docs/SERVE_CONTRACT_REAUDIT_2026-11-04.md)。
当前任务以[Serve总计划](../../docs/SERVE_MASTER_PLAN_2026-11-08.md)为准，
`inventory.json`的实际字段、路由和源码数量由下述检查命令输出；
以下`run.py`预算及`expected_current`属于历史复现，不能重跑后当作
当前正确性验收，也不能把该清单当作Python应用/托管工具的全面审计。

2026-11-07 已核对新增 Guardian host 构建、原生进程/异步账本及
Python 恢复政策来源，纳入上述锚点；原 Chat 字段、路由和白名单
未变。见[自动恢复 A/B](../../docs/SERVE_SELF_RECOVERY_AB_2026-11-07.md)：
114 项 CPU 合同已通过，包含原生控制循环、模型替身恢复、前后台
内部握手、exec 屏障与代次负控；统一生产命令、真实模型/网关与
GPU 恢复尚未交付，CPU 替身不能作为真实 LLM 业务接受。

初次冻结基线为 `7606435`，运行时不作修改。范围覆盖当时六条路由、
Chat 请求字段和嵌套消息、模板 / 输出解析、JSON / SSE、取消、资源
生命周期、观测与兼容声明；不把新增 Responses、媒体等作为验收目标。

`run.py` 用生产请求合同、模板、输出 parser、HTTP reader / dispatch
作有界复现。模型输出来自夹具；Chat HTTP handler 是显式 stub，
只证明 HTTP 接入 / dispatch，不证明生成成功。取消路由及 registry
使用生产实现。未调用 `Start/Run`、CUDA 或模型，没有监听端口。
编译复用原发布的 tokenizer / IO 静态库与生成身份头，记录 SHA256。
IO 库需要 CUDA runtime 链接依赖，但不执行 CUDA API。

固定预算：一次零警告构建（60 秒）和一次 probe（20 秒），不扩展模型
测试。失败保留原目录；工具失败修复后只重验本工具。所有产物进
`build/` 或 `.q4t-work/`；HTTP 代码探针仍经过共享互斥 guard。

```bash
/usr/bin/python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/serve-audit-20261102/lease -- \
  /usr/bin/python3 -B tools/serve_audit/run.py \
  --engine-build .q4t-work/structured-control-20261102/engine \
  --output .q4t-work/serve-audit-20261102/run
```

`expected_current` 描述待审计版本的观察结果，包含缺陷行为。
`all_observations_match=true` **不代表缺陷修复或发布验收通过**。
修复时应把相关夹具转为正确行为的回归测试，不维持错误行为来使本
历史探针通过。完整审计结论、证据和后续退出条件见
[serve 审计](../../docs/SERVE_COMPLETENESS_AUDIT_2026-11-02.md)。

`inventory.json`为顶层字段和路由逐项登记语义、限制及证据引用。
初次审计为35字段/6路由、10处白名单/51源码锚点，输入合同A组后为
16处白名单/53锚点；后续扩展至上述当前数量。受影响字段必须指向
对应修复合同与回归，不将锚点数量当作语义覆盖率。
`python3 -B tools/serve_audit/check_inventory.py --self-check` 检查代码
是否新增未登记接口、删除已有接口或使审计来源过期。CI 同步执行。
同时要求全部`src/server`及`include/q4t/server`的C++源码/头文件进入
协议身份清单，遗漏即失败；源码和头文件各有一项遗漏负控。
更改源码后必须重新评审受影响合同、更新结论及来源身份；禁止只为
通过检查盲目刷新 hash。该检查不理解业务语义，也不代替人工审查和
真实业务验收；OpenAI / vLLM 外部协议变化仍需定期人工核对。

对冻结的官方 OpenAPI 运行以下命令，可检查已声明方法的查询默认值。
报告区分匹配、缺失、不同和多义的默认值，保留原路径和字段。
省略参数的运行结果由对应的原生用例验证。

```bash
python3 -B -m tools.serve_audit.scope_queries /path/to/official-openapi.json --defaults
```

## 已鉴权的 Conversations 方法

`conversation_business.py` 和 `conversation_business.mjs` 使用当前安装的
官方 SDK 创建一份独立会话、按真实游标翻页、验证第二把密钥的访问范围并
删除本次数据。请求记录只包含方法、路径、查询、状态码和请求 ID。

对已运行的测试服务，可用目标 Python 解释器运行
`python -B -m tools.serve_audit.conversation_business`，或运行
`node tools/serve_audit/conversation_business.mjs /path/to/node_modules/openai`。
将 JSON 配置从标准输入传入。字段 `base` 为 HTTPS 的 /v1 地址，
`certificate` 为信任证书的文件路径，`key` 和 `peer` 为两个独立测试
客户的密钥，`facts` 为两条可核实的项目记录。Node 进程启动前将
`NODE_EXTRA_CA_CERTS` 设为同一个证书路径。启用 `generate: true`
时还需传入 `model`、`question` 和预期答案字符串 `expected`，用于
验证模型通过已存会话读取记录。传入 `generate: false` 可只操作会话和条目。

可以用以下命令复核已提交的六版 SDK 原始请求和分页、归属反例。

```bash
python3 -B -m unittest tools.serve_audit.test_conversation_receipt
```
