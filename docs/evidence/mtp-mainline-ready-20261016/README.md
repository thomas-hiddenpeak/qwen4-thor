# 2026-10-16 MTP 归并候选证据

结论见[报告](../../MTP_MAINLINE_READY_2026-10-16.md)：指定五档目标通过，
5项GPU诊断仍FAIL；不是全量测试通过或main合入批准。

- `summary.json`：同binary五档各3次均+50%～+200%，两侧检索11/11、尾部通过。
- `freeze.json` / `commit-map.json`：冻结时未提交合并到fe44c56的身份映射。
- `final-identity.json`：生产binary不变，后续仅修改诊断测试的身份及失败结果。
- `off/`、`on/`：结果、输出摘要与逐次生成文本、运行参数及退出状态。
- `quality-off/`、`quality-on/`：11题逐题计数、期望、回答及结果。
- `boundaries/`：6项实际HTTP请求/响应、取消SSE与服务退出0。
- `direct/`：直接合同、初始合并失败、归并前失败、夹具修正前后记录。
  `sync-diagnostic.log`测试仅新增完成等待；最终代码仅更改该等待的注释/错误文案。
- `public-host.log`、`test-gate.log`、`host-audit.log`、`ci-pass.log`：公共门禁。
- `raw-manifest.json`：本机原始证据文件路径、SHA256与大小。
  大型请求、DB、服务器日志、失败与消融均留原路径；索引不替代原始文件。

运行程序SHA256：
`26639190711e0a2179e3695e33f10ad5fd055578b3f6d38adcc7f955df6557b7`。

`run.py`、`boundaries.py`、`analyze.py`为实际采集/检查脚本副本，包含本机原路径，
复验应复制到新的`.q4t-work/`目录并更改输出目录，禁止覆盖既有失败证据。
公共CI不含GPU、真实模型或五档性能，不能用CI绿灯取代这些检查。
