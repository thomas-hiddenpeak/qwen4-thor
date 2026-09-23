# 完整 down 投影边界与路由专家合并参考

模板放入 `.q4t-work/prepared/moe-full-down-20260924/` 后去掉 `.in`。
同名证据不覆盖；重跑整体使用新目录。模型和旧证据只读。

1. prepare.py 从 checkpoint 生成每个专家的 down weight/SF/input_scale/
   weight_scale_2 来源计划，读取并保存摘要，不复制大权重。
2. observer.cpp 按 C++23、`-O2 -shared -fPIC -ffp-contract=off
   -Wall -Wextra` 构建，CUDA include、链接 dl，生成 observer.so，
   build.log 必须为空。
3. run.py 的第一项测试为 tools/evalscope 原4K HTTP 三侧观测对照，
   MTP关闭。检查请求/输出/停止/退出及48层所有元数据。原错答仍为
   600440，观测不干扰不等于质量通过。
4. 三侧对照完成后，analyze.py 重读绑定来源、checkpoint段、行映射、
   路由权重及完整输出；合并前缓冲区必须全部为正零。
5. combine.cpp 用 C++23、`-O2 -ffp-contract=off -fopenmp
   -Wall -Wextra` 构建，生成 combine 与空 combine-build.log。
   run_combine.py 用8线程按十槽FP32 fma顺序重建全部路由合并值。
6. 参考保存为实际输出位值加差异索引/替换位值；临时完整参考必须
   重建逐字节相同且SHA相同后才删除。audit.py 再从落盘delta恢复
   全部参考并核对来源和统计。

C++模板按仓库格式整理；实验实际编译源码、二进制和构建日志另在
证据目录保存，原始记录不回写。

这里的 CombineGrouped 只合并路由专家。shared expert 和后续最终
MoECombine 是另一阶段。实际 down 的输入/权重/输出及记录算法均
绑定，但本阶段不宣称已核对完整 down GEMM 算术。所有输入继续
保留实际量化尺度，不能据此消除已确认的 E4M3 编码错误。
