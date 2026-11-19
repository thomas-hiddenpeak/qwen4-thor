# Prompt剩余场景与固定条件诊断

1模型、5 Realtime、3 Chat、2WSS、1注册HTTP，53.511秒；
52次NVML查询正常，严格守卫COMPLETED，自有模型正常退出、端口和锁
释放，默认rev356不变。原失败对象库仅复制使用，原证据逐一SHA保持。

五个未完成场景通过：实际delta后取消、新请求返回56、跨客户文件拒绝、
真实CSV内部模型步骤加主回答仍只占一次RPM、下一请求拒绝。跨客户模板
更新也拒绝；无重放、无历史污染，固定v1会话仍保持。alpha结算362 tokens/
2RPM，beta结算1273 tokens/1RPM，所有预留与会话资源清零；诊断客户3次
Chat也按真实响应完整结算。SDK实际只有1次HTTP，预记16不是实际计数。

完全相同的原305-token提示，两条预冻结诊断结果：

| 设置 | 实际回答 | 质量 |
|---|---|---|
| temperature=1, seed=0 | rev356 | PASS |
| temperature=0, seed=0 | The tool result does not contain a version field. | FAIL |

两者HTTP/用量协议通过；不更改公开默认值、不进行额外采样。这不足以
定位唯一质量根因。原随机回答仍FAIL，原整组也仍FAIL；本组通过只表示
剩余协议、隔离、取消和结算通过，不把工具语义答案追认为正确。原实际
工具来源与模板输入未丢失的CPU证据见[输入复核](../serve-realtime-prompt-business-review-20261120/README.md)。

接下来补能力声明和主轻/恢复组合、可回退交付；当前尚非Prompt发行资格。
继续全部master，不重复已通过文件场景，不做完整性能/模型质量矩阵。
