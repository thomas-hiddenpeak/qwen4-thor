# 原生项目资料检索：真实业务验收

`ACCEPTED_ORIGINAL_BUSINESS_AFTER_ORACLE_FIX`。候选引擎
`f9ed346bd597e2237e1d6e9ab716674c2b3de6f3257e5d23ac68de61a2cb44c2`，
执行源码 `d088a0b9`；1次主模型启动、6次带模型HTTP请求、125次控制
观察，驱动85.158秒。严格NVML租约连续持有；进程退出0，端口释放，
未干预其他服务。默认rev80及发布元数据保持，尚待可回退交付。

实际上传本项目SSD卸载合同和验收规范的冻结段落，以原生embedding
建立持久索引。直接搜索、官方JS 7.47.0 Responses JSON/SSE、严格
JSON Schema、真实file_search事件/文件引用、后台执行、原ID检索和
历史续答全部通过；另一客户端404且未消费模型token，普通Chat返回42。
JSON答案正确说明仅SSD容量卸载及CPU/GPU共享DRAM；schema答案正确
引用非性能改动只做受影响行为的验证。include投影、共享RPM/TPM、
实际源文本与引用字符位置均核验。最终640执行步、7803输入token、
467输出token，活动请求和后台/入库任务归零。

原 `result.json` 为FAILED、`lease/exit.json` 为COMMAND_FAILED：最终
验收脚本错误地要求load_attempts=1；实际代码先加载tokenizer元数据，
再加载权重，两次均计入attempts。原后端ready、load_failures=0，权重
1191553024字节只上传一次。这不是NVML中断或模型业务失败。原失败、
原驱动、全体原始HTTP/SDK/指标与租约记录完整保留，没有改写为PASS。

仅修正该判据，1项针对性CPU含失败反例通过；随后离线复核冻结源码、
引擎、模型资产、原请求/响应、计量及清理，生成独立
`accepted-original-business.json`。只允许两份验收脚本身份变化，运行时
源码未变；新增GPU启动0、重复模型请求0。原始完整私有对象保留在
`.q4t-work/serve-file-search-20261112/business-01`，这里保存全部根级
记录与租约、对应SHA清单；网关token为驱动公开的合成测试值。

轮询不能排除短于采样间隔的干扰。本组不是性能或全模型质量验收，
也不替代原生TLS/受监护交付及A11其余内建工具/MCP和全部master任务。
