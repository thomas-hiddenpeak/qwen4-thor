# 完整QSA选择顺序参考

复用qsa-full-selection-20260924已完成三侧HTTP的冻结证据，
生产和观测器未改，不重跑HTTP。模板复制至prepared新目录并
去掉.in，更新输出路径，不覆盖冻结证据。本次qsa-full-selection-reference-20260924。

reference.cpp以g++-14 C++23 -O2 -fopenmp -Wall -Wextra构建
reference，build.log须为空，再运行run.py。CPU独立标量比较
网络重建精确排序，同分时严格比较不交换；不调用生产排序代码。
另用std::sort排序可见评分，验证选中512组无遗漏更高分，且
候选唯一、顺序非降。保存所有query的阈值、同分总数及选中数。

比较所有有效索引与-1尾部及长度。每层完整临时参考经基底+
delta逐字节/SHA恢复后删除，末尾再次恢复全部12层。保存完整
长度、稀疏标记、阈值/同分计数和语义检查结果。总参考预算1GB，
另留20GiB。输入评分未重算，不证明评分或attention计算正确，
原错答仍在，不能把参考通过当作整模型验收。
