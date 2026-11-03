# SRV-10c实际默认交付

默认ELF：`7619966ef2a5101e9a4bbc56d20805d980f85324e0313a71c248c701df6aa501`。
sidecar：`a2f9cb8feb2fab94917b4d4ad1b22020efe42c2416b87306873d15d26b71a263`。
protocol：`63b29f137b9c7057201de50c3a711930e9d76431f443ea29a083d9b429fced25`。
运行来源222项，业务接受见[完整记录](SERVE_CLIENT_RESULT_2026-11-04.md)。

首个相对guard argv被既有祖先校验拒绝，未进入begin/替换阶段，
原`.q4t-work/serve-clients-20261104/delivery/lease/`保持COMMAND_FAILED。
430b29cc新增绝对入口，绑定原固定交付包和失败记录，新输出
`delivery-absolute/`。同一已验ELF安装、CLI健康、精确ELF HTTP证据
复用通过，guard COMPLETED、finalized DELIVERED，恢复marker已清除。
不增加模型/HTTP，未执行小时/day，不安装常驻服务或开放内网。

回退包在`.q4t-work/serve-clients-20261104/delivery-package/previous/`，
原a479d8ab/9ed141ad与旧构建身份已核对。需要回退时使用独立新租约，
入口只接受当前旧/新已知pair，拒绝覆盖未知文件；两个文件分别原子
替换，仍不是跨文件掉电原子事务。以下仅为操作示例，本次没有回退：

```bash
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-clients-20261104/operator-rollback-001/lease \
  -- /usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/serve_clients/closeout.py \
  rollback --output /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-clients-20261104/operator-rollback-001
```

GPU计算上下文空、原66305/3294067锁可用，9个业务自有进程均退出，
SOAR恢复unit仍持久masked/inactive，容器空。旧两份day身份保持，
均未执行；它们仍固定旧ELF，不能用于宣称新默认长稳。新默认托管
包/长验交接继续待办，长运行由用户专门开展。

交付42原件与3本机ELF身份独立封存，压缩包171981字节，离线核对
通过。[交付证据](evidence/serve-clients-delivery-20261104/manifest.json)
绑定原拒绝、固定包、pair替换、健康、guard及后置GPU/锁。复核命令：

```bash
python3 -B tools/serve_clients/report.py verify
python3 -B tools/serve_clients/closeout.py verify
```

以上均离线读取，不启动模型/服务。policy仍需显式启动选项；实际
LAN域名/CA/来源限制、独立Key分发和新托管组合验收没有由本次默认
替换自动完成。单请求预算/在途数不等于物理RAM隔离或长期公平保证。
