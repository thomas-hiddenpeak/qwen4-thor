# SRV-02i：媒体资源边界审计清单

2026-11-03，基于SRV-02h候选`4b5bf5da`。这是后续开发清单，未实现、
未验收、不改变02h已封存的来源和预算。GPU被其他vLLM占用时可继续
做本清单的合同/纯host开发，涉及已有运行时修改须另行冻结新候选。

## 代码事实与优先级

| 优先级 | 已确认问题 | 入口与交付要求 |
|---|---|---|
| P0 | 16MiB HTTP body只限制压缩/编码输入；stbi先完整解码源图，未设置源图像素/解码RAM上限 | http_request.h、processor.cpp；任何大解码前检查格式、源尺寸及有界预算，非法/过大输入有稳定code/param |
| P0 | 视频先解码并保存全部帧，再resize；帧数与源RGB总量缺少独立上限 | ProcessVideo；在任何帧解码前完成整批几何/累计预算，不能前几帧已耗尽RAM才拒绝尾帧 |
| P0 | Visual tokens展开后的max_len及max_prefill检查位于RunVisionPipeline之后 | chat_generation.cpp；先预估各item的处理后网格/输出tokens，再结合真实文本token和槽位容量拒绝，不为必然超限请求运行vision |
| P0 | SmartResize/VideoSmartResize中height*width、frames*height*width先以int相乘；ImageShape::L和total_L同样未经统一验证 | processor.cpp、vision.h、vision.cu、chat_input.cpp；宽整数/溢出检查及一致几何合同，验证正值、对齐、累计、size_t到CUDA int边界 |
| P1 | HTTP媒体嵌套字段不像文本部分那样完整闭合；DecodeImageUrl容许任意位置的padding，支持的角色/字段/格式没有独立兼容表 | chat_contract.cpp、chat_prompt.cpp；明确data/raw base64、角色、字段、URL/mime/字节关系；负例逐项有对应错误，remote URL继续拒绝 |
| P1 | 文档称PNG/JPEG，但当前stb默认编译了更多格式 | processor.cpp、third_party/stb/stb_image.h；明确允许的格式并拒绝其余，不用输出通道强制RGB代替格式合同 |
| P1 | AttentionKernel动态shared memory等于单时间组h*w*sizeof(float)，未见请求前与设备/函数上限核对 | vision.cu；使用已知设备/函数能力检查，不假设所有尺寸可launch，不以进程失败代替输入拒绝 |
| P1 | 媒体CPU处理位于model_mu之内，取消/截止时机较粗 | chat_generation.cpp、chat_input.cpp；至少在header预检、item/frame边界及GPU提交前复查，不声称能中断单次C解码；大任务持锁/等待合同需明示 |

实际patch向量为`3*2*16*16=1536`个元素；代码中的部分“96”注释陈旧。
后续内存账必须使用实际config和shape计算，不能据此旧注释制定预算。
这次只记录审计事实，尚未改写被冻结源文件。

## 开发与验收安排

1. 先冻结媒体支持合同、源图/帧/总解码和处理后patch预算、错误code及
   参数路径；说明它们是逻辑工作量边界，全机RAM预留仍未得到证明。
   默认值需结合实际上下文容量和合法工作负载给出理由，不能事后按
   成功样本扩大。该入口仍显式实验模式，不扩大公开文本兼容schema。
2. 共享纯host几何计划用于processor和serve准入，避免两套resize公式
   漂移。先检查所有item，再解码/申请大缓冲。成功输入保持现有像素、
   网格、顺序、模板和算子数值；变化属于真实缺陷时分开记录。
3. CPU检查使用无需大分配的极值/溢出头部、伪造尺寸、截断/格式不符、
   合法边界、累计帧和分配禁止探针；证明失败发生在重解码/设备调用前。
   原合法图像/两帧像素证据在身份和计算不变时复用或直接对照。
4. 最终GPU统一验收必须覆盖02h尚未执行的所有权/错误/真实工具闭环，
   再加本批过限拒绝同PID恢复、合法图/视频和最小受影响数值；具体
   次数、墙钟与合并候选的来源另冻结。不反复重跑未变成功项。

资源上限、真实解码内存峰值、上下文准入与全机物理保护分别记账。
不能因某次小图成功或没有发生OOM，宣称任意媒体都具备工业化保障。
