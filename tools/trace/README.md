# MoE 路由轨迹基础工具

当前只实现 host 轨迹类型、读写器与离线完整性检查。**尚未接入 serve/GPU，
没有真实路由样本、热点或命中率结果。** 旧 Q4T_MOE_DUMP 不是此格式。

```bash
cmake -S tests/host -B build/public-host -DCMAKE_CXX_COMPILER=g++-14
cmake --build build/public-host --parallel
ctest --test-dir build/public-host --no-tests=error --output-on-failure
build/public-host/q4t_router_trace_check .q4t-work/moe-trace/RUN/routes.bin
```

检查器只读输入，不加载模型。退出 0 表示结构完整且所有请求终态成功；
1 表示文件/合同错误，2 表示命令行错误，3 表示结构可读但含取消/失败，
不作为成功采集通过。空文件、空运行、缺少结束记录一律非 0。

C++ API 见 `include/q4t/trace/router_trace.h`。版本 1 只接受顺序、完整请求，
不支持采样窗口、混合批次或并发。request_id 和 forward_id 分别在运行内
严格递增；阶段显式给出，decode 仅一行，不通过行数猜测 prefill/decode。
分块 prefill 使用实际输入位置；输出 token 数独立记录，不等同 decode 行数。
GPU 未完成不能 commit，也不能在失败后开始下一请求；这些是证据检查，
不是 GPU 同步或缓冲回收实现。请求取消/失败允许保存部分层作为诊断。

格式实现为小端定长整数，逐帧长度与 CRC32，无 C++ struct padding。
头部包含版本、层数、专家数、top-k、每 forward 最大行数，以及二进制、
模型清单、工作负载清单的 SHA256。请求记录包含实际输入 token 序列的
SHA256（逐 token 小端 uint32，无分隔符）；采集器须保存可核查输入清单。
CRC 仅检测损坏，不能认证来源；检查器不会证明摘要对应了实际执行，
也不能仅凭 recorded gpu_complete 证明 CUDA 已完成。真实来源绑定与
采集器接入回归是下一批工作。

每帧最多 16 MiB，读入前检查长度；按帧处理，不加载整个长请求。
层记录保留原始 top-k 顺序，检查每行范围与去重。writer 属于离线/后台
IO 接口，会分配和阻塞写入，禁止直接用于推理热路径；Finish 检查结束
记录与 flush，不能代替 fsync 或最终文件的原子发布。

本批测试使用合成轨迹：10 项 C++ 合同（含逐字节截断/损坏）、独立 Python
编码的 49 项 wire/CLI 用例。公共 CTest 自动运行两组，Thor release host
清单包含同一组 C++ 合同。尚未实现：有界 GPU/pinned 池、后台队列、
采集限额/故障隔离、原子封存、真实 HTTP 采集、热点统计与缓存回放。
