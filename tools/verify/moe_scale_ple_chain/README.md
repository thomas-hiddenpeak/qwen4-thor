# 四分支PLE组合传播

复用moe-scale-hc-write-l0-20260924主干与已冻结原4K HTTP；生产
和观测不改，不新增HTTP。复制至prepared新目录、去掉.in并改路径，
新建e2e/reference。norm.cu/conv.cu分别用nvcc C++20 -O2
-arch=sm_110a -ccbin=g++-14，host -O3,-fopenmp,-Wall,-Wextra,
-ffp-contract=off构建norm/conv。两个build.log为空后run.py，
stdout留prepared，真实终态后seal.py，禁止继续写封存日志。

每支完整4096行：自身trunk→query norm→与未变key norm点积/
gate→广播未变value→conv norm→K4/dilation3零初态因果卷积→
SiLU→BF16→gated相加/BF16→自身trunk相加/BF16。CPU按既有
独立参考顺序执行归约/FMA，设备仅rsqrt、gate非线性、SiLU。
key/value与norm_key依赖未变token查表；其冻结实际值继续绑定。
不是独立高精度或独立设备数学库证明。

baseline逐阶段及末状态恢复实际；输出摘要绑定layer1 HC read
trunk。再逐支传播，变化支持集由实际norm_conv变化位置的
0/3/6/9前向移位及trunk变化位置构成，禁止越界。后9行norm_conv
转为[C,9]保存更新状态，原分支同实际。

完整四支query/gate/gated/convnorm/final及更新状态、所有差异
索引/行分布保留。保存全部norm/gate参数/设备primitive及其输出
差异未舍入值，完整卷积F32累加与设备SiLU。匹配最终未舍入值
不另保存。控制器全20项stage覆盖，终态清单全量核对。

本次moe-scale-ple-chain-20260924，约3.18GB，预算5GB+20GiB。
终点layer1 HC attention read的trunk，尚未做其norm/mix/attention；
不能称整个第二层或最终答案正确性完成。
