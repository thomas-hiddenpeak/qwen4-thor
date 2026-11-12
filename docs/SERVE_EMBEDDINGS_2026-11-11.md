# 原生 Embeddings：真实隐藏状态、标准接口与 Batch

2026-11-11。承接总计划 A10/A09.7/A12；此项交付后继续剩余总任务。
起始默认为 `ee550cab`。当前标准接口/真实计算业务已验并交付
`3cd4361c`，项目检索表示质量仍未完成；见文末交付与后续任务。

## 已核对的实际缺口

- 固定 OpenAI JS SDK 7.47.0 的 `embeddings.create` 默认发送 base64，
  再由 SDK 解码为 float；只返回 float 不能闭合标准客户端。
- 本模型最终表示是 48 层后的 hyper-connection mixer 输出，2560 维；
  MTP 保存的是 mixer 之前的多流 trunk，不能直接作为最终表示。
- 现有有限响应、Batch 行读取/结果及身份预算不足以处理标准向量批量；
  放大所有 Chat/schema 预算或分配数百万个 Json 对象都不是合理接线。
- 当前是生成模型，不能宣称经过专门检索或 Matryoshka 训练。

依据：本地固定 SDK `resources/embeddings.ts` 与 `lib/embeddings.ts`；
[官方指南](https://developers.openai.com/api/docs/guides/embeddings)；
只读 vLLM `67111973` 的 pooling adapter/seqwise heads 和 qwen4_exp
最终 mixer。采用原生实现思路，不引入外部服务或复制参考源码。

## 实现顺序与合同

1. 原生模型原语：既有层计算、最终 mixer、最后一个真实输入 token
   的 hidden；不跑 lm_head，不生成伪向量，不添加 ChatML 或隐藏文本。
   原 Chat/MTP 的 HeadForward 保持。按现有 chunk/序列槽调度并在完成
   同步后读取，取消只能在安全完成边界释放资源。
2. 输入：必需 input/model，支持字符串、字符串数组、token ID 数组及
   token ID 二维数组。最多 2048 项，每项最多 min(8192,模型容量)，
   合计 300000 token；真实 tokenizer/词表校验，不截断。客户端输入
   上限和 RPM/TPM 继续生效；模型名只接受真实服务名和显式别名。
3. 输出：默认完整 2560 维、LAST pooling、L2 归一化；dimensions
   选择最终隐藏向量的前缀后重新归一化（1..2560）。明确这只是本地
   数值转换，不声称训练过的降维保真性；另做小型真实检索观察。
   float 用可往返 FP32 的 JSON，base64 为小端 IEEE754 FP32；拒绝
   非有限值或零范数。顺序/index/标准 usage 完整，无部分成功响应。
4. 资源与 Batch：直接构造有界 wire body，最大仍为现有 96MiB。
   对向量响应使用全量语法校验、有限元数据投影；不构造全部向量 Json
   树，原始响应字节保留。输入使用专属 400000 节点预算；Batch 按
   endpoint 选预算、原请求绑定、真实 token 计量和恢复，其他端点保持。
5. 执行：单外部请求单 RPM，累计实际完成的输入 token、输出 token=0；
   失败/取消计量、Batch 预留认领、权限、资源保护和进度观察都接入。
   HTTP/原生 TLS/可选 nginx/合同/能力表及固定 SDK 一并补齐。

## 针对性验收出口

成组开发后冻结确切用例清单，再统一执行，不按每个文件反复测试。
CPU 覆盖输入四形态/精确整数、容量/别名、编码数值、有限响应与 Batch
大结果/恢复/usage、取消/配额及真实 SDK/TLS 接入；必要构建零警告。
严格同租约 GPU 验收只覆盖新 mixer 原语数值、真实 HTTP Embeddings、
字符串/token ID 等价、不同输入、SDK 默认编码、Batch 和随后 Chat
继续服务及资源清理。真实小型资料检索结果单独记录，不以协议通过
冒充检索训练质量。运行前冻结启动/请求/时长上限和所有实际身份。

不跑五档性能或全模型正确性矩阵，不动保留视觉候选。通过后保留同
接口回退与兼容状态证据、更新默认包及元数据、阶段提交推送，随即
继续媒体后端、恢复和总计划其余任务；未通过部分继续保留为缺口。

## 本组CPU清单冻结

成组开发完成，冻结30个C++用例（15新/15直接受影响），3个schema/SDK来源用例、1个可选前置路由用例、1个真实原生TLS官方SDK编码用例。精确名字存`.q4t-work/serve-embeddings-20261111/cpu-plan.json`。GPU原语与业务尚未执行，先完成这组针对性检查和源身份核对。

首轮30 C++/真实TLS通过；合同有负零及协议文件读取界限两处失败。
修复后只补`embeddings_fp32_wire_roundtrip_and_base64_byte_order`、
两个失败合同项、新增`test_protocol_contract_has_separate_bounded_capacity`、
捕获SDK序列化前FP32字节后的原TLS项。共36个不同CPU用例；原失败保留，
其他通过证据复用，不重新执行整组或扩大到全模型测试。

## 实际业务清单冻结

`tools/serve_responses/embeddings_acceptance.py`冻结同一严格GPU租约内：
1个合成最终mixer原语用例、最多2次模型启动/8请求/18个embedding
prefill chunk/900秒，另1个Chat限64输出token；控制请求上限2000。
先执行3项驱动CPU反例/配额/预算检查；原语为T=4和T=1逐BF16位比较，
不运行其他GPU测试。实际业务包括固定SDK默认base64及float/token ID
原输入等价、32维前缀归一化、不同真实项目段落、错误/TPM拒绝、Batch
原输入执行与prefill实际提交后取消、同版本明确重启后的原件/用量
读取、在线与Batch向量相同、Chat继续回答。模型目录只读tokenizer
确认`!`对应原token0；源文档/SDK/引擎/测试二进制及默认保护身份冻结。

两条真实项目查询的3文档余弦排名单独记录，不将本生成模型的局部
表示冒充专门训练的检索质量。重启检验是同版本持久合同，不冒充
跨版本回退；后续交付须单独记录实际可用回退的能力与状态兼容。

## 当前交付与必须继续的缺口

[接口交付](evidence/serve-embeddings-delivery-20261111/README.md)已完成，
49项不同CPU、1合成GPU原语及2启动/8请求真实业务通过；默认3cd4361c、
发布rev50 STOPPED。同版本已验包作为真实恢复锚点，旧ee550cab无法
执行新Embedding任务，不能充当兼容执行回退。R06继续补版本/能力匹配。

3个项目段落、2条语义查询的top1为0/2，说明当前生成模型LAST表示
未达到所需检索用途。必须继续实现合适的原生嵌入模型或有依据的
检索表示，补真实相关性证据；不能用接口交付或声明限制关闭此项。
