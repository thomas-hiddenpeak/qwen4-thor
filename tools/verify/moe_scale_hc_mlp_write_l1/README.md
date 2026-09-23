# 四分支 layer1 最终 HC 写回 / layer2 入口

各支自身MoE块输出、自身attention后主干、自身MLP注入门值，
CPU `fma(block,gate,residual)`后BF16。原分支完整41943040项
恢复，且同冻结layer2 HC read trunk。三修正支均使用自身三个输入。

模板去.in复制到 `.q4t-work/prepared/moe-scale-hc-mlp-write-l1-20260924/`，
新建对应e2e/reference。write用g++-14 C++20 -O3 -fopenmp
-Wall -Wextra -ffp-contract=off。空build.log后run.py，stdout留
prepared/run.log；真实终态后seal.py，stdout也留prepared。
新实验新目录，不覆盖冻结输入。

保存完整BF16主干、全部差异及差异处舍入前F32、token分布，
检查有限性、BF16舍入与自身输入支持集。匹配位置rawF32未保存。
预算2GB+20GiB余量，封存36项907243849字节。至此layer0干预
经过完整layer1到达layer2入口，仍不是全模型前向或最终答案因果。
layer2尚未计算；无生产/观测修改，无新HTTP或性能结论。
