# HTTP E2E 验收

顺序与接受条件见 [EVALUATION.md](../../docs/EVALUATION.md)。改动后必要构建，
第一项测试是真实 evalscope HTTP E2E；不运行前置单测、bench 或 profile。

## 质量与同输入性能重放

使用已配置的 `tools/evalscope/.venv`。`run_acceptance.py` 拥有一次 serve
生命周期，默认普通 decode、max_seq=1、max_prefill=8192、max_len=208896。
端口需空闲；它不会结束其他进程或发送连接探测请求。

```bash
python3 tools/evalscope/run_acceptance.py --mode quality \
  --model-dir "$Q4T_MODEL_DIR" \
  --output .q4t-work/e2e/my-change-quality \
  --reference tools/evalscope/fixtures/quality_reference.json

python3 tools/evalscope/run_acceptance.py --mode performance \
  --model-dir "$Q4T_MODEL_DIR" \
  --output .q4t-work/e2e/my-change-performance \
  --reference tools/evalscope/fixtures/performance_reference.json
```

先把 Q4T_MODEL_DIR 设置为本机只读模型目录。输出目录必须尚不存在且位于
build/ 或 .q4t-work/。没有 --fixtures 时按固定算法生成语料；使用 --fixtures
可重放已有 quality-inputs/ 或含 context-N/requests.jsonl 的性能结果目录。
--binary 可选择保存的旧二进制；--port 可选择服务端口。

质量模式包含 11 条原生模板检索题，要求明确答案精确匹配、实际输入计数
匹配并正常 stop。性能模式每档使用同一输入重复三次，要求实际五档长度、
256 输出、length 结束及重复文本一致。--reference 另外要求请求 prompt
摘要和生成文本/摘要与参考一致。失败返回非零并保留响应、数据库、服务日志；
运行中断保留退出信息，缺少结果不能视为通过。

**脚本退出 0 不等于性能无回退已接受**：脚本验证 HTTP、长度、质量或输出
一致性并采集计时，性能仍需按总 decode token/总 decode 时间、每档重复
范围与对照版本分析；不得用任意固定容忍百分比替代现行接受条件。

## 固定参考的边界

fixtures/ 两份 JSON 来自 2026-09-20 完整数学修复后的真实 HTTP 结果。
对应 QSA 提交 c4717cc，二进制 SHA-256：
e98969c8d0d08572437745000d2a741a83b97d09600e60cf2ffd746643b3dcd3。
质量参考只证明这 11 道合成检索题，不是全面模型质量基准。性能参考为便于
后续等价简化而固定的输入/输出及观测时间，**该版本相对旧版仍有约 1%–2%
decode 回退**，不能以此替换原速度目标，见
[精度排障报告](../../docs/ACCURACY_AUDIT_2026-09-20.md)。

原 run_baseline.sh 每档三条不同输入，仍适用于初始负载采集；它与上述
同输入重复矩阵不应混算。disconnect_e2e.py 专门复现断连并验证服务恢复。

所有大体积请求、响应、二进制和原始数据库保留在本地忽略目录；Git 仅保存
生成算法、参考摘要和报告。只处理可信本机 evalscope 数据库，其中响应
由工具存为 pickle，不应使用本脚本解析外部不可信数据库。
