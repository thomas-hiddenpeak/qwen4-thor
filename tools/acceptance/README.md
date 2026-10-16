# MoE 分层驻留 — 最终验收工具链（B=12288，C1..C6）

最终候选：C=256 每层命中 top-n（hot-final-12288.json），L2-16，
C4 镜像环 K=8，C5 页缓存保留，C6 max-open-shards=200（全 197 分片常开）。
内存门按用户授权口径记录超支（USER_APPROVED_OVERRUN，2026-10-01 07:25）。

## 验收链（顺序执行，GPU 独占）

```bash
# 0) 前置：verify-c1 六查 PASS（K=8 最终候选；证据 verify-c1.log FAIL=0）
# 1) 质量+业务门（新二进制受影响项重验）
bash final-quality-business.sh 12288
# 2) 全矩阵（C3 页缓存协议：基线 C=0 + 候选，五档+261887 目标档，每档 3 次）
bash final-acceptance.sh 12288
# 或 1+2 自动串联（1 失败则不进 2）：
bash chain-quality-acceptance.sh
```

## 脚本职责

| 脚本 | 职责 |
|---|---|
| chain-quality-acceptance.sh | 串联质量/业务门 → final-acceptance |
| final-quality-business.sh | 质量 11 题 manifest-exact + 基线/候选逐位；业务 6 请求逐位（B 参数） |
| bitexact-final.sh | C=0 vs 候选 1024/8192 逐位（B 参数） |
| final-acceptance.sh | 基线+候选 C3 协议全矩阵 → compare_e2e → 内存门（B 参数） |
| c3-pagecache-protocol.sh | C3 页缓存协议：drop_caches→启动→45056 预热→证据→（--acceptance 时）矩阵；传播 Q4T_MOE_MAX_OPEN_SHARDS（C6，默认 200） |
| run-e2e.sh | 单配置全矩阵（tools/evalscope/run_acceptance.py + monitor_memory.py） |
| compare_e2e.py | 冻结口径对比（2026-09-30）：逐档调和均值 decode ≥50%、首/后续分列、TTFT/耗时、目标档 token 合同（in=261887/out=257/finish=length）、加载量、内存峰值 |
| budget_backfill.py | 内存账回填（[q4t][budget] + memory-peak.json 逐 tag 表格） |
| backfill_6b.py | 验收报告 §6b markdown 块生成（compare 报告+内存门+C3 证据） |
| verify-c1.sh | 六查（单测/bitexact/跨二进制恒等/补载失败/取消/预算+定向 45056）；针对 path-c worktree 二进制，证据见 .q4t-work/.../verify-c1.log |
| pilot-45056-c6.sh | final-acceptance 前单档试点（C3 协议，45056×3，分段计时） |

## 关键配置（冻结）

- 目标档：总上下文 262144（in=261887 + out=257，finish=length）
- 请求超时：read 7200s / total 10800s（runner --request-deadline-ms 10800000）
- 内存门：service_total_physical_peak_bytes（rss+gpu）+ 模型页缓存增量
  （C3 协议 post-matrix − post-load）≤ 54,000,000,000 B；B=12288 超支
  标注 USER_APPROVED_OVERRUN
- 性能门：各档 decode 调和均值 ≥ 基线 50%（本目标专项门槛）
- 正确性：bitexact + 质量 + 业务 + 受影响检查（补载失败/取消/槽位复用/
  跨请求状态）

## 回退

见 docs/MOE_RESIDENCY_DEPLOY_2026-09-30.md §3（C4/C5/C6 分级回退）。
