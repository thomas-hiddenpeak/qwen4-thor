# SRV-02k：托管后端host记账域与进程树边界

2026-11-04。业务可靠性任务，不是性能优化。补齐已接受的CUDA申请
额度/全局MemAvailable退场之外的host memcg与pids控制；不把它称为
完整统一物理RAM硬上限。本机02e已证明cudaMalloc存在memcg漏计。

## 冻结实现范围

- 显式可选资源配置绑定包摘要，旧v1-v4包保持原行为；新资源包v5。
  后端leaf设置memory.high/max、memory.swap.max=0、memory.oom.group=1
  与pids.max；参数必须明确，不给所有部署套用未经验收的默认限额。
- 通过本机systemd user D-Bus新建唯一token绑定的委派scope，监督worker
  进入control子组，后端在exec前进入payload子组；所有后端子孙继承。
  外层GPUguard/controller留在受限payload之外，保留同一flock到回收。
  scope父OOMPolicy=continue，payload组OOM整体终止；control独立有界。
- 只写本次scope内的控制文件；不改父slice/其他服务，不安装开机启动。
  校验UID、scope token/InvocationID、cgroup inode、祖先与实际进程归属，
  限额写入后回读；无法建立或确认保护时在模型exec前拒绝。
- 已有OOM/oom_kill/pids.max事件或保护观测失败是终止原因，不自动重启；
  允许正常memory.high回收并记录，达到memory.max但回收成功不伪称OOM。
  退出后保留计量及事件，确认payload无进程再移除其目录；父controller
  另核对scope退场。不能用目录消失或主PID退出单独证明GPU清理。
- host数据只代表内核记账域，历史缓存计费/初始helper分配/未计费CUDA
  不能从此推导物理RAM总额，也不把CUDA quota与memcg简单相加。
  这是可信本地程序的资源约束，不是对同UID恶意cgroup迁移的沙箱。

## 有界验收

先冻结代码后成组CPU与真实内核探针：普通生命周期、64MiB级memcg
OOM、8任务pids拒绝、限额漂移/身份失效、禁止重启与旧包兼容。
探针只使用新建自有域；在确认内核上限生效后才触页或fork，不制造
整机OOM，不加载模型、不接HTTP。每探针最多128MiB申请、16子进程、
30秒，上限6组；原始失败保留，只修正受影响项。

之后短业务验收至多1模型/1TLS网关、4成功生成/2真实资料工具；
候选明确host high16GiB/max20GiB、payload pids512，control max512MiB/
pids64，swap0。最多480秒、GPU420秒、64控制调用，同一guard租约。
核对实际配置/成员、峰值/事件、答案/预算、清理、GPU/原锁与SOAR mask。
不在生产模型上故意制造host OOM，不执行day或替代用户长稳。
候选限额只是本机短验配置，正式部署策略仍需按实际业务和物理余量选定。

## 依据

本机systemd255与cgroupv2已委派memory/pids。设计遵守委派目录的单一
写入者约束，不直接创建systemd管理层级的子组。高水位回收、最大额度、
OOM分组及线程计数含义以官方接口为准：
[systemd255资源控制](https://www.freedesktop.org/software/systemd/man/255/systemd.resource-control.html)、
[scope/OOMPolicy](https://www.freedesktop.org/software/systemd/man/255/systemd.scope.html)、
[委派约定](https://systemd.io/CGROUP_DELEGATION/)、
[内核cgroupv2](https://docs.kernel.org/admin-guide/cgroup-v2.html)。

完整接受仍需实现、真实内核执行和短业务原件；冻结文档本身不是通过。
