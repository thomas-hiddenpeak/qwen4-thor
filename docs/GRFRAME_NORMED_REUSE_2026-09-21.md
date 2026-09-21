# GRFrame normed 工作区复用（2026-09-21）

状态：接受，基线 7a34dd3。必要构建零警告；首项质量 HTTP 11/11
通过、服务退出 0，完整五档性能 15/15 通过。门禁后时间线与编译容量核对通过，无前置专项或 profile。

## 实际改动

只修改 src/model/decoder_layer.cu。两个 GRRead 从同一 d_normed
取得临时区，移除独立的 d_res_m；d_res_a 改名消除 residual/normed
混淆。DecoderLayerWorkspaceBytes 与 forward 的 scratch_bytes 同时
从三份 hc_dim 区域改成两份：normed 与 combined，PLE trunk 另计。
真实 carve 同步删除一份区域，其后 combined/PLE trunk 地址前移。
attention/MoE/PLE 的子模块区、两个 GEMM scratch 仍各自独立。

第一阶段已确认 GRWrite 不保留 normed；两次 GRRead 在同一 caller
stream 上顺序提交，当前复用无需新增事件或同步。kernel 数学、模型
状态和权重类型不变。布局变化仍可能改变 pool/cache 行为，不能以
数学不变或少用容量代替性能门禁。

## 容量推导与证据范围

模型固定 hs=2560、hc=4，区域每 token 为 20480 字节，是 256 的
整数倍；删除区域保持后续指针对齐。T=8192 减少 167772160 字节，
即 160 MiB 的 decoder workspace 预算；T=1 为 20 KiB。

ModelLoad 在 src/model/model.cu 对所有层与 head 的 workspace 取
最大值后申请 m->d_ws，实际容量不是每层预算乘以 48。其他 tensor、
持久状态、异步内存池、CPU/page cache 均不在本轮差额内。当前没有
系统峰值测量，不声称整机峰值已减少 160 MiB。

## 验收

证据目录 `.q4t-work/e2e/grframe-normed-reuse-20260921/` 保存
精确旧/新二进制、修改源文件、patch、构建日志和原始 HTTP 数据。
性能必须同时与直接父版本 GRFrame read/write 和正式参考 fcb5925
比较，不能因多次小幅漂移而逐步降低门槛。完整五档与质量通过后才
采集 4K HTTP 时间线，核对输出、kernel 和分配次数，随后运行容量核对。

## 后续边界

本轮只复用两个 GRRead 的临时区，没有把任意子模块 arena 相互别名。
下一阶段首先统一容量预算和真实 offsets 的布局来源，再单独验证
normed 与子模块 scratch 的时间别名。MoE 辅助 stream 的 event 汇合
必须纳入最后消费者判断，不能凭 host 返回证明设备操作已经完成。

## 完整五档结果

| 输入 token | TTFT 均值秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.826758 | 18.633832 |
| 4096 | 2.788454 | 17.874981 |
| 8192 | 5.500007 | 18.168000 |
| 45056 | 31.851743 | 17.885938 |
| 204800 | 165.040948 | 17.119387 |

相对父版本和 fcb5925，全部 TTFT 范围重叠；1K/4K/8K/44K decode
范围重叠，200K 三次 decode 均高于两组旧范围。本轮均值相对正式参考
约 +0.28%，样本仅三次，不据此宣称稳定加速或重置正式参考。
所有输入输出摘要一致，服务退出 0。TTFT 含 HTTP/分词/prefill，
decode 采用首 token 后总生成量除以总时间。

## 门禁后容量与时间线

check_capacity.py 从 7a34dd3 提取旧容量函数，与本次 HTTP 对应的静态库
中真实 DecoderLayerWorkspaceBytes 比较。编译零警告、退出 0；
T=1/3/4/5/33/257/8192，linear、linear+PLE、full 共 21 组，差额均为
20480*T 字节。另对模型实际层类型及 head 取最大预算，7 组均一致。

T=8192 的最大值由 linear+PLE 层决定：2683502592→2515730432 字节，
减少 167772160 字节（160 MiB）。ModelLoad 将这个最大值用于 d_ws
申请；该核对针对编译后预算函数，不是 CUDA allocator 物理驻留或系统
峰值测量。源文件、静态库与 HTTP 二进制指纹在工具执行前核对一致。

4K HTTP profile 输入输出一致，采集、服务退出、导出正常。全部 kernel
调用次数与父版本相同：prefill 87703 次、decode 442935 次（255 次前向）。
异步分配/释放 prefill 各 554 次、decode 各 141270 次；MoE counts 回读
仍为 prefill 48 次、decode 0 次。没有减少 kernel 或 allocator 调用。
profile 窗口 prefill 3.011→3.026 秒、decode 14.462→14.488 秒，带采集
开销，不能拿它替代正式五档性能判断。

## 结论与后续

保留一份具名 normed 临时区，消除第二份区域及其重复容量预算。精度与
性能门禁通过，容量确实减少；正式性能参考保持 fcb5925，未声称稳定
吞吐增益。质量矩阵不替代全面业务、并发、MTP 或多模态验证。
下一阶段将容量查询与 forward carve 统一到一个 offset/bytes 布局来源，
先保持区域物理布局，再另行试验跨子层 arena 别名。

二进制 SHA-256：`8b495fa0dafa7b8042a02546ca3ef02094a63209c4adbd619886f7d2202d1507`。
