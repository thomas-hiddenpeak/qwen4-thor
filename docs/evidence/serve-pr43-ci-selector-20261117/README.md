# PR43 CI入口名称同步

最终head `b548eb5619cd44bfc4dbc9666c31f2ca6a5b587a` 的
[push检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/44018810100)
失败于unittest选择器：v20实现将包迁移用例命名为current，但工作流
仍引用v19旧名称。该步骤23项已执行通过，1项加载失败，未掩盖为通过。
原始CI日志保留。本改动仅同步工作流选择器，没有改生产代码或用例断言。

本机精确1项通过，0.455秒、0模型动作；[结果](result.json)及
[输出](targeted-test.txt)保留。继续最终head两组CI与实际合main，
不因本地入口验证通过而宣称PR已合并。ASR开发留在后续提交中。
