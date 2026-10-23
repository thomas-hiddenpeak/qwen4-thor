# FORENSIC_ONLY_ENV_ABORT

本文件仅保留 ENV_ABORT 阶段的法证观测，不恢复 VALID，不作性能接受或候选触发。
所有 26 条 HTTP 与 6 个服务退出均早于失败采样；该事实不消除阶段监控失败。根因仍未知。

## FORENSIC_ONLY_ENV_ABORT：全部请求原值

首门未插桩，仅列值；其余五服务启用原生计时。时间单位 ms，decode 为 tok/s。

| 服务 | case | 首请求 | 输入/输出 | TTFT | handler prefill 完成 | handler prefill 区间 | post-TTFT | decode |
|---|---|---|---:|---:|---:|---:|---:|---:|
| first-http | context-8192 | True | 8170/256 | 5844.524790 | 5198.345 | 5189.674 | 7359.512439 | 34.649034 |
| subset-off-a1 | context-8192 | True | 8170/256 | 5644.487103 | 5194.490 | 5185.848 | 6614.733269 | 38.550307 |
| subset-off-a1 | context-45056 | False | 45044/256 | 31769.907504 | 30383.217 | 30344.779 | 6472.390579 | 39.398117 |
| subset-off-a1 | context-204800 | False | 204781/256 | 166878.676530 | 159149.622 | 158983.448 | 7461.711469 | 34.174465 |
| subset-off-a1 | repeat-code | False | 744/256 | 817.669490 | 695.464 | 693.717 | 5409.891996 | 47.135876 |
| subset-off-a1 | repeat-text | False | 475/256 | 638.881094 | 524.759 | 523.549 | 5580.541419 | 45.694491 |
| subset-on-b | context-8192 | True | 8170/256 | 5646.870273 | 5190.663 | 5182.046 | 6608.714578 | 38.585416 |
| subset-on-b | context-45056 | False | 45044/256 | 31819.786606 | 30432.566 | 30394.184 | 6466.542425 | 39.433747 |
| subset-on-b | context-204800 | False | 204781/256 | 167167.854131 | 159438.701 | 159272.176 | 7455.784987 | 34.201630 |
| subset-on-b | repeat-code | False | 744/256 | 819.243212 | 696.767 | 695.528 | 5053.128499 | 50.463787 |
| subset-on-b | repeat-text | False | 475/256 | 639.695469 | 524.359 | 523.134 | 5239.738055 | 48.666555 |
| subset-off-a2 | context-8192 | True | 8170/256 | 5628.631509 | 5181.315 | 5173.557 | 6589.828808 | 38.695998 |
| subset-off-a2 | context-45056 | False | 45044/256 | 31796.289515 | 30412.776 | 30373.571 | 6460.334658 | 39.471639 |
| subset-off-a2 | context-204800 | False | 204781/256 | 166847.897259 | 159134.397 | 158974.091 | 7456.952078 | 34.196277 |
| subset-off-a2 | repeat-code | False | 744/256 | 820.491215 | 698.530 | 696.527 | 5409.594609 | 47.138468 |
| subset-off-a2 | repeat-text | False | 475/256 | 642.735605 | 528.802 | 527.639 | 5580.737089 | 45.692889 |
| full-on | context-8192 | True | 8170/256 | 5841.570338 | 5195.840 | 5187.998 | 7377.979925 | 34.562306 |
| full-on | context-45056 | False | 45044/256 | 31865.149791 | 30386.538 | 30350.321 | 7098.827252 | 35.921426 |
| full-on | context-204800 | False | 204781/256 | 166934.837668 | 158993.637 | 158831.975 | 7771.951493 | 32.810292 |
| full-on | repeat-code | False | 744/256 | 838.411145 | 697.390 | 696.228 | 5361.672079 | 47.559790 |
| full-on | repeat-text | False | 475/256 | 658.847970 | 527.302 | 526.151 | 5269.820371 | 48.388746 |
| full-off | context-8192 | True | 8170/256 | 5856.595452 | 5201.406 | 5192.959 | 7374.615012 | 34.578076 |
| full-off | context-45056 | False | 45044/256 | 31899.131703 | 30418.802 | 30380.525 | 7089.751233 | 35.967411 |
| full-off | context-204800 | False | 204781/256 | 167070.172638 | 159151.502 | 158989.775 | 7792.785341 | 32.722575 |
| full-off | repeat-code | False | 744/256 | 837.327052 | 694.769 | 693.590 | 6110.004118 | 41.734833 |
| full-off | repeat-text | False | 475/256 | 660.534122 | 528.299 | 527.145 | 6019.117548 | 42.365014 |

## FORENSIC_ONLY_ENV_ABORT：固定对照的原始差值

后者减前者；TTFT 与 handler 事件原点不同。A/A 差值不是噪声容差。

| case | 对照 | TTFT Δms | prefill 完成 Δms | prefill 区间 Δms | post-TTFT Δms | decode Δ% | 相同输出 | 相同目标工作 |
|---|---|---:|---:|---:|---:|---:|---|---|
| context-8192 | subset A2−A1 | -15.855594 | -13.175 | -12.291 | -24.904461 | +0.377923 | True | True |
| context-8192 | subset B−A1 | +2.383170 | -3.827 | -3.802 | -6.018691 | +0.091072 | True | True |
| context-8192 | subset B−A2 | +18.238764 | +9.348 | +8.489 | +18.885770 | -0.285771 | True | True |
| context-8192 | full ON−OFF | -15.025114 | -5.566 | -4.961 | +3.364913 | -0.045608 | True | True |
| context-45056 | subset A2−A1 | +26.382011 | +29.559 | +28.792 | -12.055921 | +0.186614 | True | True |
| context-45056 | subset B−A1 | +49.879102 | +49.349 | +49.405 | -5.848154 | +0.090437 | True | True |
| context-45056 | subset B−A2 | +23.497091 | +19.790 | +20.613 | +6.207767 | -0.095998 | True | True |
| context-45056 | full ON−OFF | -33.981912 | -32.264 | -30.204 | +9.076019 | -0.127852 | True | True |
| context-204800 | subset A2−A1 | -30.779271 | -15.225 | -9.357 | -4.759391 | +0.063825 | True | True |
| context-204800 | subset B−A1 | +289.177601 | +289.079 | +288.728 | -5.926482 | +0.079488 | True | True |
| context-204800 | subset B−A2 | +319.956872 | +304.304 | +298.085 | -1.167091 | +0.015653 | True | True |
| context-204800 | full ON−OFF | -135.334970 | -157.865 | -157.800 | -20.833848 | +0.268065 | True | True |
| repeat-code | subset A2−A1 | +2.821725 | +3.066 | +2.810 | -0.297387 | +0.005497 | True | True |
| repeat-code | subset B−A1 | +1.573722 | +1.303 | +1.811 | -356.763497 | +7.060250 | True | True |
| repeat-code | subset B−A2 | -1.248003 | -1.763 | -0.999 | -356.466110 | +7.054365 | True | True |
| repeat-code | full ON−OFF | +1.084093 | +2.621 | +2.638 | -748.332039 | +13.957065 | True | True |
| repeat-text | subset A2−A1 | +3.854511 | +4.043 | +4.090 | +0.195670 | -0.003506 | True | True |
| repeat-text | subset B−A1 | +0.814375 | -0.400 | -0.415 | -340.803364 | +6.504206 | True | False |
| repeat-text | subset B−A2 | -3.040136 | -4.443 | -4.505 | -340.999034 | +6.507940 | True | False |
| repeat-text | full ON−OFF | -1.686152 | -0.997 | -0.994 | -749.297177 | +14.218647 | True | True |

## FORENSIC_ONLY_ENV_ABORT：全部生成工作计数

不含目标 prefill 或全 prompt draft extend。OFF 无 suffix summary，以 — 表示。

| 服务 | case | a0,a1,a2,a3 | verify | 普通尾 T1 | 目标 calls/rows | restore | 神经 proposal T1 | extend calls/rows | queries/hits |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| first-http | context-8192 | 6,11,5,53 | 75 | 0 | 75/300 | 22 | 150 | 75/255 | 75/0 |
| subset-off-a1 | context-8192 | 7,8,7,52 | 74 | 3 | 77/299 | 22 | 148 | 74/252 | — |
| subset-off-a1 | context-45056 | 7,5,6,55 | 73 | 0 | 73/292 | 18 | 146 | 73/255 | — |
| subset-off-a1 | context-204800 | 8,7,5,54 | 74 | 2 | 76/298 | 20 | 148 | 74/253 | — |
| subset-off-a1 | repeat-code | 0,0,0,63 | 63 | 3 | 66/255 | 0 | 126 | 63/252 | — |
| subset-off-a1 | repeat-text | 2,2,2,60 | 66 | 3 | 69/267 | 6 | 132 | 66/252 | — |
| subset-on-b | context-8192 | 7,8,7,52 | 74 | 3 | 77/299 | 22 | 148 | 74/252 | 74/0 |
| subset-on-b | context-45056 | 7,5,6,55 | 73 | 0 | 73/292 | 18 | 146 | 73/255 | 73/0 |
| subset-on-b | context-204800 | 8,7,5,54 | 74 | 2 | 76/298 | 20 | 148 | 74/253 | 74/0 |
| subset-on-b | repeat-code | 0,0,0,63 | 63 | 3 | 66/255 | 0 | 18 | 63/252 | 63/54 |
| subset-on-b | repeat-text | 4,0,0,62 | 66 | 3 | 69/267 | 4 | 24 | 66/252 | 66/54 |
| subset-off-a2 | context-8192 | 7,8,7,52 | 74 | 3 | 77/299 | 22 | 148 | 74/252 | — |
| subset-off-a2 | context-45056 | 7,5,6,55 | 73 | 0 | 73/292 | 18 | 146 | 73/255 | — |
| subset-off-a2 | context-204800 | 8,7,5,54 | 74 | 2 | 76/298 | 20 | 148 | 74/253 | — |
| subset-off-a2 | repeat-code | 0,0,0,63 | 63 | 3 | 66/255 | 0 | 126 | 63/252 | — |
| subset-off-a2 | repeat-text | 2,2,2,60 | 66 | 3 | 69/267 | 6 | 132 | 66/252 | — |
| full-on | context-8192 | 6,11,5,53 | 75 | 0 | 75/300 | 22 | 150 | 75/255 | 75/0 |
| full-on | context-45056 | 6,3,5,57 | 71 | 0 | 71/284 | 14 | 142 | 71/255 | 71/0 |
| full-on | context-204800 | 4,5,3,58 | 70 | 0 | 70/280 | 12 | 140 | 70/255 | 70/0 |
| full-on | repeat-code | 0,0,0,63 | 63 | 3 | 66/255 | 0 | 18 | 63/252 | 63/54 |
| full-on | repeat-text | 0,0,0,63 | 63 | 3 | 66/255 | 0 | 18 | 63/252 | 63/54 |
| full-off | context-8192 | 6,11,5,53 | 75 | 0 | 75/300 | 22 | 150 | 75/255 | — |
| full-off | context-45056 | 6,3,5,57 | 71 | 0 | 71/284 | 14 | 142 | 71/255 | — |
| full-off | context-204800 | 4,5,3,58 | 70 | 0 | 70/280 | 12 | 140 | 70/255 | — |
| full-off | repeat-code | 0,0,0,63 | 63 | 3 | 66/255 | 0 | 126 | 63/252 | — |
| full-off | repeat-text | 0,0,0,63 | 63 | 3 | 66/255 | 0 | 126 | 63/252 | — |

## FORENSIC_ONLY_ENV_ABORT：ON 直接 suffix 成本

单位 ms；numeric 已包含在 query 中，合计不重复相加。不是纯 CPU 时间。

| 服务 | case | init | query | append | release | numeric（内嵌） | 不重叠合计 | query+append+release |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| first-http | context-8192 | 0.229989 | 0.029245 | 0.051017 | 0.016110 | 0.000389 | 0.326361 | 0.096372 |
| subset-on-b | context-8192 | 0.186712 | 0.027416 | 0.052265 | 0.020601 | 0.000436 | 0.286994 | 0.100282 |
| subset-on-b | context-45056 | 0.474620 | 0.043903 | 0.062722 | 0.006352 | 0.004020 | 0.587597 | 0.112977 |
| subset-on-b | context-204800 | 0.835833 | 0.039990 | 0.057455 | 0.018287 | 0.004482 | 0.951565 | 0.115732 |
| subset-on-b | repeat-code | 0.049695 | 0.103616 | 0.038491 | 0.002371 | 0.036847 | 0.194173 | 0.144478 |
| subset-on-b | repeat-text | 0.045481 | 0.126298 | 0.045722 | 0.003166 | 0.041810 | 0.220667 | 0.175186 |
| full-on | context-8192 | 0.289695 | 0.035170 | 0.080999 | 0.027361 | 0.000444 | 0.433225 | 0.143530 |
| full-on | context-45056 | 0.419732 | 0.029830 | 0.044485 | 0.006648 | 0.003388 | 0.500695 | 0.080963 |
| full-on | context-204800 | 0.881039 | 0.032509 | 0.049137 | 0.016880 | 0.003456 | 0.979565 | 0.098526 |
| full-on | repeat-code | 0.044509 | 0.102454 | 0.043813 | 0.002657 | 0.034023 | 0.193433 | 0.148924 |
| full-on | repeat-text | 0.041352 | 0.134299 | 0.054024 | 0.002371 | 0.047663 | 0.232046 | 0.190694 |

## 解释边界

- Whole stage remains ENV_ABORT; prior-window coverage does not restore VALID.
- All five comparison services have Q4T_MTP_TIMING=1; these are intrusive forensic observations.
- Uninstrumented first-http is listed only, excluded from every comparison.
- One A1/B/A2 and one full ON/OFF pair are not population evidence, a noise band or a loss tolerance.
- TTFT uses the client request origin; handler decode elapsed uses its own origin and ends target prefill before prompt draft initialization.
- Handler elapsed is printed at 0.001ms; an event-pair difference has rounding bound 0.001ms and an interval-pair difference 0.002ms.
- Work counts describe generation only, excluding target prefill and full-prompt draft extend; native phase cost sums are not used here.
- Direct suffix costs are host wall intervals, not pure CPU time. numeric_ns is nested in query_ns and never added twice.
- OFF direct suffix cost is absent, not a measured zero. Shared table startup cost is outside request TTFT and not included here.
- Equal outputs/work or small directly timed index costs do not establish equal throughput or a cause of the observed timing differences.

**根因仍未知。** 这里只记录封存原值及算术差，不输出统计接受结论，不据此启动候选或阶段 B。
