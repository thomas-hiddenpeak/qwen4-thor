# 完整PLE归一化与门控边界

模板复制至prepared新目录并去掉.in，更新输出路径，不覆盖冻结
证据。本次ple-full-gate-norm-20260924。
observer.cpp以g++-14 C++23 -O2 -shared -fPIC -ffp-contract=off
-Wall -Wextra、CUDA include及dl构建observer.so，build.log须为空。
第一项测试运行run.py：tools/evalscope原4K关闭/开启/关闭观测，
MTP关闭；请求、完整答案、usage、stop、服务退出及六份元数据
均检查。600440仍错，观测保持不代表质量验收通过。

现场依次检查三组norm、gate、gated及卷积消费者。仅保存新的
norm0 input/output、norm1 output、value、gate、权重与参数。
已有norm1 input、norm2 input/output、gated和最终卷积输出同
冻结ple-full-conv全字节比对；现场指针交接也验证，不重复保存。
采集预算280MB，参考100MB，保留20GiB磁盘余量。

HTTP完成后运行analyze.py，直接匹配checkpoint三组权重，重读
既有证据绑定并检查旧10行尾部。使用实际gate和value做完整CPU
FP32乘法及独立BF16最近偶数舍入，完整输出以实际gated基底+
delta无损恢复。norm/gate算术尚未重算，不能视为整个PLE通过。
