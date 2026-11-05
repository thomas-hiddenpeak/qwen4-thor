# GDN qkv/z BF16小批输入转换复用：有界判定卡

2026-11-06，用户授权限定GDN qkv/z、保持数值合同、HTTP收益达标后
再完整验收。本项是性能优化，遵循EVALUATION，不运行bench。

## 实现身份与范围

复用`codex/mtp-selected-head-rows`和原工作区。先将未接受的末行head
两运行文件及对应serve来源清单恢复到main；旧原型与证据完整保留于
`64ee7ce679e89d61f1b5222d77fc9525a99d5b7d`及之前提交，不混入候选。
当前默认仍为c1bdb0a7/sidecar0efc2c0d，main def8cc96运行来源逐项核对。
新候选相对接受来源仅改变small_m_gemm.cu、linear_attention.cu及gemv.h。

仅主模型一条序列的checkpointed verify，T=4、K=2560、qkv N=10240/
z N=6144启用；要求SSM/conv checkpoint、num_ckpt=3、非ragged，带槽ID
时tokens_per_seq=4。无FP8 shadow且无原有三个投影ablation才走候选。
普通prefill/decode、其他M、QSA同形投影、GDN out/a/b、MoE/HC、head、
状态及缓存路径均保持。符合候选条件后launch失败立即报错，不静默回退。

每CTA先把BF16输入精确转换为FP32 shared，按[row,pair,K/8]转置存放，
使每warp的float2读取连续；后续输出warp复用转换结果。
权重仍BF16；lane/K遍历、f32x2 FMA次序、warp归约、alpha=1及BF16舍入
保持。原内核保留为模板false特化。shared从20KiB增至40KiB，未减少
权重字节；额外shared访存/占用可能抵消转换收益。源码推理不代替实测
数值合同；不把上一轮15.45ms输入组全部算作可消除时间。

## A：先执行HTTP可行性关卡

零警告构建后第一项测试为真实evalscope HTTP；之前不跑单测、微测或
profile。固定full父→full候选→subset候选→subset父，各启动一次。
max_seq1/prefill8192/max_len208896、MTP k3、startup-plan off，缓存/
suffix/计时器关闭。既有模型、subset产物和输入保持，不清系统页缓存。
每臂1K×3、45K×3，均256输出；再11个五档固定质量题，最多32输出。
总预算4启动/68HTTP/2400秒，每臂≤600秒；无warmup、生成探针或重试。
完整驱动一把原仓库GPU锁，资源监控/退出屏障/最终join与外层租约有效
后才分析。所有请求/首请求、TTFT/解码/E2E原值保留。

事前投入门槛：**两head、两档四格分别**aggregate decode
`sum(output_tokens-1)/sum(latency-ttft)`至少+3%，同输出aggregate E2E
下降；prompt/文本/计数/finish及按请求绑定的MTP接受直方图一致，44个
固定质量期望全部通过。仅同head比较，不要求两种head输出互相一致。
这是继续投入门槛，不是通用损失容差或跨启动统计证明。

环境/监控失败记ENV_ABORT并保留现场，不作候选性能结论。输出/质量
失败停止定位；有效但未达收益门则NO_GO_FEASIBILITY_TARGET，保留
默认、候选与记录，停止本轮，不补样本、不扫参数、不进入B。

## B：只有A通过后再冻结执行细节

完整接受前仍须以下证据，不能把A通过当发布许可：

- 两head各父候选补4K/8K/200K×3，复用A的1K/45K并说明跨启动；
  plain父/候选各五档×3、保护TTFT/decode/E2E、精度及质量。
- 原small-M/GEMV与候选两种真实形状的逐位输出合同，固定归约独立
  参考、有限值边界/抵消/subnormal/零值与canary，不更换正确性基准。
- 实际GDN checkpoint与MTP logits/argmax/接受恢复、T=4及其他形状
  路由、FP8/ablation保持，多槽/取消/缓存的适用直接与HTTP合同。
- 每次继续前冻结具体程序/输入/请求与截止，B总额≤7200秒、14启动/
  110HTTP、直接GPU合同≤900秒；不因微小观察波动自动扩样。

只有全部通过才能按已有授权交付，另最多900秒/2启动/6HTTP完成回退
包、默认与元数据、健康和阶段提交推送。否则仅研究收尾；不开展长稳。
