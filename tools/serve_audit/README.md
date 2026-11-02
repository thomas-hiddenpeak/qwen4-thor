# serve 完备性审计工具

本轮冻结基线为 `7606435`，运行时不作修改。范围覆盖现有六条路由、
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

`inventory.json` 为 35 个顶层字段和 6 条路由逐项登记语义、限制及
证据引用，并固定 10 处直接白名单与 51 个服务相关源码锚点。
`python3 -B tools/serve_audit/check_inventory.py --self-check` 检查代码
是否新增未登记接口、删除已有接口或使审计来源过期。CI 同步执行。
更改源码后必须重新评审受影响合同、更新结论及来源身份；禁止只为
通过检查盲目刷新 hash。该检查不理解业务语义，也不代替人工审查和
真实业务验收；OpenAI / vLLM 外部协议变化仍需定期人工核对。
