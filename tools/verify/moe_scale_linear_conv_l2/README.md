# 四分支 layer2 短卷积与历史

各支自身layer2 QKV投影，重绑本层K4/dilation1权重和原零历史。
CPU按tap0..3显式FMA，设备仅SiLU，CPU BF16舍入。helper新增layer
参数，算术未变，避免以后再为层号复制计算逻辑。

模板去.in复制到 `.q4t-work/prepared/moe-scale-linear-conv-l2-20260924/`，
新建对应e2e/reference。conv用nvcc C++20 -O2 -arch=sm_110a
-ccbin=g++-14，host -O3,-fopenmp,-Wall,-Wextra,-ffp-contract=off。
空build.log后run.py，stdout留prepared/run.log；真实终态后seal.py，
stdout也留prepared。新实验换名，不覆盖冻结文件。

原支必须恢复完整卷积输出/GDN输入及更新历史。变化支输出不得
超出自身QKV变化位置前向0/1/2/3步支持集；更新历史是最后3行
原始QKV转[C,3]，不能据此推断GDN递推状态相同。
保存完整acc/SiLU FP32、BF16、历史及全部差异/token分布。
预算3GB+20GiB余量。无生产/观测修改、无新HTTP或最终答案证明。
