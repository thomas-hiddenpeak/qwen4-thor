# 可选部署组合检查

`profile.py`把已准备的后端、可选TLS网关、回退组合固定到独立JSON。
`prepare`和`check`只读包、模型JSON及证书，调用既有验证器；不启动
模型、HTTP、GPU、unit或通知。高级能力按实际需要选择；业务接受须
另查绑定相同manifest的阶段记录。SHA用于身份核对，不是签名授权。

```bash
python3 -B tools/serve_deploy/profile.py prepare \
  --backend BACKEND_PACKAGE --backend-sha256 BACKEND_MANIFEST_SHA256 \
  --gateway GATEWAY_PACKAGE --gateway-sha256 GATEWAY_MANIFEST_SHA256 \
  --rollback-backend OLD_BACKEND --rollback-backend-sha256 OLD_BACKEND_SHA256 \
  --rollback-gateway OLD_GATEWAY --rollback-gateway-sha256 OLD_GATEWAY_SHA256 \
  --output .q4t-work/deployment/profile-001
python3 -B tools/serve_deploy/profile.py check \
  --profile .q4t-work/deployment/profile-001/profile.json --sha256 PREPARE返回摘要
```

不启用TLS时省略对应gateway参数对；后端仍只监听loopback。使用TLS
要求显式CA，核对业务/运维端口、内部客户端策略及冲突；API Key、私钥
不复制到profile或输出。包和profile保持本机私有权限，不发布完整
gateway配置。验证器来源或解释器变化时显式重新审核/准备，不以当前
工作区动态解释旧接受；包运行入口始终使用包内快照。

## 启用、停止及回退

1. 启用前核对profile和对应接受记录。实际LAN另确认真实监听地址、
   来源CIDR、DNS/证书/CA、从第二台客户端访问与绕过入口拒绝；只开放
   指定TLS入口。通知接收端在启用时验证。loopback短验不替代这一步。
2. 用[后端run入口](../serve_service/README.md)和profile中后端的绝对
   包路径、manifest，指定全新运行目录；确认READY后，再用[托管网关](
   ../serve_gateway/MANAGED.md)绑定该后端代次运行。前台显式启动，不
   安装自启。GPU验证继续通过生产入口持有共享租约，不嵌套第二把锁。
3. 查看后端status/inspect、网关status及[运维入口](../../docs/SERVE_OPERATIONS_2026-11-03.md)。
   `STOP_REQUESTED`仅表示意图，须核对终态、实际监听、GPU和flock。
   内存保护FAILED与清理成功分别保留，不能当正常业务成功或自动重试。
4. 升级或回退先停止网关，再停止后端；确认实际清理和共享锁释放后，
   才使用profile里对应组合和全新目录启动。禁止覆盖包内ELF、复活旧
   运行目录、重放未确定结果或按名称停止无关服务。不可用时保留现场。
5. 应用任务的计划内备份/恢复使用[持久任务入口](../project_tasks/README.md)
   及[已验边界](../../docs/SERVE_TASK_BACKUP_RESULT_2026-11-04.md)。旧源端
   封存/单活和显式迁移约束保持；源盘丢失灾备未包含。

## 资源范围及短验

启动容量检查、进程CUDA申请账、host cgroup和全机MemAvailable属于
不同计量域，禁止相加当物理总占用。profile只展示选用策略，不声称
完整物理RAM硬上限；生产低水位与本批30GiB试验门限分别说明。
长稳由用户执行，旧day计划不能只改摘要套用新包。

当前固定组合与预算见[有界交付计划](../../docs/SERVE_BOUNDED_DELIVERY_2026-11-05.md)。
`stage.py cpu/prepare/check/run`是该批短验驱动，不是通用部署命令。
`run`经正常包内CLI取得原GPU锁，并保管同一flock到保护退场/清理完成；
原批最多一次模型启动；授权补验另一次，两个预算现均关闭。
[最终结果](../../docs/SERVE_BOUNDED_DEPLOYMENT_RESULT_2026-11-05.md)：4完整
响应/2工具通过，内存压力先达到安全余量，退场组合NO_GO。候选包
c1653470没有接受收据，继续使用已接受41dc8fdb及原回退包。
`report.py verify`离线核对封存原件；返回passed只表示记录一致，业务
状态仍为NO_GO，不是部署批准。不得复用旧run目录或自行扩展压力。
