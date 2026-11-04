# 准备阶段锁等待取消验收

范围见[SRV-03c](../../docs/SERVE_PREPARE_LOCK_2026-11-04.md)。
`baseline.cpp`复现旧阻塞锁，退出0表示缺陷被观察，不是修复通过。
`probe.cpp`链接真实Chat/preflight/HTTP和生产清理边界；socketpair、
持锁owner和未加载server验证23组合。无模型加载，不代表真实生成；
不抢占持锁owner，不触及模型计算。handler、body、registry和客户端
lease有实际检查；缓存lease析构沿用既有合同，本探针未创建缓存快照。

CPU组验冻结在`.q4t-work/serve-prepare-lock-20261104`。两次工具失败
保留：首次探针缺生产链接，随后清单遗漏CMake锚点。修正后零警告构建，
`cpu-finish`只重验清单并绑定其余未变CPU证据；`driver-03`只验证
7个不完整/失败探针证据拒绝，不替代实际handler或业务通过。

提交后用`stage.py prepare`冻结来源、完整默认回退依赖与本机临时
TLS测试配置；高级功能不自动变成部署要求。`stage.py execute`只能
从共享guard内执行，整组最多300秒（含45秒清理）、1模型、6生成、
100控制；候选通过后按既有交付事务替换默认pair，安装健康复用同ELF
的HTTP证据并核对CLI，`finalize`要求guard完成。失败不得覆盖目录或
自动重跑，旧包/Key/day不改，新任务协议不自动登记兼容。

```bash
python3 -B tools/serve_prepare_lock/stage.py prepare
python3 -B "$PWD/tools/evalscope/gpu_exclusive.py" \
  --output "$PWD/.q4t-work/serve-prepare-lock-20261104/execution/lease" -- \
  python3 -B "$PWD/tools/serve_prepare_lock/stage.py" execute
python3 -B tools/serve_prepare_lock/stage.py finalize
```

实际guard核对祖先命令的脚本绝对路径及输出目录。不执行用户day计划。
