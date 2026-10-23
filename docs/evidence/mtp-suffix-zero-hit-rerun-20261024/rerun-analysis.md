# 修复后零命中诊断（56请求/12服务）

VALID_INDEPENDENT_AUDIT_BOUND；仅描述性复核，工程决定另审。

| 重复材料 | 完整ON/OFF | 子集B/A1 | 子集B/A2 | 共享B几何均值 | 开发目标 |
|---|---:|---:|---:|---:|---|
| repeat-code | 1.142130 | 1.066675 | 1.069318 | 1.067996 | True |
| repeat-text | 1.145357 | 1.064153 | 1.061507 | 1.062829 | True |

| 长探针 | 比较 | TTFT差ms | target prefill完成差ms | 首内容后差ms | decode变化 |
|---|---|---:|---:|---:|---:|
| context-8192 | subset A2-A1 | 1.767 | -12.058 | 26.223 | -0.397% |
| context-8192 | subset B-A1 | 12.930 | 1.537 | 41.988 | -0.634% |
| context-8192 | subset B-A2 | 11.163 | 13.595 | 15.766 | -0.238% |
| context-8192 | full ON-OFF | -28.468 | -21.515 | -12.804 | +0.174% |
| context-8192 | timing ON-OFF | -6.516 | -9.870 | 19.365 | -0.292% |
| context-45056 | subset A2-A1 | -17.675 | -13.069 | 1.527 | -0.024% |
| context-45056 | subset B-A1 | -4.054 | -4.682 | 24.746 | -0.382% |
| context-45056 | subset B-A2 | 13.622 | 8.387 | 23.219 | -0.358% |
| context-45056 | full ON-OFF | -33.623 | -26.612 | -1.732 | +0.024% |
| context-45056 | timing ON-OFF | -27.804 | -23.630 | -74.015 | +1.141% |
| context-204800 | subset A2-A1 | 207.160 | 198.807 | 19.772 | -0.265% |
| context-204800 | subset B-A1 | 275.549 | 264.826 | 27.529 | -0.369% |
| context-204800 | subset B-A2 | 68.389 | 66.019 | 7.757 | -0.104% |
| context-204800 | full ON-OFF | -71.410 | -53.699 | 14.429 | -0.187% |
| context-204800 | timing ON-OFF | 36.829 | 30.120 | -6.287 | +0.084% |

| 首long | TTFT ON−OFF ms | prefill完成差ms |
|---|---:|---:|
| context-45056 | -41.895 | -39.678 |
| context-204800 | 34.276 | 14.379 |

| 插桩探针 | prefill event总差ms | 舍入界±ms | handler区间−event残差差ms |
|---|---:|---:|---:|
| context-8192 | -9.760 | 0.010 | -0.080 |
| context-45056 | -23.070 | 0.060 | -0.163 |
| context-204800 | 29.170 | 0.250 | 0.585 |

所有同head输出SHA逐请求核对一致。原值、中位、接受/目标工作、native整代和首内容proxy、逐chunk与generation层事件量在JSON。

请求suffix init/query/append/release分别计入；numeric嵌套于query，未重复相加。共享分类表单独记录每服务初始化/释放和请求后销毁。
统一RAM概况使用全阶段MemAvailable及请求窗口RSS；逻辑分配字节不等于物理RAM差，不把重叠峰值相加。

- This parser checks consumed seals and environment completion, not a replacement for the independent full identity/model/resource audit.
- Normal and instrumented services stay separate; layer timing creates/events/synchronizes/prints on every forward and changes native phase observations.
- All material is previously seen development material. Repeat medians are same-startup summaries; a shared subset B does not create independent blocks.
- Native values printed0.1ms: each phase/row ±0.05ms; pair differences sum both arms, three-phase sums multiply by3.
- Layer values printed0.01ms: each reported total/row ±0.005ms; pair differences sum both arms. Printed total and sum of groups have separate rounding.
- Layer embed marker begins after embedding launch; adjacent layer event slots are rerecorded, potentially including PLE/host gaps in preceding spans. top5 is partial, not complete per-layer data.
- Layer total is device event elapsed including stream idle, not pure GPU busy. Handler-prefill minus event sum is an unmatched-window residual, not CPU time.
- First content from retained SSE maps to an emitting native round. Removing that round is a server-emission proxy, not an exact client TTFT cut; tail/SSE/scheduler/cleanup/overlap remain in residual.
- Generation target work excludes target prefill and full-prompt draft extend. numeric_ns is already included in query_ns.
- 1Hz samples and nearby proc brackets are not exact boundary counts; new/exited threads and interval spill remain. Frequencies/IO/temperature covariance does not prove causation.
- This56/12 diagnosis is not a new five-tier/plain-decode acceptance and cannot erase the prior protection NO_GO or authorize deployment.
