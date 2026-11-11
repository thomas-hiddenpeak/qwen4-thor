# 原生检索嵌入后端：解决真实相关性缺口

2026-11-11。承接A10/A11/A12，不以Embeddings接口交付代替检索能力。
当前默认3cd4361c保持。已有生成模型LAST向量在3个项目段落/2个
查询中top1为0/2，必须实现合适的检索表示并取得真实任务证据。

## 已核实的实现依据

采用[官方Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
作为首个专门训练的本地嵌入后端，冻结模型提交
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`。其官方配置为28层、
hidden1024、GQA16/8头、head128、FFN3072、普通RMSNorm、完整RoPE
theta1000000、BF16权重，LAST+L2及MRL输出；不是Qwen4的HC/QSA/MoE
结构，不能将当前模型层实现直接套用而忽略结构差异。

实际tokenizer为151643基词、151387条数组形式merge、26个added
token，与现有Qwen4正则的Unicode marks处理不同。其训练后处理
还追加151643 EOS，必须精确实现，不能使用Qwen4分词或丢失EOS。
参考公式使用已安装Transformers5.16.1源码；验证环境已有
PyTorch2.14.0+cpu/tokenizers0.23.2，仅用于验收，不成为Serve依赖。

## 依赖、资源与故障边界

- 增加的是可选的Apache-2.0模型权重资产，官方文件1191586416字节，
  SHA256 `0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd`。
  下载到`.q4t-work/native-embedding-20261111/assets`，主模型及reference
  保持只读；记录版本、哈希和许可来源，不将权重提交到代码仓库。
- 使用项目C++23/CUDA/既有BF16 GEMM、ICU tokenizer和safetensors
  能力，原生补标准GQA/RMSNorm/SwiGLU；不以外部推理服务执行。
- 独立模型名、tokenizer、维度/容量及健康状态，仅显式配置启用。
  资产缺失/损坏、该后端初始化失败或自身额度不足不得停止健康Chat；
  返回对应端点的真实错误和独立健康诊断。真正CUDA设备故障仍触发
  全局设备保护，不能伪装为普通辅助失败。
- 权重、临时张量、文件页缓存均计入Thor统一DRAM；逐输入有界执行，
  复用全局GPU互斥及客户端RPM/TPM/取消，不引入并发模型竞争。
  配置/发布身份必须绑定新资产与可用容量，禁止隐藏额外RAM占用。

## 连续实施顺序

1. 为固定Qwen3嵌入资产增加显式tokenizer profile，精确验证正则、
   merge形式、EOS后处理和全部ID；原Qwen4默认合同保持。冻结实际
   参考token ID和反例，成组完成后只做针对性CPU验证。
2. 实现原生dense嵌入模型：严格配置/张量形状/字节验证、只读加载、
   普通RMSNorm、因果GQA及RoPE、SwiGLU和残差、最终norm/LAST/L2。
   复用BF16 GEMM和数值保护；没有lm_head、HC、QSA或生成伪向量。
3. 接入Serve显式模型发现/路由、原始文本/token输入、真实token
   用量、维度范围、Batch准备/执行、故障隔离及资源释放；封包和
   原生自监护配置携带可核验资产选项，缺资产不扩大为Chat不可用。
4. 冻结受影响CPU检查，再同一严格GPU租约做小型原语/参考差分、
   实际HTTP/SDK与项目检索、取消/Batch/Chat继续服务。对照现有
   0/2缺口，并扩充有来源的查询/段落，不能仅为两条观察调参过拟合。
   具体启动/请求/时间预算在代码完成后冻结，不跑完整性能/全模型矩阵。
5. 形成可用原生发行、资产和恢复合同，更新能力与证据；继续A10
   剩余媒体、A11工具与R/Q全部总任务，不以本后端结束总目标。

当前已完成并验证固定分词及配置/张量基础合同，完整原生后端仍在
开发；尚未取得专用后端真实业务证据，不能宣称已解决检索问题。

## 基础合同CPU组

显式tokenizer profile、310张量和配置合同已成组开发；独立固定
tokenizers0.23.2产生14条原始/追加EOS参考输入。冻结6项检查：完整
参考ID、旧Qwen4默认隔离、EOS容量与取消、错误EOS/非整数ID/重复key、
错误数值结构、实际张量名/形状/字节。仅检查基础合同，未实现完整
前向/Serve后端；通过后立即继续后续步骤，不将此组作为总目标终点。

6/6基础CPU于2026-11-11首次通过，见[原始证据](evidence/serve-native-embedding-foundation-20261111/README.md)。继续原生计算及服务接入。
