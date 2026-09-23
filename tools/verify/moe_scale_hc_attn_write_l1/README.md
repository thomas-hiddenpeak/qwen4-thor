# 四分支 layer1 注意力 HC 残差写回

各支自身attention block、自身PLE主干及自身attention inject gate
执行CPU `fma(block, gate, residual)`然后BF16舍入；原分支恢复
冻结fused.output及MLP残差消费者。无生产/观测改动，无新HTTP。
不得将旧门值或主干代入修正分支。

模板去.in复制到 `.q4t-work/prepared/moe-scale-hc-attn-write-l1-20260924/`，
新建对应e2e/reference；write用g++-14 C++20 -O3 -fopenmp
-Wall -Wextra -ffp-contract=off。空build.log后run.py，stdout留prepared；
真实终态后seal.py，封存stdout也在prepared，不覆盖任何冻结文件。
预算2GB+20GiB余量；保留完整BF16输出、全部差异、差异处rawFP32
及token计数，检查BF16舍入、有限性与自身三输入的因果支持集。
终点仅注意力后的主干，尚未MLP HC归一化/读取、路由或专家计算。
没有最终答案正确性或性能结论。
