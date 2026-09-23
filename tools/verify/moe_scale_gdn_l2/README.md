# 四分支 layer2 GDN 传播

运行prepare.py，复核layer1封存helper并只编译可选层号parameters。
新构建必须零警告；reused-build.log仅为旧构建证据。随后运行新
prepared目录中的run.py，stdout留prepared/run.log；终态后运行
seal.py，stdout留prepared/seal.log。冻结目录不得覆盖。

复用已门控HTTP，生产及观测无修改；不新跑HTTP。各支自身QKV、
A/B和本层零初态/权重进入指定算术递推，原支必须全量恢复。
保留完整norm各变体、数学参数、归一化QKV、Y/状态及全部差异。
预算4GB并保留20GiB空闲。封存164项，3137917625逻辑字节。

终点仅layer2 GDN，不代表最终任务正确。详细范围与数字见
../../../docs/MOE_SCALE_PROPAGATION_2026-09-24.md。
