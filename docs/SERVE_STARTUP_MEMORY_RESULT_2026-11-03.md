# SRV-02e 启动可用内存与Thor计量结果

限定接受并交付启动检查。当前默认ELF `f69982d5`，sidecar `34de1c8f`，
protocol `80529db3`；实现冻结812f78d4，验收驱动纠正冻结7565e3b1，
200个运行时来源可核对。完整head、默认MTP k3；没有改变模型数值、
模板、分词器、grammar或容量选择，相关旧证据明确复用。

[冻结合同](SERVE_STARTUP_MEMORY_2026-11-03.md)、
[计数纠正合同](SERVE_STARTUP_MEMORY_CORRECTION_2026-11-03.md)、
[原始证据索引](evidence/serve-startup-memory-20261103/manifest.json)。
工业化长期目标保持active；本项不证明整机RAM硬限额或新ELF长稳。

## 实际交付行为

默认新增2048MiB启动余量。metadata前读取一次可用量；大分配前重新
要求既有完整峰值估算加余量；ready前再次读取剩余可用量。未知/
不一致/缺估算/加法溢出拒绝启动，0和--no-budget明确绕过；原运行期
内存准入独立。配置和capabilities公开策略、绕过和实际三个快照。
快照不是预留，不能阻止其他进程在检查后分配；不自动缩容量或重试。

真实正常启动，三次新读数与实际配置逐字段一致：

| 检查点 | 可用量 GiB | 要求 GiB | 结果 |
|---|---:|---:|---|
| metadata前 | 116.618 | 2.000 | admitted |
| 分配前 | 116.335 | 92.435（峰值90.435＋余量2） | admitted |
| ready前 | 34.819 | 2.000 | admitted |

另两次真实启动负例：1TiB余量在metadata前拒绝；64GiB余量允许读取
metadata，在完整估算后以154.435GiB要求拒绝。两者均exit1，未进入
模型权重分配或监听。原始退出、日志与GPU空闲证据均留存；没有靠
耗尽主机内存触发拒绝。未知观测/溢出等边界是CPU证据，不冒称实机故障。

## 验收与保留的原失败

CPU OFF234/ON243、必需host200、后端9、驱动6及配置15+10通过；
引擎、探针零警告，输入清单10个源码锚点复核，路由/字段/嵌套白名单
未变。首轮lease内原生HTTP24通过。

首轮五次真实生成全成功：普通JSON/SSE、strict JSON、Node24.18.0 /
OpenAI JS7.47.0 required资料工具及none最终回答。实际读取本项目
MODEL第1–24行，准确回答architectures并带真实引用；5份wire、19个
封套离线核对。随后驱动错误断言只有2个worker，使原整体
[FAILED](evidence/serve-startup-memory-20261103/original-result.json)与
[COMMAND_FAILED](evidence/serve-startup-memory-20261103/original-guard.json)
保留。实际strict JSON、required工具、none文字各需一个约束worker，
3次start/ready/reap和日志匹配，owner归零、slot1/1free，正常退出。

纠正只补2项CPU正反断言、只读复核原证据和原先未执行的4个探针。
没有重跑原生HTTP/模型，也没有追加生成预算。原最后三个health HTTP
与独立末样本未执行，明确缺失；已有ready指标、业务HTTP、drain资源
和clean shutdown作为限定接受依据。45个连续模型样本单独保留，
不是总物理峰值或新一小时稳定证明。

累计3次启动尝试（只有1次加载模型）、2次启动拒绝、5次生成、4个
探针、9次控制/发现请求，346.183秒完成finalize，未超过原1800秒及
各项次数上限。首轮失败和纠正是两段独立lease；纠正guard COMPLETED，
未报告ENV_ABORT。无自动重试或扩样，未作性能接受/非劣声明。

## 256MiB分配的实际记账

四个独立进程/owned scope，依次各分配256MiB，实际逐字写入并核对
边界值，释放API成功后退出。runtime13030、driver13020、SM11.0、
integrated/pageableMemoryAccess/concurrentManagedAccess均1。
模型不与探针并发，scope MemoryMax2GiB只约束控制器记账域。
每检查点两个样本，managed另有CPU触页后样本；共26个外部样本，
13份CUDA快照。下面是held相对各自baseline的增量，单位MiB：

| 分配 | 父进程RSS增量 | cgroup memory.current增量 | 全局可用量下降 | CUDA free下降 |
|---|---:|---:|---:|---:|
| 匿名host | 256.000 | 256.262–256.406 | 246.164–249.816 | 249.898 |
| cudaMalloc | 0.148 | −0.008–0.000 | 257.195–257.430 | 257.496 |
| cudaMallocHost | 256.063 | 257.516–257.750 | 266.527–281.051 | 283.492 |
| cudaMallocManaged，GPU触页后 | 256.168 | 256.871–257.000 | 241.125–247.137 | 242.180 |

本机这次device分配没有表现为相当的memcg/RSS增量，所以不能把
MemoryMax当作完整CUDA统一RAM限制。其他三类的256MiB级增量只证明
这次条件下能被观测，不能外推所有大小/映射/内核路径的完整覆盖。
各域重叠、采样非原子且受背景活动影响，表中各列不可相加。

释放后匿名RSS回基线，其他RSS剩余约0.063–0.199MiB；memcg增量接近
基线。全局/CUDA快照没有逐字节立即恢复，尤其pinned；不据此断言
泄漏或完整即时物理释放。保留全部baseline/host/held/freed原始值，
不把释放API成功替换成物理峰值/恢复的证明。无整机OOM或硬限额试验。

## 交付、回退与保管

已替换默认ELF及匹配发布元数据。实际安装CLI/隔离worker握手通过；
HTTP明确复用相同SHA候选，没有新增安装路径模型验收。
[完成记录](evidence/serve-startup-memory-20261103/finalized.json)与
[安装健康](evidence/serve-startup-memory-20261103/installed-health.json)可核对。

`.q4t-work/serve-startup-memory-correction-20261103/rollback-bundle/`
封存新f69982d5/34de1c8f与直接旧c597adc3/a350c978、各cache和v4规则。
两pair只读核验通过，本阶段未做实际回退切换。只读历史链39个匹配pair，
末端原有previous_release_was_stale边界仍不可安装，未伪造第40个pair。

14:28:26 CST后检GPU空、锁可用、所有owned scope移除、无恢复标记，
MemAvailable116.970GiB。457原始记录无损打包为17份Git文件约0.774MB，
14个ELF/object/source bundle留本机并逐个校验身份；原日志不称全档脱敏。
17份Git blob/457解压原件/14本机身份和579变更链接核对通过，见[保管receipt](evidence/serve-startup-memory-custody-20261103.json)。

下一步审计和补齐分配失败传播、已拥有资源的回收与可证明的资源策略；
继续保留全机物理硬保护、实际网络/鉴权/通知、多租户、天级运行和通用
任务恢复缺口。采样保护不改称硬限额，不恢复CPU容量offload路线。
