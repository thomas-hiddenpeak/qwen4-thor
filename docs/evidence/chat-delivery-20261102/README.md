# 聊天接入安装交付证据

2026-11-02，`PASS_DELIVERED_CHAT_INTEGRATION`。
最终默认830694ce、sidecar 0f28e0f0。业务验收证据沿用相邻的
chat-business-acceptance-20261102及chat-business-runtime-20261102。

`sealed-files.json`绑定98份原始文件；本README不在封条内。
摘要、范围与精确回退命令见[交付记录](../../CHAT_DELIVERY_2026-11-02.md)。

- `deployment/`：两head健康的6次生成、2次能力GET、客户端日志、
  服务器资源清理、完整租约与最终DELIVERED收据。
- `package/`：原封包封条、最初sidecar及原始冻结合同、旧默认身份、
  候选构建来源/运行时清单、执行工具及最初7项CPU故障合同。
  原合同误写messages默认解析false，作为原始记录保留，不是当前接口。
- `metadata-correction/`：经真实capabilities核对的修正sidecar、
  对应工具、9项回退合同及TTFT说明调整后2项定向重验记录。
- `metadata-deployment/`：只改sidecar的独立租约、替换前后摘要；
  没有新增模型启动或生成请求。
- `installed/`、`final-audit.json`、`final-state.json`：实际默认元数据、
  源码/封条/业务答案/资源及最终GPU释放核验。

完整二进制、335MB subset head与两个增量Git bundle保留于本地
`.q4t-work/chat-delivery-20261102/`，不重复提交到Git；封条保留摘要。
原包bundle依赖55299a0，修正包bundle依赖426f0dd，校验输出入库。
原包不修改；当前回退入口同时识别修正前后sidecar及已知部分pair。
私有vLLM恢复配置不在本证据集内。

安装健康覆盖实际4槽配置下的单请求工具与推理往返，不能扩张为
四路并发、缓存收益、性能非劣或通用SDK适配结论。两个guard的轮询
记录不能证明采样间隔内绝无不可见干扰。
