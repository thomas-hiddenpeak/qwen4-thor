# 阶段4：新运行时整模型验收

运行时SHA256 `be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072`。
结论见[数值收口报告](../../../MTP_NUMERICAL_CLOSURE_2026-10-16.md)。

- `summary.json`：五档off/on各3次增益85.2%/98.4%/106.1%/100.8%/93.5%，
  目标全部通过；十组文本同修复前；质量两侧各11/11，尾部/取消恢复通过。
- `off/`、`on/`：每次吞吐/TTFT、确定性摘要、生成文本、参数及退出状态。
- `quality-off/`、`quality-on/`：逐题期望、结果及长度；提示哈希与旧测量相同。
  DB schema不保存HTTP状态码，null明确保留，success均为1；不推造200。
- `boundaries/`：六项尾部/容量请求的实际HTTP响应、流式取消及后继恢复；
  `exit.json`确认passed及server_returncode=0。
- `freeze.json`、`final-identity.json`：运行时与测试二进制归属；阶段3通过后
  仅变更注释/诊断打印/格式，最终测试构建零警告，不重复完整推理。
- `raw-manifest.json`：原目录`.q4t-work/mtp-numerical-closure-20261016/http`
  的完整文件大小/SHA256清单；大型请求、DB和服务日志留本机原路径。
- `archive-schema-note.json`：首次归档检查的字段假设失败及核对schema后的
  修正。它不是运行时失败，未覆盖或重跑模型结果。

runner的`performance_acceptance=false`表示其自身不作跨off/on性能裁决；
本次目标由`analyze.py`计算同输入均值比、`summary.json`记录target_pass，
`archive.py`核对五档、两侧质量、退出状态及二进制身份后归档。

`run.py`、`boundaries.py`是实际采集脚本，复验需换新的工作目录，禁止覆盖
旧证据。`remaining.py`绑定本次已结束的PID，仅作过程记录，不可直接复用。
使用`--allow-unqualified-binary`进行候选验收，不代表部署发布资格。
PLE四项与完整256步复用phase02；五项局部严格合同复用phase03。公共CI不
代替GPU和模型证据，最终提交的CI状态以PR #4检查为准。
