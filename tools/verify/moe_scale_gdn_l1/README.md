# 四分支 layer1 GDN 传播

复用已门控冻结HTTP和四分支短卷积/A/B投影；无运行时或观测修改。
将模板复制到 `.q4t-work/prepared/moe-scale-gdn-l1-20260924/`，去掉
`.in`，创建对应e2e/reference目录。C++使用g++-14 C++20 -O3
-fopenmp -Wall -Wextra -ffp-contract=off；CUDA使用nvcc C++20 -O2
-arch=sm_110a -ccbin=g++-14，host -Wall,-Wextra,-ffp-contract=off。
构建零警告后run.py，stdout保留prepared/run.log；进程终态后seal.py，
stdout留prepared/seal.log。不同实验使用新目录，不覆盖冻结文件。

norm按每lane四次平方FMA及32路XOR，设备rsqrt，q额外1/sqrt(128)，
BF16写回；v不改。A/B使用自己的新投影，checkpoint dt_bias/A_log
重读绑定，设备计算softplus/decay/beta。recurrence从原始全零状态
开始，CPU按实际FMA顺序`fma(alpha,S,k*delta)`推进4096步。
原分支必须完整恢复归一化QKV、门值、Y和最终状态，才能比较修正支。

保存完整norm CPU/device/FP64输出、数学参数、归一化QKV、门值、Y、
最终状态、全部差异位置及token计数；比较范围以reference.json为准。
CPU/FP64 norm是额外对照，不是高精度整链递推。输入只读硬链接，
生成文件独立；不得修改共享文件权限或内容。预算3GB+20GiB余量，
封存162项、约2.77GB逻辑字节，哈希逐项验证。

终点仅layer1 GDN输出及状态，尚未NormGate/out/HC/MoE，也未证明
最终错答因果。无新HTTP质量通过或性能结论。
