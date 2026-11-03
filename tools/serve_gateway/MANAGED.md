# 托管TLS网关

用于已确认的受控内网、多客户端独立API Key入口。
[本机短验结果](../../docs/SERVE_GATEWAY_MANAGED_RESULT_2026-11-04.md)
覆盖正常排空、manager死亡、nginx master崩溃和后端退场。
实际监听地址、CA/DNS、来源限制及远端客户端还需部署验收。
长时间运行由用户专门开展，短验工具不会自动启动day计划。

可选SRV-10c策略接线尚在验证，见[客户端策略合同](../../docs/SERVE_CLIENT_ISOLATION_2026-11-04.md)。
新prepare支持`--client-policy FILE`，须与模型托管包使用相同字节。
新托管网关包version2固定策略解析器源码；旧version1运行入口保持。
本页已有短验结果不自动覆盖这项新能力。

## 准备固定包

按[基础配置说明](README.md)准备私有policy、匹配域名的有效证书、
私钥及固定nginx。选择新的包路径：

```bash
python3 -B tools/serve_gateway/managed.py prepare \
  --policy .q4t-work/gateway-policy-private.json \
  --nginx .q4t-work/serve-gateway-20261103/vendor/root/usr/sbin/nginx \
  --certificate .q4t-work/certs/server.pem \
  --key .q4t-work/certs/server-key.pem \
  --output .q4t-work/managed-gateway/package-001
```

prepare只执行配置检查，不监听或加载模型。包复制nginx、配置、证书/
私钥和源码，返回manifest摘要；保存该摘要为身份锚点。包与运行目录
均为当前用户私有；包含Key的policy/nginx.conf、私钥和私有syntax
日志禁止发布。测试证书不能替代实际部署的信任链。

运行使用包内源码入口和prepare时记录的Python，避免工作区修改改变
既有包。下面的`MANIFEST_SHA256`应替换为prepare返回的完整摘要；
Python路径也应与manifest的`python.path`保持一致。

## 显式启动与停止

先按[模型托管说明](../serve_service/README.md)启动已接受后端包。
后端必须READY、固定ELF/同namespace/loopback，网关绑定这一代实例：

```bash
/usr/bin/python3 -I -B \
  .q4t-work/managed-gateway/package-001/source/tools/serve_gateway/managed.py run \
  --package .q4t-work/managed-gateway/package-001 \
  --manifest-sha256 MANIFEST_SHA256 \
  --backend-run .q4t-work/managed/run-001 \
  --output .q4t-work/managed-gateway/run-001
```

这是前台进程；每次启动使用新运行目录，既有目录不会覆盖。
同一个包只允许一个活跃网关，nginx继承包锁；该锁不是GPU锁。
模型持有自己的生产GPU租约。GPU开发验证仍需按AGENTS约定覆盖完整
阶段，不能仅启动网关就认为已取得GPU独占。

在另一终端使用相同包内入口查看或停止：

```bash
/usr/bin/python3 -I -B \
  .q4t-work/managed-gateway/package-001/source/tools/serve_gateway/managed.py status \
  --run .q4t-work/managed-gateway/run-001
/usr/bin/python3 -I -B \
  .q4t-work/managed-gateway/package-001/source/tools/serve_gateway/managed.py stop \
  --run .q4t-work/managed-gateway/run-001
```

`STOP_REQUESTED`只确认已记录意图/发送通知；继续查看status，直到
STOPPED且cleanup_confirmed=true。正常stop给nginx SIGQUIT，最多
35秒排空；超过上限记失败并TERM/KILL有界回收。客户端必须按原协议
识别完整终态；中断流不能当成功，也不能自动重做工具。

状态和清理分别判断：

| 状态 | 含义与处理 |
|---|---|
| READY | 本轮最近一次监督核对通过；不代表长期或实际远端可用性已验收 |
| STOPPED / cleanup=true | 正常停止完成；保留终态和日志 |
| FAILED / cleanup=true | 本轮失败已锁存、自有前端已回收；查看error，修复后显式启动新代次 |
| OWNER_ABSENT / cleanup=false | manager失联，历史状态不能证明当前清理；只读核对进程/监听/锁，不按遗留PID文件批量kill或删除 |

网关每秒核对绑定后端及前端身份；后端停止、替换或观察不可信会
关闭本轮网关。它不会重启自己、重放请求或停止模型，也不会自动
绑定到新后端。nginx内部worker管理仍由nginx负责。新后端准备好后
由操作者显式启动新代次；异常遗留nginx.pid时准备新包，保留旧现场。

## systemd与日志

`unit`与`run`使用相同参数，仅输出模板，不安装或启动服务：

```bash
/usr/bin/python3 -I -B \
  .q4t-work/managed-gateway/package-001/source/tools/serve_gateway/managed.py unit \
  --package .q4t-work/managed-gateway/package-001 \
  --manifest-sha256 MANIFEST_SHA256 \
  --backend-run .q4t-work/managed/run-001 \
  --output .q4t-work/managed-gateway/run-001
```

模板固定包内入口、Restart=no、KillMode=mixed、60秒停止窗、UMask0077，
无Install段/timer。常驻安装需配合实际部署参数，不从生成模板推导
为已上线或允许自动恢复。停止网关不释放仍在运行的模型GPU；停模型
应走其自己的正式stop。包路径不可随意搬移。

每代次stdout/stderr合计保留2×4MiB，事件2×256KiB，共8.5MiB，
完整status/terminal JSON另计。轮转可能切断一条日志；日志写失败会
停本轮网关。该上限不包含跨代次目录总量、systemd journal或外部
归档；跨代次保留和告警接收应由部署策略明确。

证书有效期在准备/启动检查，运行期核对内容身份，不提供自动续期。
换Key/证书：先准备新私有包，正常停止并确认旧网关清理，再显式
运行新包；没有零停机轮换或自动撤销在途请求。对外路由、错误封套、
独立控制额度和请求ID行为仍见[基础合同](README.md)。

只读复核本批历史证据：

```bash
python3 -B tools/serve_gateway/managed_report.py verify-archive
```
