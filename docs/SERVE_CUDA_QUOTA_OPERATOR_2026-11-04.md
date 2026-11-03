# CUDA额度版本的用户长验交接

当前默认a479d8ab已通过本阶段直接serve短验。新固定包单独放在
`.q4t-work/serve-cuda-quota-integration-20261104/operator-package`，
计划在同阶段`operator-soak/day/plan.json`，旧day目录保持。只准备
与离线核对，不自动启动模型、监听或临时服务。

旧11项soak CPU及不变的控制器实现证据复用；新包未在托管模式下
执行过，尚无新包托管E2E或长稳结论。下面的用户执行会同时验证
新包的托管启动/关闭与24小时业务闭环；失败保留，不自动重试。

在项目根目录检查身份：

```bash
python3 -B tools/serve_cuda_quota_integration/handoff.py check
```

由用户选择GPU窗口后手动运行：

```bash
python3 -B tools/serve_cuda_quota_integration/handoff.py run --operator-run-day
```

全程由既有控制器持有同一GPU租约，单次启动、零自动重启；5760周期/
28800请求/5760真实资料工具执行。第一小时240周期/1200请求保留
检查点；保留原资源增长合同与总窗口限制。新package端口18126，
只监听127.0.0.1，不代替LAN/TLS部署。执行前默认pair或固定来源变化
会拒绝，不能悄悄换版本继续旧计划。

Ctrl-C保留中断证据并清理自有临时服务。终端丢失时：

```bash
/usr/bin/python3 -I -B tools/serve_service/service.py stop \
  --run .q4t-work/serve-cuda-quota-integration-20261104/operator-soak/day/execution/managed-run
/usr/bin/python3 -I -B tools/serve_service/service.py inspect \
  --run .q4t-work/serve-cuda-quota-integration-20261104/operator-soak/day/execution/managed-run
```

完成后只读核对，不重新运行：

```bash
python3 -B tools/serve_cuda_quota_integration/handoff.py report
```

完整响应、引用、资源观测和guard记录留在新execution。不能只凭
progress或单个PASS文件判断通过；物理RAM硬保护不由本次长验自动
成立。此交接不恢复vLLM，不修改旧day或其历史接受范围。
