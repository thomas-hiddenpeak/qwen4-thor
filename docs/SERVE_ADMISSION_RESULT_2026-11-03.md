# SRV-02b结果：有界准入与容量FIFO已限定交付

2026-11-03，**ACCEPT_BOUNDED_ADMISSION / DELIVERED**。新增共享chat/preflight
inflight上限、slot等候上限和容量适配FIFO，取消/异常自动注销等待项；
CLI、有效配置schema和固定指标同步。生产候选通过后已成为默认程序。
本项为业务可靠性，未作吞吐/性能非劣或工业化整体完成声明。

## 身份和实际接受范围

| 项目 | 身份或结果 |
|---|---|
| 冻结源码/驱动 | 919cc01f807c7c6cc6b096edfdd2c1576dc8b9ba |
| 默认生产ELF | c16caf0573ed2ecdf23237229a54a23a0d4ead46e86f6b25e7a44436d3caafe0 |
| 默认sidecar | be4cd02115f97a232aa885b487fa5e42bf044e9ccfa1bca8be31701a0c8d4463 |
| 专用验证ELF | 234fae71f3970a85c97383405eecd57ea6477cc0ad75dbeade6c2a147bef396b；不安装 |
| 协议SHA | b70fc28ab551740f3543061decadbac8f945f8ea8e1923364ce11a8aa48c7ca6 |
| 运行时文件 | 192；模型/数值源码未变，新增请求所有权与slot队列，提取原Alloc/Free函数 |
| 真实尝试 | 10生成（含2次有意取消）、4专门拒绝、2模型启动，首轮完成，无自动重试 |
| 时间与环境 | 业务81.573秒、首次锁至finalize110.008秒；guard COMPLETED、ENV_ABORT0 |
| 预算 | 计划10/4/2；原硬限12/8/3及1800秒保持，备用额度未用 |

两个模型组、原生HTTP生命周期和安装健康均由同一gpu_exclusive驱动
持锁。生产与专用构建的运行时源码/协议相同；仅故障宏不同。专用使用
已有指定请求的响应前暂停，实际gate0.806秒，服务器硬上限10秒；
不改模型logits，不注入模型输出字节，不把专用二进制作为默认程序。

## 真实业务与资源证据

生产组为完整head、默认MTP k3、单槽8192/prefill2048、inflight1/queued0。
strict仍采用plain受约束路径。合法64项数组对象进入slot后，普通chat
和preflight分别被总准入拒绝，均503 capacity_exhausted；健康、ready和
live控制仍可用。显式取消发生于prefill，SSE错误code explicit_cancel，
客户端拒绝把它作为完整轮次。随后active/body/queue/strict/worker全部
归零，槽1/1空闲，worker started=reaped；202本身没有被当作排空证明。

普通JSON恢复准确返回710003。固定Node24.18.0/openai7.47.0零自动重试
完成严格read_project_record，实际只读docs/MODEL.md第1..24行；调用ID
关联真实工具结果，最终回答Qwen4ExpForConditionalGeneration并包含
准确引用[docs/MODEL.md:L1-L24]。完整wire、参数、工具结果、历史和usage
均保留；本组3个worker最终全部reap。

专用组为plain、异容量8192/2048、inflight8/queued2。H已完成生成但
在响应前持有大槽；A、C依次等待。队列满后普通大请求503，strict大请求
HTTP200+初始assistant角色帧+唯一queue_capacity_exhausted+[DONE]。
无生成payload或成功finish/usage，拒绝前后prompt/generation计数不增；
grammar worker仍按所有者清理。总准入拒绝计数0、队列拒绝计数2，未混淆。

小请求B绕过无法使用小槽的两条大请求并正常完成；取消队头A后补入D，
释放H的gate。实际slot-admission次序恰为H0/B1/C0/D0，A未获得槽；
H/B/C/D均准确回答710003，最后strict JSON恢复{"ok":true}。
最终槽2/2空闲、所有请求/排队/body/strict计数0、worker2/2已reap。

10份生成wire/17封套、4份拒绝HTTP/5封套均通过相应机器合同。FIFO是在
准备完成进入slot admission时排序，允许容量不匹配时使用空闲小槽；
不是HTTP到达顺序、每租户公平或完成时间承诺。配置值不是连接/RAM预留。

## CPU、失败记录和复用

host OFF206/ON215、生产必需172、直接生产等待6、grammar后端9通过；
HTTP原生完整13（7生命周期+6等待）在GPU锁内通过。生产/专用/host/probe
构建零警告。新native probe验证计划chat/preflight/cancel输入7种通过。
CPU真实等待覆盖零/满队列、FIFO/容量绕过、首中尾取消、断连、deadline、
shutdown、GPU状态和异常注销，不声称每种排列都另外做了模型HTTP。

保留以下夹具失败，均未触发GPU请求：

- 原生配置15项初次10通过/5失败：检查器误写default，实际稳定来源为
  builtin_default。相同probe修复5项通过，另10项按身份复用。
- 驱动初次11项10通过/1失败：取消负载数组根不合法；修成对象后又被
  128项超过支持64项拦住。最终对象内64项通过，未扩生产schema合同。
- 源码复核发现SSE会先发role，验收器允许初始角色但禁止内容/成功终态；
  新增2项，受影响7项通过，驱动现13项，未受影响项复用。

模板、工具历史、分词和未变数值依赖复用既有证明，不重复数值矩阵。
配置与队列运行时受本次新证明约束；旧协议SHA的整包验收没有冒充新SHA。
SRV-03b严格长输出背压仍未证明，单独失败/预算保持；访问、长稳、通用
任务恢复和整机RAM执法仍未完成。

## 内存、交付和回退

10次阶段资源观测：parent RSS观测最大944.332MiB，worker RSS最大
221.082MiB，global MemAvailable最低33.004GiB，无采样错误。PPid和
start ticks核对父子身份；这是稀疏观测，不声称完整运行峰值。三类值
分别报告，不相加为唯一物理RAM，也不把RSS当全部CUDA统一内存。

已保留旧5642eb68/6aca6a3e、真实build cache和完整历史回退链。安装仅
生产candidate及匹配sidecar；安装路径CLI和隔离grammar worker握手通过。
HTTP健康明确复用同SHA生产候选的普通/真实工具证据，无另起安装模型。
07:13:45 CST GPU计算PID空、共享锁可用，无常驻模型/worker或恢复标记，
MemAvailable116.997GiB。没有drop_caches或干预其他服务。

收尾发现接受检查依赖当前工作区源码，会使后续正常开发阻断历史回退。
当前rollback动作改为核验已封存package、旧pair/cache和目标已知新旧pair，
不依赖此后工作区内容；接受执行仍保留严格源码检查。CPU两项通过，未
实际回退已交付版本、未追加模型验收；这是919cc01f后的运维工具修正。
证据见[收尾回退检查](evidence/serve-admission-closeout-20261103/README.md)。

需要回退时，从本仓库根目录使用一个新的独占输出目录：

```bash
Q4T_ROLLBACK_OUTPUT="$(mktemp -d "$PWD/.q4t-work/serve-admission-rollback.XXXXXX")"
/usr/bin/python3 -B "$PWD/tools/evalscope/gpu_exclusive.py" \
  --output "$Q4T_ROLLBACK_OUTPUT/lease" -- \
  /usr/bin/python3 -B "$PWD/tools/serve_admission/stage.py" rollback \
  --output "$Q4T_ROLLBACK_OUTPUT"
```

未知默认身份/外来恢复标记拒绝覆盖；旧包及当前交付包不得删除。

## 原始证据与下一阶段

[原始归档](evidence/serve-admission-20261103/README.md)复制227份记录，
共231文件/3016986字节；含全部原始失败、构建、CPU/真实wire、指标、
服务/guard日志、源码/模型身份、安装和回退绑定。10份ELF/object/bundle
仅登记本机身份，原package/execution及仓库副本各有seal；本机
`.q4t-work/serve-admission-20261103`和之前回退链保持。
交付与收尾238份Git blob逐字节核对通过；首次目录add遗漏的ignored .bin
已显式加入，未改原wire。三份CMakeCache保留原始末尾空行，不改证据
迁就格式检查；源码diff检查通过，见[入库核对](evidence/serve-admission-custody-20261103.json)。

下一包按[SRV-01b接收拒绝与输出失败观测合同](SERVE_OBSERVABILITY_2026-11-03.md)
继续：先补缺失的原因分类和固定指标，再冻结其独立真实接受范围。
长期目标继续active，本次不表示整个serve已达到工业化验收。
