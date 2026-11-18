# 注册音色实际合入主线

[PR #49](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/49)已在
2026-11-19 04:30（Asia/Shanghai）合入main：
`42a428466e258c6a8698fea14418ecf962ae66a6`。
精确头`0febc4f58e5b6ad70f163a3aa5a0ab2acdad7ac9`的push和PR两项
Public host contracts均SUCCESS；使用match-head-commit后实际merge，
不是只关闭PR。包含rev326注册音色、持久恢复、Base-only消费与CI
会话故障夹具修复，复用各自既有针对验收。

原PR CI失败保留在会话恢复夹具证据。本次不新增GPU、不更换默认。
ICL编码器仍在本地后继阶段，未混入PR49。原工作分支存在独有ICL
提交和未提交开发以及证据路径，因此保留并继续复用，不自动清理。
Serve总目标继续active，其他必做任务没有因本PR合入被标记完成。
