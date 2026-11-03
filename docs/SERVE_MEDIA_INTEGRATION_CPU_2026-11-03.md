# SRV-02i接线：CPU验收与GPU待验边界

2026-11-03，本机Thor，可靠性任务。按[接线合同](SERVE_MEDIA_INTEGRATION_2026-11-03.md)
完成实现和CPU验证；**尚未GPU/原生HTTP/模型验收，不替换默认**。
长期工业化目标保持active，下一步冻结02h+02i组合业务验收驱动。

## 实现与候选

共享媒体计划已用于serve和processor：全部源头/帧及累计容量先检查，
媒体token展开后重新计算槽位预算，实际槽位及解码尺寸再复核。user-only
媒体字段、canonical base64、PNG/JPEG与MIME一致性、稳定错误位置已接入。
stb的非抛出分配回调记录256MiB所有权配额；配额拒绝和真实分配失败分开。
取消在item/frame与GPU提交前协作检查，显式媒体启动查询实际attention
函数shared能力，未知则拒绝启动；config/capabilities公开对应策略。

- 生产候选：`280b95dceabe5a3b1fd794019102de0dc9f0b62e8a3aa9fcc30eb5c57fdfd793`。
- validation：`00718deab9fd07729cd0bd1409bf04180297db3312fd493f2e6eb547cab87897`。
- protocol：`29810d03d0cb0603bd86222b18d8a53693d888a28d2033a3de1c73d4677a86e8`，
  211运行时来源；第一轮与纠正后运行时代码/生产ELF身份相同。
- 当前默认仍为`3fd31a21` / sidecar `3ba0b8d3`，候选与旧默认pair保存于
  `.q4t-work/serve-media-integration-20261103/cpu-accepted/`。

13个视觉kernel/forward及BICUBIC函数的去注释/空白代码核对未变；
几何采用共享宽整数计划，不把该审查当作整个模型数值或质量验收。
媒体仍是显式实验范围，公开文本request schema没有扩成通用VL合同。

## 已验证及首轮失败

生产、validation、公共host两模式及独立探针全部零警告构建。

| 范围 | 本轮结果 |
|---|---|
| 公共host OFF/ON | 有效覆盖258/267；首轮257/266通过，均有同一个旧夹具失败，修复后各只重验1项 |
| 必需host | 有效覆盖224；首轮223通过、1项失败，修复后只重验该1项，逐名核对完整清单 |
| 结构化后端 | 9通过 |
| 配置与启动配置 | 15+10通过；新增media_limits按闭合schema验证，历史v1允许省略该新节点 |
| 共享预检核心 | 80合同通过，持续禁止分配下0分配尝试 |
| 实际processor几何 | 12张PNG/JPEG × image/2帧/5帧，共36通过 |
| 配额/取消/回收 | 134实际processor调用通过，含30配额拒绝、80取消检查边界，失败输出清空、所有权归零及双线程预算独立 |
| host异常响应/processor | 22响应组合；41处C++及27处独立C分配持续拒绝；8正常/恢复调用 |
| 参考像素 | a/b图及两相同帧视频共4组逐字节相同；transformers5.16.1/Pillow12.3.0 |
| 静态合同 | inventory100来源锚点及生成语义防漂移通过 |

独立core/processor/配额探针启用ASan/UBSan，无报告。配额探针以实际
解码峰值减一、等于峰值和更低配额检查失败/恢复；PNG峰值84988字节，
JPEG352495字节。没有制造物理OOM，也没有宣称已验证256MiB边界的
所有压缩炸弹；该额度是C分配可见所有权，不含allocator内部瞬态。

首轮CPU整体FAILED完整保留于`cpu-01/`，不是首轮全通过：

1. `chat_tool_history_media_order_guard`仍期待有序tool结果允许媒体，
   与事前冻结的user-only合同冲突。仅改该夹具预期，文本tool结果仍接受。
2. 配额探针报告“cancel after decoding”。反汇编确认优化器移除了
   builtin malloc前的C++域标记，错误字符串分配被算成decoder分配。
   旧持续拒绝探针也有相同问题。改为C++显式调用`__real_malloc`并单独
   跟踪所有权，新增C/C++计数自检；生产运行时代码未因此修改。

旧记录的“68处C分配”实际包含41处C++调用，**不能继续作为68处独立
C边界的证据**。纠正后27处C边界逐一持续拒绝并回收通过；旧原件与
原二进制保留，不追改历史日志。`cpu-correction-01/`只重验受影响项，
其余首轮成功按相同来源复用；固定响应及像素随纠正后的探针再核对。

封存首次还发现公共host构建未导出compile_commands，原打包器误假定
该文件存在；未完成包保留于`cpu-accepted-incomplete-01/`。仅修正封存
逻辑，明确记录缺失的导出文件，保留原构建日志，没有为此再执行测试。

## 保管与未完成项

[证据目录](evidence/serve-media-integration-cpu-20261103/README.md)保存完整
原始压缩记录、来源快照、两轮结果与像素、环境记录及本机大产物身份。
698原件封存为9份Git文件3,118,147字节，35本机ELF/对象身份逐项核对；
[保管核对](evidence/serve-media-integration-cpu-custody-20261103.json)还验证
9份暂存Git blob与原文件/压缩成员一致，包含原失败二进制和未完成包。
旧02h包的seal继续匹配，旧execution仍不存在；其source gate因当前
运行时更新而不适用，不能直接launch或仅刷新哈希绕过。

18:10:56 CST只读NVML仍发现vLLM PID2336565占93,864,288,256字节。
不干预该服务，本批GPU/模型/原生HTTP尝试均为0。下一组合验收须保留
02h所有权/错误/真实视觉上传、真实项目工具与完整SSE范围，再覆盖02i
提前拒绝、同PID恢复、合法图/视频、实际函数能力和取消，执行前另冻结
完整驱动、身份和次数/墙钟预算，全阶段持有同一GPU lease。

CPU处理目前仍持有model锁，取消不抢占单次C解码或kernel；头部验证
不证明压缩数据一定有效。全机物理RAM硬保护、真实网络/访问/多人配额、
新ELF长稳及通用恢复继续独立待办，不以本批结果宣布工业化完成。
