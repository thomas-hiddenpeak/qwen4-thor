# 完整PLE key/value投影边界

模板复制至prepared新目录并去掉.in，修改输出路径，不覆盖冻结
证据。本次ple-full-projection-v2-20260924。首版控制脚本因来源
manifest覆盖fixture变量，在启动服务/发送HTTP之前KeyError，
保留于ple-full-projection-20260924；v2仅修正控制变量，观测器
二进制保持相同，不能把首版说成一次HTTP失败。

observer.cpp以g++-14 C++23 -O2 -shared -fPIC -ffp-contract=off
-Wall -Wextra、CUDA include及dl构建observer.so，build.log须为空。
首项测试为run.py的tools/evalscope原4K关闭/开启/关闭观测，
MTP关闭；完整请求/答案/usage/stop/服务退出及六份元数据检查。
原600440错答保持不代表质量验收通过。

实际全FP8、转换BF16、缩放前后和每次投影输入均现场全字节
比较冻结ple-lookup step0；key/value输出以及下游真实消费者
分别比较冻结norm0 input和gated value。只新增完整实际权重、
算法和缩放参数，不复制输入/输出。全部cublas布局、FP32计算、
alpha1/beta0、默认epilogue、32MiB workspace直接检查。

对照后运行analyze.py，直接核对checkpoint两组权重和weight_scale，
重新校验来源摘要并冻结manifest。本阶段不执行投影算术参考。
采集75MB、参考100MB、另留20GiB余量。生产不改，原错答仍在。
