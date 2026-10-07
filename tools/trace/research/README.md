# 离线研究算法副本

`moe_partition.h` 从 `15feb5772c017fe8aaff26f014d53a52362d2a24`
的 `include/q4t/model/moe_partition.h` 逐字复制。
它仅由 `tools/trace/gpu_cache_schedule.cpp` 使用，未接入 runner。
保留原 namespace、注释和算法，以便对历史 GPU 缓存研究做相同规则回放。
这里的 min-new 分块是历史研究策略，不表示 main 的当前运行时策略。

公共 host 构建编译该 helper 及 `offload_sort.cpp`，使用合成路由验证：
整组容量、原 C++ 排序与同键次序、legacy/min-new 分块、输入拒绝合同。
无模型、CUDA、真实轨迹或存储时序。
