# Offload限额工具与GPU lease接入

状态：候选已接入正式工具路径，103项host合同复核通过，真实GPU待验；
目标仍active，当前不是scope交付完成声明。
旧systemd service由PID1托管，外层GPU guard不能仅凭其名称证明归属。
不能将任意NVML PID或同名unit列入白名单解决。

候选采用systemd scope，执行器保留调用者后代关系，同时设置memcg
MemoryMax/MemorySwapMax与IOAccounting。机制见
[systemd-run官方说明](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)。
当前草稿位于.q4t-work/offload-foundation-20261020/scope-staging，
初版CPU实测源码归档在同阶段scope-cpu-original-source，逐文件匹配
原scope-draft-manifest.json的SHA；映射为scope-cpu-original-source.json。
当前接入草稿身份为scope-candidate-v3-manifest.json。未改动正在执行
基线的源码。

候选合同：随机Description token证明unit所有权，拒绝复用既存unit；
payload PID/starttime/cgroup绑定，pidfd发送定向信号；独立helper记录
真实子进程退出并保留cgroup至多15秒，客户端仍在scope外。helper的
少量host charge计入资源观测，memcg仍不等于Thor物理RAM总量。

已完成CPU实机：正常退出及后代归属、TERM、KILL、执行路径不存在
四种情况均清理scope；MemoryMax=128MiB、swap=0实测属性符合。
7项主机合同覆盖非法名称/限额、既有unit、替换所有者拒绝清理、
缺失退出记录、错误token和pidfd发送。它们不证明GPU载荷与监控兼容。
CPU探针发生于描述性基线期间，无GPU载荷；全局设备/系统计数不作
独占归因，不能据本轮记录声称纯测量环境或严格性能比较。

剩余：基线封存后接入两个旧runner及run_acceptance，完整纳入工具
身份清单；核对异常清理、旧证据兼容及相关host合同，再以单一GPU
lease下有界HTTP验证真实进程归属和资源保留。无需为工具接入重建或
替换已验收运行时；基线身份与新工具验证分别记录。

## 接入草稿进展

已在暂存副本接入run_acceptance、budget/lifecycle两个runner与矩阵
审计依赖清单。异常fallback清理同样验证token，scope审计绑定payload
PID/starttime/token/真实退出，不伪造service的MainPID；旧service证据
按其旧合同分支核对。

当前草稿103项主机检查通过：scope10、runner18、原矩阵/partition/
生命周期71、新scope证据4。最初暂存测试有16项临时根路径错误、36项
缺少相邻审计测试源文件的错误；均为暂存布局问题，补齐读取布局后
重验对应项通过，日志原样保留。未改变生产测试实现以绕过失败。
当前草稿尚未重新做CPU实机或GPU实机，初版CPU通过仅覆盖原SHA。
下一步等待基线封存后将候选移入正式位置、直接复核主机合同并进行
有界真实GPU接入，不追加五档性能样本。

## 正式接入后实机验收预算（启动前冻结）

同一GPU lease覆盖完整驱动，先做CPU scope正常退出/TERM/KILL/启动
失败及64MiB受控OOM，确认实际memcg限制与清理，不将OOM当模型失败。
GPU最多2次加载：新budget runner以C256、L2=16、mirror8、16GiB
host/cache上限、max-len262144、普通模式完成1条1024-token质量请求，
对比最终19ea4e57已封存质量结果；另一次相同runner在NVML确认目标PID
进入GPU后发送SIGINT，要求该scope及后代清理，原中断证据完整保留。
每次runner上限1200秒，无自动扩样。全程不清模型缓存，不宣称严格
物理RAM限额、SSD冷态或性能收益。lifecycle入口的公共启动/清理后端
复用此次GPU覆盖，业务六题/取消完整序列不在本次新增复测范围。

实机驱动已准备为tools/verify/inference_metadata/run_scope_integration.py，
仅做AST检查，尚未执行；启动前要求基线guard已COMPLETED。其受控
中断通过NVML正向确认目标PID后发送SIGINT，核验wrapper退出、scope
移除及进程组无存活成员；失败保留原输出，不将控制器补清理当runner
自身清理通过。完整阶段仍需由seal_stage/gpu_exclusive包裹。

离线审计入口为tools/verify/inference_metadata/audit_scope_integration.py，
核对封存摘要、当前工具身份、五种CPU结果、真实payload PID/starttime/
owner token、正常HTTP质量与NVML/cgroup绑定、受控中断见证及最终清理。
目前只有AST检查，必须等真实阶段完成后执行；脚本存在不代表验收通过。

## v1具体失败与修复

v1正常/TERM/KILL/启动失败完成，受控64MiB OOM使payload退出-9，
systemd记录Result=oom-kill，但本机DefaultOOMPolicy=stop提前结束
scope，最终memory.events文件消失。计数未知未当0或通过；v1 guard
为COMMAND_FAILED并完成清理，GPU加载0，现场已封存。

显式设置OOMPolicy=continue，仅让helper在载荷死亡后保留最终计数；
载荷OOM仍被判失败，不自动恢复模型。机制与
[systemd scope官方说明](https://www.freedesktop.org/software/systemd/man/latest/systemd.scope.html)
一致。每次创建scope都校验实际OOMPolicy，因此v2重验受影响的scope
启动/退出/清理CPU项，再执行尚未开始的2次GPU预算；不重跑五档。

## v2 GPU环境中断与一次定向复现

v2五项CPU实机均通过，含保留的OOM计数。GPU加载时NVML worker卡在
init，超过既定5秒（实际5.39秒）后guard记录ENV_ABORT并清理，未发
HTTP。最后观测NVML目标PID分配约41.20GB；已观测memcg max/oom事件
均0，不能据此断言限额导致超时。清理后NVML查询0.064秒返回OK且
无计算PID，scope列表为空；保留原始超时/中断记录，不视作验收通过。

v3仅对未完成GPU部分做一次同配置诊断复现，仍用16GiB host/cache限额、
NVML默认后端和5秒门；无定向cache驱逐。复用v2相同scope源码及封存
CPU五项证据，不重复CPU采样。原GPU预算的正常质量/受控中断仍待完成，
额外v2中断加载单列为ENV_ABORT，不能隐藏在2次有效加载的计数中。

## v3复现与v4定向限额对照

v3再次ENV_ABORT：NVML init超时且worker在额外1秒清理窗口内未退出，
记录CLEANUP_FAILED。稍后目标进程及scope均已退出；内核18:01:36记录
GPU0 _memdescAllocInternal 返回NV_ERR_NO_MEMORY。该记录支持调查
驱动分配/资源压力，不证明16GiB memcg是唯一原因，也不是代码缺陷定论。

v4仅将host/cache memcg上限由16GiB改为64GiB，保持其余模型、GPU
配置、NVML5秒门及正常质量/受控中断预算；复用同源码CPU合同。
这是针对新分配失败证据的限额对照，若通过只接受64GiB配置的工具
接入；16GiB保留未通过，不能宣称严格物理RAM上限或低预算offload可用。

## v4限额对照未消除故障，v5缓存对照

v4 64GiB仍在GPU加载时NVML init超时，ENV_ABORT，未完成HTTP。
不支持仅放宽memcg即可解决。现场模型缓存继承自基线，主机Cached约
63GiB，MemFree约8GiB；KReclaimable约49GiB不能与CUDA计数简单相加。
历史OFFLOAD_RAM_PROTOCOL的外部预热文件探针已证明旧cache不会
自动迁入新组；这提示调查继承缓存，尚不证明NVML故障因果。

v5保持64GiB及原有监控门，唯一新增--clear-model-cache：只读打开模型
文件，POSIX_FADV_DONTNEED后观测payload驻留必须为0才启动。每次
加载前执行，无全机drop_caches、无文件内容修改。仅验证既定质量/
中断合同，不追加五档或声称冷SSD性能；若仍失败不自动继续重试。
