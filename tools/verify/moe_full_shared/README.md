# 完整 shared expert / 最终 MoECombine 边界

模板放入 `.q4t-work/prepared/moe-full-shared-20260924/` 后去掉
`.in`。重跑使用新目录，不覆盖冻结证据。

observer.cpp 用C++23、`-O2 -shared -fPIC -ffp-contract=off
-Wall -Wextra`、CUDA include和dl构建为observer.so，build.log须为空。
第一项测试运行run.py：tools/evalscope原4K，关闭/开启/关闭观测
各一次，MTP关闭；请求、答案、停止与退出必须保持，所有240份
边界元数据完整。原答案600440仍错，三侧一致不等于质量通过。
之后才运行analyze.py，重读所有来源，核对checkpoint权重、形状
和文件尺寸，生成冻结manifest。此模板不执行算术参考。

观测从每层CombineGrouped结束开始，依次绑定shared GU、SwiGLU、
shared down、最终MoECombine。保存完整共享输出、实际权重和
算法；实际输入x、routed和最终输出分别全字节比较冻结的HC mixed
输入、routed combine-after和HC write.block，不重复保存。

SwiGLU输出指针/完整值接通down实际输入，down输出指针/完整值
接通最终MoECombine；最终合并的256线程归约形状也记录。输入源
一致不能代替算术正确，后续需要完整投影、非线性、gate归约及最终
混合参考。共享之外的E4M3缺陷和原错答不因边界核对而消失。
