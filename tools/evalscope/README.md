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

quality_reference.json 来自 2026-09-20 完整数学修复后的真实 HTTP 结果，
对应 QSA 提交 c4717cc，二进制 SHA-256：
e98969c8d0d08572437745000d2a741a83b97d09600e60cf2ffd746643b3dcd3。
只证明这 11 道合成检索题，不是全面模型质量基准。

performance_reference.json 更新为 2026-09-21 短上下文索引打分 CTA 映射
完整五档结果，二进制 SHA-256：
e22a69026a2ad5182d00d4243f7289f0e364f4e466b6480fb9fae9b8d8c90279。
输入/输出摘要一致，计时整体更新，不拼接最优档位；旧参考保留在
Git 历史、原报告及本地 performance-before.json。
五档 decode 18.599/17.737/18.130/17.857/17.085 tok/s，4K 较前一
接受版提升约 6.20%，其余四档和全部 TTFT 范围重叠。
完整 E2E 后 288 组、589824 个 FP32 分数逐位一致；配对 4K HTTP
打分累计 1158.343→280.322 ms，调用数与编译资源不变。
见 [索引打分报告](../../docs/INDEXER_DECODE_2026-09-21.md)。
后续仍逐档比较，不能因质量输出不变就忽略性能回退。

原 run_baseline.sh 每档三条不同输入，仍适用于初始负载采集；它与上述
同输入重复矩阵不应混算。disconnect_e2e.py 专门复现断连并验证服务恢复。

所有大体积请求、响应、二进制和原始数据库保留在本地忽略目录；Git 仅保存
生成算法、参考摘要和报告。只处理可信本机 evalscope 数据库，其中响应
由工具存为 pickle，不应使用本脚本解析外部不可信数据库。

## 已通过 E2E 后的时间线诊断

仅对已经按 EVALUATION.md 接受的同一二进制执行；不是前置测试，也不替代
五档正式性能验收。profile_http.py 校验既有五档 HTTP/输出记录与二进制
SHA，加载后才开启 Nsight，每档重放一条同输入 256 输出请求并核对摘要。
每条独立 trace，结束服务后导出 SQLite；失败保留证据。需要本机 nsys。

```bash
python3 tools/evalscope/profile_http.py \
  --accepted-run .q4t-work/e2e/position-metadata-20260920/performance \
  --model-dir "$Q4T_MODEL_DIR" \
  --output .q4t-work/e2e/my-http-timeline
python3 tools/evalscope/analyze_http_trace.py .q4t-work/e2e/my-http-timeline
```

--lengths 可指定诊断子集，不能把子集称为完整五档；profile 结果带采集开销，
不得回填 performance_reference.json。分析依赖当前单流普通路径的 logits
和 argmax 回读形状，遇到不同结构会报错。GPU busy 使用区间并集；kernel
累计时间、重叠 API 等待与 GPU 窗口不能相加。详见
[HTTP 时间线报告](../../docs/HTTP_TIMELINE_2026-09-20.md)。

## 输出上限边界 E2E

run_acceptance.py 的 --mode limits 使用固定 1024-token prompt，分别验证
流式与非流式的 max_tokens=1/2/8。先用 --binary 保存旧版结果，再以
--reference 指向该 results.json 重放候选，要求实际输入/输出长度、length
结束、请求 stream 字段和生成文本一致。非流式显式传 --no-stream；
evalscope 默认 stream=true，不能靠省略 --stream 来关闭。

此模式用于输出边界，不替代固定质量题与五档性能矩阵；不把非流式的
TTFT 字段或单次短输出结果解释成 decode 吞吐。

固定 limits 参考已保存为 fixtures/limits_reference.json，来自已接受的
位置元数据版本（SHA dbefb9dd794ebb1da71471d19709b3fb1dbec6415f6f8e47994b124ddb3401a8）
真实流式/非流式重采；可直接以 --reference 使用，不需要每次重跑旧二进制。
它只保存边界输出，不作为性能参考。
