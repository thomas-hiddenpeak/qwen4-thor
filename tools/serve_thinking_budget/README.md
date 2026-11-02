# SRV-05b 真实 thinking token 预算验收

业务正确性阶段；不接受性能优化结论。开发/计量合同见
[合同](../../docs/SERVE_THINKING_BUDGET_2026-11-03.md)。无权重 CPU gate
与本机真实 HTTP 分开记录，不把文字中出现闭合标记当作 token 控制证据。

`verify.py` 固定 19 次生成（含一次严格长度错误及安装路径健康）、
12 次输入拒绝、3 次启动。总上限 22 次生成、4 次启动、首次 launch
起 1800 秒，预留只供具体失败修复，禁止自动重试或重置时间。
完整驱动持有同一 GPU 锁，loopback、seq1、完整 head、MTP k3；
MTP/缓存阶段 max_len16384，其余8192，prefill2048。

覆盖：缺省/null对照、预算0/1/16/128、隐藏与SSE、自然闭合、关闭
thinking的前缀、严格工具真实读取与推理历史回填、schema、总长度终态、
实际MTP批数与回退、私有SSD快照命中及fresh回答比较。缓存用例只证明
冻结业务回答与真实restore，不声明全张量数值等价。所有usage与日志、
metrics交叉核对；旧120模板差分仅在渲染/历史函数/分词器身份相同后复用。

先完成CPU记录并提交源码，再 `stage.py prepare`。随后仅以绝对路径经
`tools/evalscope/gpu_exclusive.py --output .../execution/lease -- .../sdk-env/bin/python
-B tools/serve_thinking_budget/stage.py execute` 完成HTTP、配对安装、安装
路径健康。guard COMPLETED 后 `finalize`，保留旧ELF/sidecar与source.bundle。
失败保存全部原始结果；不干预其他服务、不仅凭driver退出码宣布交付。
