# 当前main复审原始证据

锚点802bd70，父对照c26e97a。本目录37份文件逐字节复制自
`.q4t-work/main-reaudit-20261031/`，来源、SHA256和长度见files.json。
本轮只进行CPU审计，未修改生产源码、默认程序或模型文件。

| 文件 | 用途 |
|---|---|
| plan.json / result.json | 冻结范围、169项源码身份和四项未修遗漏的结论 |
| source-identity.json | 当前及父提交的编译/监督源码身份 |
| delivery-recheck.json | 默认、工具封条、源码与只读回退检查 |
| io-*.log / io-commands.json | 新输出目录下零警告构建和24项IO/驻留合同 |
| prepare_probes.py | 合成夹具的生成步骤；保留原执行路径，勿覆写旧输出 |
| residency_probe.cpp / CMakeLists.txt | 可重新构建的CPU探针，链接生产源码，CUDA由CPU替身实现 |
| probe-note.json / probe-build-commands.json | 探针保护区、返回码语义与实际构建命令 |
| current-*.log / parent-*.log | 两版源码的构建与四种缺陷观测完整输出 |
| residency-observations.json | 8次进程执行的命令、stdout/stderr和退出码 |
| supervisor_probe.py / parent-release.py | 真实自有CPU后代的生产监督循环夹具及父版脚本 |
| supervisor-observations.json | 两版监督器返回后残留后代的观测及最终回收证明 |
| supervisor-current/ / supervisor-parent/ | 受控配置、真实PID/进程组启动和stopped原始记录 |

驻留探针给host分配额外增加64字节哨兵保护区，观察越过请求边界的
写入而不破坏进程堆；权重读取/槽位计划/线程入口使用原生产源码。
worker异常探针以SIGABRT退出，是被观察的缺陷；没有将“测试进程
结束”解释为正常资源回收。监督器夹具只修改启动命令、配置验证和
停止事件，真实session/线程/信号/等待代码均执行；探针随后回收
了自己创建的所有进程。

在本机仓库根目录可使用**新**构建目录重放驻留CPU观测：

```bash
cmake -S docs/evidence/main-reaudit-20261031 \
  -B .q4t-work/main-reaudit-replay-01 \
  -DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_BUILD_TYPE=Release
cmake --build .q4t-work/main-reaudit-replay-01 --parallel 4
./.q4t-work/main-reaudit-replay-01/residency_probe audit_short_weight_observation
./.q4t-work/main-reaudit-replay-01/residency_probe audit_oversized_scalar_observation
./.q4t-work/main-reaudit-replay-01/residency_probe audit_hot_duplicates_observation
```

当前缺陷存在时这些观测输出OBSERVED/PASS；它们不是期待正确行为
的验收测试。worker异常需要独立进程并禁用core dump，原退出码为
-6。原supervisor_probe.py的固定OUT保存本次现场，重复运行前应在
脚本副本中改用新的OUT，不能覆盖上述原始目录。

原GPU、HTTP和发布验收身份不变，见[审计报告](../../MAIN_REAUDIT_2026-10-31.md)。
