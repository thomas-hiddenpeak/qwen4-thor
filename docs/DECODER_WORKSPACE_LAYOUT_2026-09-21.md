# Decoder workspace 布局单源化（2026-09-21）

状态：接受，基线 657b2e8。构建零警告，首项质量 HTTP 11/11、
服务退出 0；完整五档性能 15/15 通过，门禁后布局与时间线核对通过。
没有前置专项或 profile，正式性能参考保持 fcb5925。

## 结构变化

之前公开容量函数、forward 容量检查和指针 carve 分别维护相同布局。
候选增加内部值类型 DecoderWorkspaceLayout 与唯一构造函数，返回
各 region 偏移、子模块原始字节数和总容量。公开 API 查询总容量；
forward 从同一布局取偏移和传给子模块的容量，不再重算一份公式。

attention、MoE、两个 GEMM scratch、PLE、mixed、block、normed、
combined、PLE trunk 的顺序保持。两次 GRRead 仍复用同一 normed。
没有动态 region 容器、解释器、JSON 热路径解析、新分配或同步。
新增一个值类型与一个内部构造函数，删除重复维护的预算与游标推进，
为后续可审计 arena 别名提供同一布局来源；本轮不做别名。

header 同步纠正旧 GR 调用链与“layer 自己申请 workspace”的注释；
实际由 ModelLoad 取全模型最大值并申请，layer 只借用。

## 形状和对齐边界

当前模型 hs=2560、hc=4，使各激活区域大小都是 256 字节整数倍。
新构造函数逐 region 对齐；对非模型 hs/hc，不宣称旧容量逐字节保持。
保留公开 API 的 full==nullptr 兼容预算；真实 full.max_len 影响
T<=4 的 indexer scratch，forward 传真实 full 参数。

## 验收设计与证据

证据 `.q4t-work/e2e/decoder-workspace-layout-20260921/` 保存精确
旧/新二进制、两个改动源文件和 HC 依赖快照、patch、静态库指纹及
HTTP 原始请求结果。先质量 11 题，再五档各三次、输出 256；性能
同时对照直接父版与正式固定参考 fcb5925，不能用连续小幅漂移降低门槛。

只有完整 E2E 通过才采集 4K HTTP 时间线，然后运行
`tools/verify/compare_decoder_workspace.py`。工具从父提交直接提取
旧 forward carve，保留旧算式与顺序，仅将指针转成字节偏移；对照
候选源码提取的布局构造，同时链接 HTTP 候选静态库核对公开容量 API。
执行前检查完整门禁、二进制、源码和静态库指纹。

实际覆盖 T=1/3/4/5/33/257/8192，max_len=8192/208896/262144，
linear/full 与有无 PLE，共 84 个布局，各 14 字段；另核对有/无 full
参数的 168 个编译容量值。84 个布局的全部 14 字段一致，168 个编译容量值一致；
构建零警告、退出 0。
它检查布局，不替代 kernel 数值验证、系统峰值测量或全面并发/MTP 质量。

## 完整 HTTP 性能

| 输入 token | TTFT 均值秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.830147 | 18.643652 |
| 4096 | 2.792251 | 17.908847 |
| 8192 | 5.500133 | 18.180257 |
| 45056 | 31.858452 | 17.871695 |
| 204800 | 165.201948 | 17.125967 |

对直接父版与正式参考均无不利分离范围，输入输出摘要一致。
TTFT 含 HTTP/分词/prefill，decode 按首 token 后总生成数除以总时间；
不由小样本均值变化宣称稳定提速，正式参考不更新。

## 时间线与接受依据

4K HTTP 输入输出匹配，采集、服务退出、SQLite 导出全部正常。与父版
相比，所有 kernel 各自调用数相同：prefill 87703、decode 442935，
对应 255 次 decode 前向。异步分配/释放仍为 prefill 各 554 次、decode
各 141270 次；MoE counts D2H 仍为 prefill 48 次、decode 0 次。
带采集开销的窗口 prefill 3.026→3.023 秒、decode 14.488→14.451 秒；
不把这组 profile 数值当成正式吞吐提升。

接受依据是三处容量/偏移维护收敛到一个构造函数，公开申请预算与
forward 实际偏移使用同一值类型描述，且模型形状的物理布局逐项保持。
代价是增加一个内部值类型和构造函数；没有增加运行时协议、分配器或
解释器。workspace 容量沿用父版，未额外节省内存，也不宣称稳定提速。

下一步独立验证 normed 借用 MoE 工作区，提案见
../dataflow-engine/plans/normed_moe_alias.json；不得沿用本轮 E2E 结果。

二进制 SHA-256：`d28ae958651cb0725304e36c1f7bf3a50a8e7c7679fec99bcc232239e4a95bef`。
