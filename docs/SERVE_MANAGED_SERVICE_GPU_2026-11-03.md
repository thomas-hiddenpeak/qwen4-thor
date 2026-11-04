# SRV-14 实际systemd托管与业务验收矩阵

使用95547cc5的CPU包，manifest为
`e33a0ac2771d450a380c2b49eed8d6a5221b5e67c6b10ea6c70eca5199de5787`。
ELF280b95dc、sidecar18c9d2c5及211运行时来源保持，原29托管/30guard
和配置对照证据复用。本批增加验收驱动，不改变被测托管逻辑或模型。
业务可靠性验收，不作性能非劣或优化结论。

## 锁与单元

临时user systemd单元使用生产`unit_text`生成的ExecStart与所有服务选项，
只把Description改为本批随机所有权标记。使用`link --runtime`，没有
Install段，不安装持久/开机启动。被测包内控制器实际调用共享
`gpu_exclusive.run_guard`；它是整个GPU阶段的唯一lease owner。
验收客户端在该cgroup外，但全部生成/控制请求以及两个模型实例、
重启等待均由该连续lease覆盖，不再嵌套第二个持锁GPU驱动。停止后
只读核验、调用幂等stop及移除临时单元，不再次启动模型。

在两个ready点分别核对同一控制器boot/start ticks、同一锁dev/inode、
controller/worker/后端三方实际fd、unit MainPID与所有进程的cgroup。
guard的NVML与/proc监控贯穿阶段；发现竞争或监控失败保留ENV_ABORT，
不停止外部业务。轮询不能证明短于采样间隔的干扰绝不存在。

## 固定序列

1. 原单元不存在、GPU空；启动实际包：full-head MTP k3、seq1、8192
   context/2048 prefill、inflight8/queued2、2048MiB floor/启动余量、
   512MiB恢复滞回、loopback18120。restart_limit显式为2。
2. 活跃期间做三个拒绝：第二个guard抢锁返回LEASE_BUSY且不执行子命令；
   复用同运行目录拒绝；错误manifest摘要拒绝且不创建运行目录。
3. 首个后端5次真实生成：普通JSON、普通SSE、严格`{"ok":true}`，以及
   一次strict资料读取/一次答案回填。读取真实docs/MODEL.md:L1-L24，
   答案须有准确architectures值及工具给出的引用。每次保存完整EOF wire。
4. 空闲对账：5成功、无额外请求、owner归零、slot1/1free、grammar
   started=reaped。仅SIGKILL已经核实身份的本批后端；监督器回收旧组
   后按配置恢复。锁和控制器不中断，不重放已完成HTTP或工具。
5. 第二个后端3次生成：普通JSON、真实资料工具/答案各1；本实例计数
   为3成功，不能把上实例计数拼接。核对旧组已不存在。
6. 空闲时再SIGKILL第二后端，等进入RESTART_WAIT后调用正式stop入口。
   原始guard须COMPLETED、terminal STOPPED/cleanup_confirmed；实际
   后端启动2次，restart预算预占2次，其中1次替换、1次等待被停止。
   不将预占计数2写成实际重启了两个后端。
7. 重复stop应返回同一terminal；观察两个5秒退避长度后单元仍inactive、
   MainPID0、NRestarts0，只有2份attempt记录。旧owned进程、监听和GPU
   均无，原锁可取得；然后删除本批runtime链接并确认unit不存在。

计划**2启动、8生成、6负面/终止场景**；沿用开发合同硬限3/8/8、240
控制观察和1200秒总墙钟。不自动重试或追加请求。生成各128 tokens，
工具与答案各512 tokens；request timeout180秒；无真实内存压力。

监督器每次探测最多4个GET且每轮之后至少等待5秒。为固定240控制
预算，GPU业务窗口额外限定220秒（从systemctl start前至terminal），
覆盖最多`floor(220/5)+3`轮、保守预留188次GET；显式HTTP及systemd/
CLI命令最多48次，其中8次保留清理，总保守上限236。不是声称观测了
精确188次；启动期未监听的探测可能没有发出HTTP。常规单元状态由
本地状态文件读取；完整守护监控及状态变化另存。窗口不足就失败退出，
不把300秒启动合同改成已实测，不自动扩预算。清理不会因业务预算
耗尽而跳过；超额只在安全清理之后报告失败。

## 出口

CPU先验证驱动的请求预算、重复/失败计数、缺终态wire、外部unit
保护、清理预留和来源门禁。提交后prepare冻结本批来源，再execute。
完整wire/工具结果、guard原记录、所有权、unit/journal、状态/终态及
清理记录封存。默认pair不变，不重新做已验模型数值或完整性能矩阵。

本批证明实际systemd管理下的生产控制器路径；不声称独立前台控制器
被SIGKILL后的真实GPU清理已验（该合同已有CPU进程证据），也不证明
常驻生产部署、通知、多租户/公网入口、新ELF小时或天级稳定。
