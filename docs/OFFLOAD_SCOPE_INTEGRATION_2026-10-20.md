# Offload限额工具与GPU lease接入

状态：草稿CPU合同已验证，尚未接入生产工具/真实GPU，目标仍active。
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
