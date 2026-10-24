# 固定 k3 suffix 执行观测结果

PARTIAL_PREFIX_BOUND_TO_INDEPENDENT_AUDIT

已完成前缀：43/43请求、9服务（37正常＋6 CPU观测）。原阶段STOPPED，整体测量不完整；失败profile全部排除，没有GPU活动结论。

| 重复材料 | 完整ON/OFF | 子集B/A1 | 子集B/A2 | 共享B几何均值 | 目标 |
|---|---:|---:|---:|---:|---|
| repeat-code | 1.136774 | 1.067568 | 1.063106 | 1.065335 | True |
| repeat-text | 1.144034 | 1.068658 | 1.065952 | 1.067304 | True |

| 探针 | 组 | 比较 | TTFT差ms | prefill完成差ms | decode变化 | 目标工作相同 |
|---|---|---|---:|---:|---:|---|
| context-8192 | normal | B-A1 | +9.783 | +5.674 | +0.284% | True |
| context-8192 | normal | B-A2 | +5.819 | +1.417 | -0.015% | True |
| context-8192 | normal | A2-A1 | +3.964 | +4.257 | +0.298% | True |
| context-8192 | execution | B-A1 | +1.684 | -0.288 | +0.089% | True |
| context-8192 | execution | B-A2 | -17.228 | -9.765 | +0.138% | True |
| context-8192 | execution | A2-A1 | +18.912 | +9.477 | -0.049% | True |
| context-204800 | normal | B-A1 | -26.822 | -20.193 | -0.057% | True |
| context-204800 | normal | B-A2 | -19.558 | -23.237 | -0.390% | True |
| context-204800 | normal | A2-A1 | -7.264 | +3.044 | +0.334% | True |
| context-204800 | execution | B-A1 | +35.739 | +20.393 | -0.090% | True |
| context-204800 | execution | B-A2 | -36.551 | -45.027 | -0.139% | True |
| context-204800 | execution | A2-A1 | +72.290 | +65.420 | +0.049% | True |

CPU表按独立phase展示inclusive差值；父子行不能相加。原始ns及exclusive分区均在JSON。

| 探针 | 比较 | 窗口 | phase | wall差ms | thread CPU差ms | 非运行wall差ms |
|---|---|---|---|---:|---:|---:|
| context-8192 | B-A1 | prefill_and_init_batches | handler_prefill | -0.329183 | +1.088728 | -1.417911 |
| context-8192 | B-A1 | prefill_and_init_batches | target_prefill_call | -0.843161 | +0.784369 | -1.627530 |
| context-8192 | B-A1 | prefill_and_init_batches | host_readback.stream_sync | +0.293750 | +0.293862 | -0.000112 |
| context-8192 | B-A1 | prefill_and_init_batches | ple.reader | -0.246504 | -0.233025 | -0.013479 |
| context-8192 | B-A1 | prefill_and_init_batches | handler_mtp_init | +1.647802 | +0.548644 | +1.099158 |
| context-8192 | B-A1 | prefill_and_init_batches | mtp.attention | -0.598060 | -0.033501 | -0.564559 |
| context-8192 | B-A1 | prefill_and_init_batches | mtp.moe | +0.708564 | -0.021379 | +0.729943 |
| context-8192 | B-A1 | prefill_and_init_batches | mtp.api.moe_counts_d2h | +0.034425 | -0.003760 | +0.038185 |
| context-8192 | B-A1 | prefill_and_init_batches | mtp.wait.moe_counts_sync | +0.526139 | +0.526241 | -0.000102 |
| context-8192 | B-A1 | prefill_and_init_batches | mtp.moe.experts_submit | -0.588984 | -0.518247 | -0.070737 |
| context-8192 | B-A1 | prefill_and_init_batches | mtp.head | -0.000055 | +0.000240 | -0.000295 |
| context-8192 | B-A1 | after_server_first_content_round | ple.reader | +0.230555 | +0.219035 | +0.011520 |
| context-8192 | B-A1 | after_server_first_content_round | ple.previous_upload_wait | +0.005808 | -0.006123 | +0.011931 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.draft_token | -5.804157 | -0.252993 | -5.551164 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.attention | -0.069616 | -0.041597 | -0.028019 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.moe | -0.682390 | -0.279998 | -0.402392 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.api.moe_counts_d2h | -0.336018 | +0.033852 | -0.369870 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.wait.moe_counts_sync | +0.000640 | +0.001517 | -0.000877 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.moe.experts_submit | -0.478753 | -0.456767 | -0.021986 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.head | +0.027514 | +0.020736 | +0.006778 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.api.draft_matrix_d2h | +0.541560 | -0.003784 | +0.545344 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.verify_forward | -2.126301 | +12.212053 | -14.338354 |
| context-8192 | B-A1 | after_server_first_content_round | mtp.accepted_extend | -0.374701 | +0.488395 | -0.863096 |
| context-8192 | B-A2 | prefill_and_init_batches | handler_prefill | -9.670771 | -25.941976 | +16.271205 |
| context-8192 | B-A2 | prefill_and_init_batches | target_prefill_call | -9.969218 | -25.935473 | +15.966255 |
| context-8192 | B-A2 | prefill_and_init_batches | host_readback.stream_sync | +0.002630 | +0.002714 | -0.000084 |
| context-8192 | B-A2 | prefill_and_init_batches | ple.reader | -0.184691 | -0.167806 | -0.016885 |
| context-8192 | B-A2 | prefill_and_init_batches | handler_mtp_init | -6.051508 | -4.797862 | -1.253646 |
| context-8192 | B-A2 | prefill_and_init_batches | mtp.attention | +0.353039 | -0.130195 | +0.483234 |
| context-8192 | B-A2 | prefill_and_init_batches | mtp.moe | -4.284153 | -4.510935 | +0.226782 |
| context-8192 | B-A2 | prefill_and_init_batches | mtp.api.moe_counts_d2h | +0.063926 | -0.012601 | +0.076527 |
| context-8192 | B-A2 | prefill_and_init_batches | mtp.wait.moe_counts_sync | +0.492185 | +0.491889 | +0.000296 |
| context-8192 | B-A2 | prefill_and_init_batches | mtp.moe.experts_submit | -5.048138 | -4.919296 | -0.128842 |
| context-8192 | B-A2 | prefill_and_init_batches | mtp.head | -0.012462 | -0.012019 | -0.000443 |
| context-8192 | B-A2 | after_server_first_content_round | ple.reader | -0.354326 | -0.373740 | +0.019414 |
| context-8192 | B-A2 | after_server_first_content_round | ple.previous_upload_wait | -0.029441 | -0.039104 | +0.009663 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.draft_token | -1.877954 | -5.075238 | +3.197284 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.attention | -0.611882 | -0.566869 | -0.045013 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.moe | +4.173229 | -2.437444 | +6.610673 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.api.moe_counts_d2h | +6.315291 | -0.335475 | +6.650766 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.wait.moe_counts_sync | -0.014048 | -0.008149 | -0.005899 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.moe.experts_submit | -1.729389 | -1.731475 | +0.002086 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.head | -0.005690 | -0.009048 | +0.003358 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.api.draft_matrix_d2h | +0.161154 | +0.005125 | +0.156029 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.verify_forward | -0.845497 | +7.715471 | -8.560968 |
| context-8192 | B-A2 | after_server_first_content_round | mtp.accepted_extend | -2.576630 | -4.877959 | +2.301329 |
| context-8192 | A2-A1 | prefill_and_init_batches | handler_prefill | +9.341588 | +27.030704 | -17.689116 |
| context-8192 | A2-A1 | prefill_and_init_batches | target_prefill_call | +9.126057 | +26.719842 | -17.593785 |
| context-8192 | A2-A1 | prefill_and_init_batches | host_readback.stream_sync | +0.291120 | +0.291148 | -0.000028 |
| context-8192 | A2-A1 | prefill_and_init_batches | ple.reader | -0.061813 | -0.065219 | +0.003406 |
| context-8192 | A2-A1 | prefill_and_init_batches | handler_mtp_init | +7.699310 | +5.346506 | +2.352804 |
| context-8192 | A2-A1 | prefill_and_init_batches | mtp.attention | -0.951099 | +0.096694 | -1.047793 |
| context-8192 | A2-A1 | prefill_and_init_batches | mtp.moe | +4.992717 | +4.489556 | +0.503161 |
| context-8192 | A2-A1 | prefill_and_init_batches | mtp.api.moe_counts_d2h | -0.029501 | +0.008841 | -0.038342 |
| context-8192 | A2-A1 | prefill_and_init_batches | mtp.wait.moe_counts_sync | +0.033954 | +0.034352 | -0.000398 |
| context-8192 | A2-A1 | prefill_and_init_batches | mtp.moe.experts_submit | +4.459154 | +4.401049 | +0.058105 |
| context-8192 | A2-A1 | prefill_and_init_batches | mtp.head | +0.012407 | +0.012259 | +0.000148 |
| context-8192 | A2-A1 | after_server_first_content_round | ple.reader | +0.584881 | +0.592775 | -0.007894 |
| context-8192 | A2-A1 | after_server_first_content_round | ple.previous_upload_wait | +0.035249 | +0.032981 | +0.002268 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.draft_token | -3.926203 | +4.822245 | -8.748448 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.attention | +0.542266 | +0.525272 | +0.016994 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.moe | -4.855619 | +2.157446 | -7.013065 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.api.moe_counts_d2h | -6.651309 | +0.369327 | -7.020636 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.wait.moe_counts_sync | +0.014688 | +0.009666 | +0.005022 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.moe.experts_submit | +1.250636 | +1.274708 | -0.024072 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.head | +0.033204 | +0.029784 | +0.003420 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.api.draft_matrix_d2h | +0.380406 | -0.008909 | +0.389315 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.verify_forward | -1.280804 | +4.496582 | -5.777386 |
| context-8192 | A2-A1 | after_server_first_content_round | mtp.accepted_extend | +2.201929 | +5.366354 | -3.164425 |
| context-204800 | B-A1 | prefill_and_init_batches | handler_prefill | +19.225186 | +47.623918 | -28.398732 |
| context-204800 | B-A1 | prefill_and_init_batches | target_prefill_call | +19.775563 | +48.350837 | -28.575274 |
| context-204800 | B-A1 | prefill_and_init_batches | host_readback.stream_sync | -0.732037 | -0.799138 | +0.067101 |
| context-204800 | B-A1 | prefill_and_init_batches | ple.reader | -0.583221 | -0.220914 | -0.362307 |
| context-204800 | B-A1 | prefill_and_init_batches | ple.previous_upload_wait | +0.016788 | +0.010723 | +0.006065 |
| context-204800 | B-A1 | prefill_and_init_batches | handler_mtp_init | +16.245423 | +18.496356 | -2.250933 |
| context-204800 | B-A1 | prefill_and_init_batches | mtp.attention | -4.344024 | +1.379751 | -5.723775 |
| context-204800 | B-A1 | prefill_and_init_batches | mtp.moe | +5.179599 | +5.030244 | +0.149355 |
| context-204800 | B-A1 | prefill_and_init_batches | mtp.api.moe_counts_d2h | +2.457885 | +0.122994 | +2.334891 |
| context-204800 | B-A1 | prefill_and_init_batches | mtp.wait.moe_counts_sync | -0.338477 | -0.338164 | -0.000313 |
| context-204800 | B-A1 | prefill_and_init_batches | mtp.moe.experts_submit | +2.804185 | +5.004119 | -2.199934 |
| context-204800 | B-A1 | prefill_and_init_batches | mtp.head | +0.004055 | +0.004611 | -0.000556 |
| context-204800 | B-A1 | after_server_first_content_round | ple.reader | -0.389426 | -0.403112 | +0.013686 |
| context-204800 | B-A1 | after_server_first_content_round | ple.previous_upload_wait | +0.081860 | +0.073599 | +0.008261 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.draft_token | +5.509714 | +7.877321 | -2.367607 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.attention | +1.052534 | +1.060811 | -0.008277 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.moe | -5.175046 | +3.814969 | -8.990015 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.api.moe_counts_d2h | -8.181212 | +0.767205 | -8.948417 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.wait.moe_counts_sync | +0.029968 | +0.032783 | -0.002815 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.moe.experts_submit | +1.901106 | +1.924188 | -0.023082 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.head | +0.050941 | +0.057615 | -0.006674 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.api.draft_matrix_d2h | +0.345456 | +0.064403 | +0.281053 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.verify_forward | -4.785517 | +0.795982 | -5.581499 |
| context-204800 | B-A1 | after_server_first_content_round | mtp.accepted_extend | +3.765894 | +8.560440 | -4.794546 |
| context-204800 | B-A2 | prefill_and_init_batches | handler_prefill | -45.946027 | -43.895148 | -2.050879 |
| context-204800 | B-A2 | prefill_and_init_batches | target_prefill_call | -45.573674 | -43.169660 | -2.404014 |
| context-204800 | B-A2 | prefill_and_init_batches | host_readback.stream_sync | -0.809167 | -0.738377 | -0.070790 |
| context-204800 | B-A2 | prefill_and_init_batches | ple.reader | -2.548517 | -2.798339 | +0.249822 |
| context-204800 | B-A2 | prefill_and_init_batches | ple.previous_upload_wait | +0.000818 | -0.000963 | +0.001781 |
| context-204800 | B-A2 | prefill_and_init_batches | handler_mtp_init | +8.710408 | +17.775518 | -9.065110 |
| context-204800 | B-A2 | prefill_and_init_batches | mtp.attention | +3.227507 | +0.989759 | +2.237748 |
| context-204800 | B-A2 | prefill_and_init_batches | mtp.moe | +0.907734 | +6.281785 | -5.374051 |
| context-204800 | B-A2 | prefill_and_init_batches | mtp.api.moe_counts_d2h | -2.671723 | -0.043003 | -2.628720 |
| context-204800 | B-A2 | prefill_and_init_batches | mtp.wait.moe_counts_sync | -0.229270 | -0.231774 | +0.002504 |
| context-204800 | B-A2 | prefill_and_init_batches | mtp.moe.experts_submit | +3.677588 | +6.405421 | -2.727833 |
| context-204800 | B-A2 | prefill_and_init_batches | mtp.head | +0.005267 | +0.005425 | -0.000158 |
| context-204800 | B-A2 | after_server_first_content_round | ple.reader | -0.543384 | -0.571383 | +0.027999 |
| context-204800 | B-A2 | after_server_first_content_round | ple.previous_upload_wait | +0.080800 | +0.072382 | +0.008418 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.draft_token | +10.041089 | +8.562388 | +1.478701 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.attention | +1.432942 | +1.433238 | -0.000296 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.moe | -1.202816 | +4.260765 | -5.463581 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.api.moe_counts_d2h | -4.585489 | +0.839626 | -5.425115 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.wait.moe_counts_sync | +0.046703 | +0.050155 | -0.003452 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.moe.experts_submit | +2.103106 | +2.160330 | -0.057224 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.head | +0.054534 | +0.049087 | +0.005447 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.api.draft_matrix_d2h | -0.795703 | +0.074110 | -0.869813 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.verify_forward | -3.271267 | +4.464527 | -7.735794 |
| context-204800 | B-A2 | after_server_first_content_round | mtp.accepted_extend | +4.425782 | +9.598720 | -5.172938 |
| context-204800 | A2-A1 | prefill_and_init_batches | handler_prefill | +65.171213 | +91.519066 | -26.347853 |
| context-204800 | A2-A1 | prefill_and_init_batches | target_prefill_call | +65.349237 | +91.520497 | -26.171260 |
| context-204800 | A2-A1 | prefill_and_init_batches | host_readback.stream_sync | +0.077130 | -0.060761 | +0.137891 |
| context-204800 | A2-A1 | prefill_and_init_batches | ple.reader | +1.965296 | +2.577425 | -0.612129 |
| context-204800 | A2-A1 | prefill_and_init_batches | ple.previous_upload_wait | +0.015970 | +0.011686 | +0.004284 |
| context-204800 | A2-A1 | prefill_and_init_batches | handler_mtp_init | +7.535015 | +0.720838 | +6.814177 |
| context-204800 | A2-A1 | prefill_and_init_batches | mtp.attention | -7.571531 | +0.389992 | -7.961523 |
| context-204800 | A2-A1 | prefill_and_init_batches | mtp.moe | +4.271865 | -1.251541 | +5.523406 |
| context-204800 | A2-A1 | prefill_and_init_batches | mtp.api.moe_counts_d2h | +5.129608 | +0.165997 | +4.963611 |
| context-204800 | A2-A1 | prefill_and_init_batches | mtp.wait.moe_counts_sync | -0.109207 | -0.106390 | -0.002817 |
| context-204800 | A2-A1 | prefill_and_init_batches | mtp.moe.experts_submit | -0.873403 | -1.401302 | +0.527899 |
| context-204800 | A2-A1 | prefill_and_init_batches | mtp.head | -0.001212 | -0.000814 | -0.000398 |
| context-204800 | A2-A1 | after_server_first_content_round | ple.reader | +0.153958 | +0.168271 | -0.014313 |
| context-204800 | A2-A1 | after_server_first_content_round | ple.previous_upload_wait | +0.001060 | +0.001217 | -0.000157 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.draft_token | -4.531375 | -0.685067 | -3.846308 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.attention | -0.380408 | -0.372427 | -0.007981 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.moe | -3.972230 | -0.445796 | -3.526434 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.api.moe_counts_d2h | -3.595723 | -0.072421 | -3.523302 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.wait.moe_counts_sync | -0.016735 | -0.017372 | +0.000637 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.moe.experts_submit | -0.202000 | -0.236142 | +0.034142 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.head | -0.003593 | +0.008528 | -0.012121 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.api.draft_matrix_d2h | +1.141159 | -0.009707 | +1.150866 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.verify_forward | -1.514250 | -3.668545 | +2.154295 |
| context-204800 | A2-A1 | after_server_first_content_round | mtp.accepted_extend | -0.659888 | -1.038280 | +0.378392 |

所有响应hash、接受分布、目标工作、suffix计数、逐batch CPU原始记录及首轮拆分见JSON。

- The original frozen plan remains 49 requests/13 starts. Only nine completed normal/CPU groups (43 requests) enter this report.
- The failing profile group and all unstarted groups are excluded. There is no valid GPU busy/queue/kernel attribution here.
- The success-only post-shutdown monitor barrier was not reached. This report preserves STOPPED/COMMAND_FAILED and does not mark the whole stage VALID.
- Normal, CPU-observed and Nsight services remain separate; observation changes execution timing.
- Repeat material is already seen development material. Three requests share a startup; subset B/A1 and B/A2 share B and are not independent blocks or a population noninferiority result.
- CPU clocks retain integer nanoseconds. Wall and thread CPU clocks are sampled sequentially; small signed wall-minus-CPU values are preserved rather than clamped or called GPU wait.
- Inclusive phase totals are nested. Exclusive phases partition observed roots only. Thread CPU sums cover observed threads and windows, not all process CPU.
- CUDA API wall can include driver CPU, blocking and descheduling; exact GPU activity requires the separately audited Nsight timeline. Host wall minus CPU is not GPU busy time.
- First-content SSE ordinal maps to a server emitting round. Removing it is a proxy and does not slice at the actual client TTFT timestamp.
- Batch flush costs are separate from CPU scopes. Observer clocks/NVTX bookkeeping and unobserved scheduling gaps are not subtracted to fabricate uninstrumented costs.
- Suffix numeric time is nested in query time. Init/query/append/release are host wall intervals with different windows from post-TTFT; no memory/RAM inference is made here.
- CPU observed services and normal services are separate startups. CPU phase differences cannot establish the cause of a normal HTTP difference unless supported by further independent evidence.
- Thread CPU includes host computation, CUDA driver work, submission and possible busy waiting. A pageable D2H API may itself block; a later explicit synchronization with wall close to thread CPU does not prove there was no wait. Neither is GPU busy time.
- mtp.forward exclusive is the measured remainder after its nested scopes: it includes workspace carving, position copying, embedding, RMSNorm/FC and HC work; it is not one operation. Its generation totals combine neural proposal and accepted-prefix extend.
- This adapter was written after collection for the exact completed prefix; source and imported parser hashes are retained. It changes no frozen input, metric definition or original stage status.

CPU核对摘要（所有数值从原始整数ns换算为ms）：

| 探针 | CPU组 | target prefill wall | target prefill thread CPU | 首个内容轮后 forward exclusive wall | 对应thread CPU | 对应非运行wall |
|---|---|---:|---:|---:|---:|---:|
| context-8192 | exec-off-a1 | 5143.590956 | 811.776630 | 94.065734 | 82.290346 | 11.775388 |
| context-204800 | exec-off-a1 | 158789.211283 | 17212.409251 | 94.405629 | 83.879240 | 10.526389 |
| context-8192 | exec-on-b | 5142.747795 | 812.560999 | 88.962604 | 82.755259 | 6.207345 |
| context-204800 | exec-on-b | 158808.986846 | 17260.760088 | 109.243014 | 94.410316 | 14.832698 |
| context-8192 | exec-off-a2 | 5152.717013 | 838.496472 | 96.486949 | 89.214771 | 7.272178 |
| context-204800 | exec-off-a2 | 158854.560520 | 17303.929748 | 94.772695 | 83.096504 | 11.676191 |

target prefill为各真实chunk调用之和；forward exclusive剔除了已观测子区间，且包含神经proposal和接受前缀extend。首个内容轮后是server轮次代理，不等同于客户端TTFT精确切片。上述CPU与非运行wall均不能解释为GPU耗时。
