# 首层高区间尺度修正四分支对照

全部复用冻结原4K HTTP输入，不改运行时或新增HTTP。按顺序inter、
两个exit、audit；每阶段用新prepared/e2e目录，去掉模板.in、调整
路径，不覆盖封存证据。stdout留prepared，真实终态后seal.py。

inter目录默认moe-scale-factorial-inter-l0-20260924，新建reference。
inter.cpp用g++-14 C++23 -O2 -fopenmp -ffp-contract=off -Wall
-Wextra，build.log为空后运行。baseline/input_fixed先完整逐字节
复现moe-scale-inter-l0-20260924输出，再计算inter_fixed/both_fixed。
中间修正仅[248,432)尺度改为最近偶数，次正规规则保留；各新分支
与其父分支激活/ratio相同，FP4变化必须限于尺度变化组。保留四组
完整激活、ratio、量化及所有差异。预算1.5GB+20GiB余量。

两个exit目录默认moe-scale-exit-{inter_fixed,both_fixed}-l0-20260924，
各新建inputs/down/combined。使用moe_scale_exit_l0已有down、
combine、final和common.h；本次按已封存manifest核对并复制原
零警告构建及二进制，reused-build.json记来源和摘要，未重新编译。
注意原down可执行文件实际归档于down/down，因为down亦为结果目录。
新构建时沿用原工具README命令，不从未绑定旧库重链接模型服务。

每支先恢复原routed及final再计算新down/合并，保持actual shared
路径，逐行/逐token界定变化。传播终点仍是HC write前MLP block。
inter_fixed执行版plan沿用“legacy encoding”旧描述不准确，原值保留，
scope-clarification.json明确实际读入高区间修正结果；此模板已修正
描述，数值文件未改。两个exit各预算1.5GB+20GiB余量。

audit目录默认moe-scale-factorial-audit-l0-20260924，无reference子目录。
绑定三组最终出口和各自baseline，核对baseline相同、影响token交集/
并集及两两差异，保存所有索引。不能把变化数量解读成误差或质量。
模型与reference只读，正确性/任务因果仍未完成。
