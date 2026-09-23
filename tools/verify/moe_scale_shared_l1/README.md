# 四分支 layer1 共享分支与完整 MoE 输出

各支自身mixed计算shared GU、SwiGLU/BF16、shared down；自身mixed
计算256路点积归约和设备sigmoid门值。与自身routed F32用CPU
显式FMA+BF16合并，原分支GU/inter/down/最终MoE均逐位恢复。
门值dot/sigmoid对照是之前的条件参考，未直接观测内部值；最终
输出是真实捕获。不能把门值参考复现描述成新增内部观测。

模板去.in复制到 `.q4t-work/prepared/moe-scale-shared-l1-20260924/`，
新建对应e2e/reference。cpu/final用g++-14 C++20 -O3 -fopenmp
-Wall -Wextra -ffp-contract=off；gemm用g++-14 C++20 -O2 -Wall
-Wextra，链接cublasLt/cudart；device用nvcc C++20 -O2 -arch=sm_110a
-ccbin=g++-14，host -Wall,-Wextra,-ffp-contract=off。空build.log
后run.py，stdout留prepared/run.log；真实终态后seal.py，stdout
也留prepared。新实验新目录，不覆盖任何冻结文件。

四组checkpoint权重重读并验证GU拼接；记录cuBLAS算法/设备sigmoid
仍是参考依赖。保存完整24阶段结果、差异/token分布、inter/final
rawFP32和dot，核对BF16舍入。预算1GB+20GiB余量，封存139项
728314338字节。无生产/观测改动、新HTTP或最终答案正确性证明。
终点完整MoE块输出，尚未HC残差写回。
