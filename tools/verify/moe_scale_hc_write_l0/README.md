# 首层尺度修正四分支HC残差写回

复用冻结HTTP与前阶段四分支MLP block，不改生产/观测，不新增
HTTP。复制至prepared新目录、去掉.in并改路径，新建e2e/reference。
write.cpp用g++-14 C++23 -O2 -fopenmp -ffp-contract=off -Wall
-Wextra构建，build.log为空后run.py。stdout留prepared，真实终态
后seal.py，封存后不能追加日志。

CPU逐项显式FP32 FMA(block*gate+residual)再BF16。门值/残差
来自冻结HC：GatedResidualFrame::ReadImpl在MoE前准备inject gate，
本轮干预只发生于MoE内部，因此不依赖新block。代码路径摘要保存。
原分支41943040项写回全同，且完整摘要同已捕获PLE trunk输入；
再核对三个修正分支，变化不能越出block变化token。

完整四组BF16 trunk、全部差异索引、差异位置舍入前FP32值和
每token计数保留；匹配位置未舍入值不保存。当前只到layer1 PLE
输入，不能直接复用旧PLE query norm/gate/gated/conv结果。
本次moe-scale-hc-write-l0-20260924，约461MB，预算1GB+20GiB。
