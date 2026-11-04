# SRV-02k：host资源边界与真实工具短验原始记录

385条记录/源码快照，2个本机ELF的路径、字节数和SHA256单独登记。
`records.tar.gz`为504329字节，SHA256：
`22a44409eea5ad5ec86bfea01490f1de48d69578a9d788dc5207a46077e96c20`。

范围含155 CPU、原high超时与1定向修正、六类内核结果、2驱动CPU、
真实1模型1TLS网关/4生成2工具、任务SQLite、cgroup计量及成员、
GPU租约、完整清理与固定包身份。失败原件不改写。密钥/私钥及两个
ELF本体不在归档；不能据memcg统计证明Thor全部物理RAM硬保护。

从项目根目录离线重建，无新模型、GPU或HTTP：

```bash
python3 -B tools/serve_service/resource_report.py verify
```

逐文件摘要在`manifest.json`，重建结果在`result.json`。
业务说明见[限定接受报告](../../SERVE_HOST_ENVELOPE_RESULT_2026-11-04.md)。
