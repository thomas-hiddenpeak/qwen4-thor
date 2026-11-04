# serve 观测与编译资源首组

起点：输入合同 A 组已交付 ea678009 / 90372798（源码 b71f026）。
分类为业务正确性和资源治理，不作性能优化接受声明。

## 冻结实现与仍有的边界

- F5：新增 slot_wait / admission / generation_complete 三个明确口径；
  既有 queue/e2e 保留原数值含义，HELP 明确为弃用别名，避免下游
  仪表盘无提示地改口径。response 才是完整本地响应写完；不保证
  对端应用已消费。新增固定六类取消计数，不以用户 / ID 作标签。
- F6 首个资源门：所有 constrained 请求在词表准备前独立竞争一个
  租约，忙时 503/structured_capacity_exhausted；不排编译队列，
  普通 auto/raw 不竞争该租约。取消不会提前归还仍持有 grammar 的
  租约，直到请求 / grammar / 输出对象释放之后才准入下一请求。
- 词表记录保守 storage（vector 和字符串 capacity）最多 32 MiB，
  已编译 grammar 仍最多 64 MiB，单 compiler 线程，禁用 grammar
  cache。新增 active/preparing/rejected/storage/failure/prepare 时间
  指标，capabilities 明示限制。
- **F6 尚不全部闭合**：XGrammar 编译本身没有抢占接口，取消在前后
  协作检查；shutdown 保留安全 drain，可能等待编译返回。32/64 MiB
  是指定存储 / 结果界限，不是 compiler 峰值 RSS 或硬耗时保证。
  本组不以提前销毁对象 / 强杀进程冒充正常取消，也不扩充混合并发。
  后续硬时间 / 峰值保证需要独立设计和验收。

## 冻结验证

host ON/OFF、Thor 必需及后端门；资源互斥、取消后保留所有权、异常
释放、容量溢出与低基数标签合同。指标由真实 HTTP snapshots 与
请求诊断交叉核对，保留旧别名相等证据，不靠名称声称语义。

同一 GPU guard 中至多 3 启动 / 10 次模型请求尝试 / 6 拒绝请求 /
30 分钟：普通＋严格并发准入、普通槽等待、未读取 SSE 的严格客户端
显式取消、短 deadline、取消后的真实工具回填、请求中的正常 shutdown，
以及安装后普通 MTP / JSON schema 健康。每个场景有上限，失败留证；
不自动增加压力或重复全矩阵。

读取进程 RSS/HWM 与 MemAvailable 等原始计量，结合已有 guard 的
NVML 记录观察统一 RAM；不把 CPU/RSS 和 GPU 分配相加声称物理峰值。
没有慢客户端真正写满内核发送缓冲的保证，后续背压专门场景仍待验。

CPU 检查通过：host ON156 / OFF151、Thor 必需117＋后端5、驱动6 /
回退6，构建零警告。A组已通过且身份未变的输入 / 模板 / 客户端证明
复用；真实混合清单为8个请求尝试 / 1个503 / 2启动，在上限内。
## 交付结果

2026-11-02 19:12 CST 混合验收、安装健康及 finalize 完成。源码
`caced6d`，当前默认 binary
`f03ee0d465419c2320552cfc20a589c5104e139120baa7204fed60a71d80b56b`，
sidecar `a1e9d24e3119a76939ed0859ac3d2f27bf49610df1a5d653efea93482ff08402`。
继续完整 head / MTP k3 默认，受约束请求 plain masked decode。

- 生产 HTTP 支架5项、业务48.38秒、安装健康39.43秒通过；执行窗口
  88.42秒。共8次模型请求尝试 / 1个过载503 / 2启动，5次正常完成、
  显式取消 / 短deadline / shutdown 各一次预期中止。guard COMPLETED、
  ENV_ABORT0，无重试或候选失败。
- 未读SSE的严格请求运行时，第二个严格请求得到503；普通请求成功
  进入槽等待，显式取消前者后输出710003。取消JSON/SSE终态检查未
  出现成功finish；deadline返回409/deadline_exceeded。
- 取消之后真实读取 docs/MODEL.md 并回填，架构与行号引用正确；
  工具响应3.508秒，最终回答3.237秒。带请求SIGTERM记录server_shutdown，
  正常退出；安装健康普通MTP输出710003，schema输出323/中文。
- 关闭前固定取消计数为explicit=1、deadline=1，其余0，总数2；
  之后shutdown以日志验证（监听已关闭，不能再查询最后metrics）。
  structured active/preparing/vocab计数归零，拒绝1、准备失败0。
- 同一快照：admission累计0.542056秒、slot wait累计0.049827秒；
  后者与请求诊断逐项相符，预处理没有混入slot等待。旧queue/e2e与
  新名直方图逐样本相等。generation包含取消尝试、response只统计
  成功完整写入，不能比较不同样本总体的sum来判断单请求阶段顺序。

## RAM 观察及未证明项

业务服务阶段RSS从704.82MiB升至最高采样1014.34MiB；进程VmHWM
为3903.98MiB（含启动，不等于编译峰值）。采样的系统MemAvailable
最低约32.13GiB。取消并清理后RSS仍高于初始值；不能从对象计数归零
推断allocator已将物理页全部返还，未对保留页逐项归因。原始进程 /
系统快照和NVML监控均保留，不累加重叠指标冒充统一RAM物理峰值。

两次模型退出均0 in-flight，startup reservation分配全部释放；
最终NVML计算PID为空，GPU锁释放，无自有恢复标记或常驻服务。
这些结果关闭F5与本组独立准入/协作取消合同；**F6的硬编译时间 /
峰值RSS边界仍未闭合**，未知schema极值和更多并发组合不能沿用本次
通过结论。统一HTTP request-id、心跳及真正发送背压也未在本组加入。

原始证据及实际归档文件SHA见
[封存清单](evidence/serve-resources-20261102/sealed-files.json)。
本机 `.q4t-work/serve-resources-20261102/package/` 保存前一默认
ea678009 / 90372798、当前pair与源码bundle；构建产物留本机，文档
归档仅保留其身份收据。回退继续要求对应冻结源码和共享guard，未知
pair不覆盖。模型 / 数值源码未变：旧manifest中仅5个服务文件变化，
新增两份资源 / 原因合同头；A组未改的输入 / 模板 / 客户端证据复用。
发布的历史协议使用独立副本并指向原sidecar，已核对前一协议未被
当前metadata更新改写。没有性能优化、全上下文或生产多租户接受声明。
