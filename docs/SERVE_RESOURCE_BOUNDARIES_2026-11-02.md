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
当前默认未替换，等待 GPU guard 下混合验收与安装健康。
