# 聊天接入交付

本阶段接续[已完成的业务验收](CHAT_BUSINESS_ACCEPTANCE_2026-11-02.md)，
按用户2026-10-20自动收尾授权和本轮“继续”完成默认程序交付。
用户要求释放GPU，结束后不启动vLLM或q4t常驻服务。

## 冻结范围

候选为830694ce、旧默认0c5ab1de/sidecar e6fdd13b，复用原二进制、
164项运行时源码、158项CPU/模板、8项故障及23生成/10负例证据。
不重编、不重跑已通过的业务矩阵。本阶段属于业务交付，不作性能优化
或统计非劣结论。

默认保留完整head、MTP k3，缓存及suffix仍需显式启用。
`parse_response`默认false；标准工具调用、推理内容和SSE输出需显式
`parse_response: true`。不扩展strict schema、强制工具、媒体或SDK
兼容范围。

交付前核对已归档的原始证据封条、冻结源码/模型配置输入、候选及
真实构建cache；保留旧二进制、旧sidecar、两份cache、subset产物、
候选源码、协议身份、Git bundle及原业务结果。包位于
`.q4t-work/chat-delivery-20261102/package/`，独立封条核验后才允许写默认。
Git bundle是增量包，依赖已保留的55299a0及其祖先；不是完整仓库镜像。

## 安装健康预算

先执行新增交付工具的7项CPU故障合同。之后由同一个
`gpu_exclusive.py`租约覆盖替换、全部健康请求、失败回退和自有进程
清理。包准备完成时冻结20分钟截止，失败不隐式重试、不重置截止。

| 项目 | 上限与通过条件 |
|---|---|
| 模型启动 | 2次，完整head和65536行BF16 subset各1次，均MTP k3 |
| 服务配置 | loopback 18097，4槽，208896＋3×65536，prefill4096 |
| 生成请求 | 共6次：每head两轮lookup工具往返＋1次流式推理 |
| 工具业务 | 完整head用JSON，subset用SSE；各只调用lookup一次，key=audit，最终831907 |
| 推理业务 | 两head SSE reasoning_content非空、答案323、stop及usage完整 |
| 能力GET | 共2次，由正式客户端校验协议身份及MTP/text运行参数 |
| 退出 | 每次3请求均释放槽，0 in-flight，startup reservation已分配资源归零 |
| 最终状态 | GPU计算进程为空、guard COMPLETED、自有恢复标记清除 |

缓存功能继续使用原验收证据；此次不新增缓存或并发能力声明。
四槽启动验证部署配置和单请求释放，不等于新增四路并发业务验收。

## 写入与回退

默认程序和sidecar分别同目录原子替换；两文件不是断电事务。
写前持久化`build/chat-delivery-recovery.json`；健康失败自动回退；
只有guard成功结束才正式finalize。未知文件身份、包损坏或外来恢复
标记直接拒绝写入。ENV_ABORT/未知清理状态保留现场，不能宣称交付。

只读核验：

```bash
python3 -B tools/response_protocol/delivery.py check
```

需要回退时，用一个新的输出目录持租约执行，不加载模型：

```bash
python3 -B tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/chat-delivery-rollback/lease -- \
  python3 -B tools/response_protocol/delivery.py rollback \
  --output .q4t-work/chat-delivery-rollback
```

回退恢复精确旧pair，保留原cache路径依赖及独立备份。原旧发布包
和此前回退链不清理。当前入口及依赖在Git bundle中；源码变动后
应从保留锚点恢复对应工具再操作。结束后服务保持停止。

## 执行状态

交付准备中；最终结果以本节、STATUS和原始安装证据为准。
