# 2026-10-31项目审计证据

对应[审计报告](../../PROJECT_AUDIT_2026-10-31.md)。原始执行目录为
`.q4t-work/audit-20261031/`，这里封存小型文本证据和CPU复现源码；
二进制、构建缓存、合成分片和完整临时目录继续保留在原路径。
`evidence-manifest.json`列出本目录其余文件的SHA256。

## 证据索引

| 文件 | 用途 |
|---|---|
| plan.json / source-identity.json | 本轮范围与审计前源码锚点；文件清单不等于覆盖率 |
| environment.json | 本机编译器、CUDA编译器与两种Python能力 |
| validation-summary.json | 各测试组、原失败与定向重验的分开记录 |
| full-*.log / host-*.log | 全工程构建、公用host构建与初始CTest |
| release-host/ | 72项必需host注册、执行和判定原始记录 |
| offline/ | 15组145项离线检查；checked-client初始前提失败保留 |
| checked-client-system.log | 提供native worker后相关6项定向通过 |
| guard-system-python.log | 支持pidfd的系统Python下27项通过 |
| io-build.log / split-io-results.json | 独立入口链接失败及拆分编译执行命令 |
| *-store.log / prefix-seed.log | CPU存储字节、O_DIRECT与失败路径合同 |
| io-probe.log / io_probe.cpp | 异常extent、范围溢出、fd泄漏、头部溢出 |
| residency-probe.log / residency_probe.cpp | 真实驻留代码跨分片误读与部分初始化泄漏；CUDA均为CPU替身 |
| capacity-probe.log / capacity_probe.cpp | 参数和预算接受无法容纳完整decode批次的配置 |
| watchdog-probe.log / watchdog_probe.py | 实际supervise配模拟时钟/进程/HTTP；1261秒强杀记录 |
| expert-shard-layout.json | 当前只读索引24,576组专家均同分片；未校验全量权重字节 |
| installed-release.json / published-runtime-source.json | 审计时默认发布元数据与169源码清单 |
| delivery-identity.json / runtime-identity.json | 默认声明依赖、源码和前版回退身份核对 |
| documentation-drift.txt | 文档更正前的矛盾摘录；原历史日志未改 |
| replay/ | 新增重放入口的一次实际编译/执行日志、命令与生产源码身份 |
| final-doc-checks.json | 本地链接、JSON/Python语法、历史日志保留与169源码身份检查 |

## 重放缺陷探针

以下为CPU诊断，不启动模型或真实HTTP，不链接CUDA运行时库。
需要本机g++14与CUDA头文件。使用全新输出目录，避免覆盖本次证据：

```sh
/usr/bin/python3 -B docs/evidence/project-audit-20261031/reproduce.py \
  --output .q4t-work/project-audit-replay-new
```

重放脚本编译当前工作区的生产源码，会记录实际源码摘要与命令。
在修复后的源码上运行，输出可以与本次锚点不同。探针退出0表示观察
顺利完成，**不表示缺陷已修复或合同通过**；判据如下：

- IO：本次非法extent仍成功读ABCD；wrap读取命中文件头；12次错误
  Open泄漏12fd；极端header抛length_error。正确修复应拒绝非法数据，
  且失败前后fd/map没有增加。
- 驻留：本次第二次分配失败后Free仍残留1分配；跨分片gate/up读取
  161/162，独立期望为34/51。正常Free对照剩余0。
- 容量：本次options_accepted=1、budget_feasible=1，max_seq=2却只有
  1行forward容量。修复后应在加载前拒绝或得到一致的有效配置。
- 监督：本次健康200、未结束请求在时钟1261触发backend.kill。
  此模型化输入反映当前生产者的计数更新语义，不代替真实长请求验收。

reproduce.py是在封存时补充的重放入口，随后在全新目录
`.q4t-work/project-audit-replay-20261031/`实际执行一次，四个观察均与
原结果一致；日志另存replay/，没有覆盖原记录。仅验证新增CPU重放
工具，没有重复真实推理或重新执行已通过的完整测试矩阵。
