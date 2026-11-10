# 文件综合业务与新状态格式跨版本验收

2026-11-10。PASS，严格同一GPU租约192.380秒，3次显式启动、
7条生成、9模型步骤（各进程1/6/2）、118控制操作；180次NVML均OK，
无ENV_ABORT。轮询只能覆盖观测时刻，不能证明采样间隔外不存在竞争。
Node SDK7.47.0实际Responses SSE；不跑全模型正确性或性能矩阵。

- 候选默认MTP普通Chat返回42，实际28输入/3输出token，正常退出。
- 候选显式media/no-MTP：原PDF同时返回数字42和绿色背景；预检与
  推理均1478输入token，含1152视觉token。PDF原件公开content_parts
  保持631字节源的精确base64，摘要/页图没有替代公开原内容。
- 新CSV计数实际执行1准备步骤，379输入/169输出token在响应头报告；
  标准count正文只报最终prompt。新CSV SSE完整created到completed且
  序号连续，真实摘要后回答42；previous的count和回答7复用冻结摘要，
  不重新准备。所有模型步骤、RPM和累计实际TPM按每进程准确对账。
- 实际17MiB inline Conversation原件由新版本HTTP接口持久化。显式
  正常退出后，用前一源码eee091b4构建的程序读同一目录，原字节SHA
  不变；实际Responses冻结历史继续回答7，公开PDF原件重建Chat后
  再处理并回答42/green。Chat重新处理PDF和Responses冻结快照复用
  分别记录，不把Chat误称为提供previous_response_id。

候选43472e764d89139995b2e967ccb7c62d4a768f922ef09bebd02abe082164d082，
来源1a507c93（引擎与93923d5b运行源码一致）。兼容读取器
2657eb168192aa6c9cf3e1dd77c52d1f9dea053d237777dc4b56e18d30134a96，
源码eee091b4634d95f855e8c5937bb0a4a3c500bcda。计划里的pending说明是
冻结时状态；本验收接受跨版本格式读取与业务，不代替尚未完成的封包
和部署检查。该回退代码仍有旧输入容量，恢复不能抹去原业务记录。

3个模型均exit0，全部自有进程/端口释放，锁重新取得并释放验证通过。
冻结源码、二进制、默认Uploads文件身份保持；既有vllm_proxy进程
715229/start20006607保持，未操作它或生产状态。大wire和私有对象
保留原工作目录，artifacts.json记录完整大小/SHA；其余原件复制于此。

复用先前已过的CSV后台真实业务及针对性CPU链；继续兼容回退/候选
封包、可回退默认交付、Batch及全部A/N/R/Q/G总计划，未合main，
不以本组通过结束总目标。
