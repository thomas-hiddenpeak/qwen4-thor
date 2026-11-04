# SRV-07验收工具

范围与预算见[冻结合同](../../docs/SERVE_PREFLIGHT_2026-11-03.md)。
`prompt_probe.cpp`使用生产规划/模板/Tokenizer，不调用Start、CUDA或模型；
`template_diff.py`以固定版本checkpoint Jinja与tokenizers作独立字节/ID对照，
包括已有角色/推理历史、文本范围拒绝、严格模板、长工具历史及raw输入。
probe链接runner库不意味着加载模型；不能把CPU预检当真实生成验收。

```bash
cmake --build .q4t-work/serve-preflight-20261103/engine --target q4t_preflight_probe
/usr/bin/python3 -B tools/serve_preflight/template_diff.py \
  --probe .q4t-work/serve-preflight-20261103/engine/q4t_preflight_probe \
  --output .q4t-work/serve-preflight-20261103/cpu/template-diff
```

请求包装与响应机器合同见`docs/serve-preflight-v1.schema.json`；配置
见`docs/serve-config-v1.schema.json`。完整Chat输入schema仍由SRV-08跟踪。
