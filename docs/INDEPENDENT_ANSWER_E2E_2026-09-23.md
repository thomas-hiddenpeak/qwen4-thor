# 独立答案 HTTP 正确性对照（2026-09-23）

## 结论

当前候选33b11796不能按保持正确性接受。本请求集正式7364767为
14/15，候选13/15；候选新增1K条件求和答错。4K条件求和两版都错。
其余13题两版都对且输出相同。没有修改答案、重试替换或重设原参考。

| 输入token | 记录最终写入值（正式/候选） | 跨表查询 | 条件求和 |
|---:|---|---|---|
| 1024 | 对/对 | 对/对 | 对/错 |
| 4096 | 对/对 | 对/对 | 错/错 |
| 8192 | 对/对 | 对/对 | 对/对 |
| 45056 | 对/对 | 对/对 | 对/对 |
| 204800 | 对/对 | 对/对 | 对/对 |

| 失败题 | 独立正确答案 | 正式7364767 | 候选33b11796 |
|---|---:|---:|---:|
| 1K条件求和 | 360010 | 360010 | 360019 |
| 4K条件求和 | 360284 | 600440 | 600430 |

1K应相加120003与240007；4K应相加120140与240144。其他记录
不满足target且accepted条件，不能纳入。所有失败均正常stop，
非输出截断、HTTP失败或实际输入长度错配。

## 方法与证据

五档各三题，每版15请求，共30。正式先、候选后，单流、greedy、
MTP关闭，max_prefill=8192、max_len=208896、输出上限32。
原生聊天模板关闭思考，经tools/evalscope HTTP采集。每题每版
只运行一次，不构成重复性、尾延迟或整体准确率估计。

题目以合成权威记录定义答案，推理前冻结。实际prompt中的三组
记录重新解析后与manifest逐项核对，独立计算答案；服务端usage
确认精确输入长度。原始SQLite/SSE、输出、结束原因及两版SHA均
核对。初始输入准备有1-token偏差，未发HTTP；原脚本和日志保留，
通过背景padding修正后才生成冻结用例。

两个质量驱动均因语义失败退出1，两服务正常退出0。总采集驱动
退出0只代表完成对照，绝不代表质量通过。PID2299583已结束。

证据目录：`.q4t-work/e2e/independent-answer-http-20260923/`。
关键文件：`final-audit.json`、`comparison.json`、`results.json`、
`input-oracle-audit.json`和各版本`raw-audit.json`。
准备程序及初始失败：`.q4t-work/prepared/independent-answer-http-20260923/`。

二进制SHA：
- 正式：0c3f3914cb62ef66d9582c3c1b764e2a98917f6ebf33dbe986aa7f17b28cda30。
- 候选：33b1179677d642120536aab0c15b126283b5ea0a2e45d9f16437a9100191e22b。

## 能证明与不能证明

本轮已发现候选相对正式版本新增答错，不能仅以旧11题检索通过
或性能接近基线接受候选。正式版也有答错，故其任意生成文本
不能自动视为真值。输出不同本身不等于算错，本轮的独立答案
提供了更强、但仍只覆盖这些请求的证据。

没有独立模型实现的同题对照，没有定位到首个数值分歧，不能
据此断言某kernel数学错误、模型自身能力不足或量化误差是根因。
也不能推广为完整模型质量、MTP、多模态或批处理正确性。

## 同阶段性能与后续讨论

33b11796完整五档15响应已核对，均同直接父候选/正式回退，仍与
正式调度参考不同。TTFT均值0.801673/2.638763/5.171009/30.483429/
159.477408秒，decode18.586690/17.866128/18.177694/17.950162/
17.194894 tok/s。4K、44K后续TTFT仍对正式参考不利，其余指标
无不利范围分离；不是稳定提速声明。

本阶段确认工作完成，运行时候选不合入，正式接受版本不变。
按用户要求先讨论下一步，不继续叠加性能改动。建议先以新增
1K失败做固定请求的旧→新→旧HTTP复核，区分可重复回退与单次
不稳定，再按候选改动边界进行HTTP隔离；保留4K共同失败作为
独立问题，不用修复其中一个来掩盖另一个。该建议现已执行，结果见下文。


## 后续三侧复核：新增错误可重复

`independent-answer-repeat-20260923`已完成旧→新→旧各三次，
9条原始响应核对、三服务正常退出。正式前后六次都返回正确
360010；候选三次都返回360019，全部stop且输入1024。未改变
原题、答案、二进制或运行时。候选质量阶段失败保留，总驱动0
仅表示完成采集。该请求的新增错误稳定重现，但根因仍未定位。


## 进一步隔离：仅位置补写已足以改变本题

短indexer每head BF16舍入之前d85和之后d056各三次均360019，
六条HTTP原始核对通过，说明本题错误早于该舍入改动。
首轮归档无执行权限导致serve启动前失败，无HTTP样本；修正
复制件权限后的v2记录完整保留，原归档不变。

从7364767源码独立构建，仅添加原pooled decode MRoPE坐标补写，
不含GDN或indexer候选改动。首项HTTP正式→仅位置补写→正式
各三次，前后正式六次全部360010，隔离版本三次全部360019。
九条原始响应核对、三服务0，总采集驱动0；新增错误单独重现。
二进制SHA 0266bf52b17f6db5ece1ae349fef7e47dec8a54e1d51dab623171e9010536919。
证据`independent-answer-rope-only-20260923`，工作区运行时未改。

这证明位置补写单独足以改变本题答案，并不证明补写在数学上
错误，也不能以旧版偶然答对为由保留潜在位置错误。下一步应
审查位置生成与消费语义，而非继续调GDN/indexer或性能参数。


## 位置语义源码审查（未运行数值工具）

- model.cu的BuildRopePositions将[3,max_len]清零，只填充提示词
  的[0,T)；纯文本delta=0。分块prefill也只写各块覆盖范围。
- chat_server.cpp在prefill后令seq.position=T，提交当前生成token
  时使用该位置，decode完成才加一，没有发现本题的双重加一。
- full_attention.cu的RoPE及indexer query按seq槽、轴和绝对位置
  读取持久表；压缩key还读取组起点坐标。旧pooled decode未写
  新token坐标，因此可能读到零/旧槽内容，而非期望的T、T+1等。
- ModelDecodeStep已经写position+rope_delta，候选pooled补写与此
  相同；seq槽偏移为seq_id*3*max_len，轴步长max_len。
- 固定参考环境.q4t-work/refenv的Transformers **5.16.1**源码
  modeling_qwen4_exp.py:1369–1370以past_seen_tokens+arange生成
  新位置。另查evalscope环境为5.17.0，两者该语义相同；没有
  将5.17替换为验收参考，也没有运行独立模型推理。

据此，补写符合当前纯文本位置合同；旧调度路径存在缺失更新。
这不能证明所有MRoPE、多模态或数值运算正确，也不能消除本轮
观察到的答案回退。单题旧版答对不足以支持恢复未更新的位置。
下一步需要独立实现的同题输出证据或更明确的数值正确性证据，
同时继续遵守HTTP E2E先行约定，不能把源码一致直接当精度通过。


## 阶段收口：确认失败，不是确认正确

本轮独立答案30请求、旧新旧重复9请求、indexer边界6请求、
仅位置补写9请求，共54条HTTP响应已完成核对。确认新增回退
可重复，位置补写单独足以触发；尚未证明候选实现整体正确。
正式版也存在4K独立答案失败，不能将其所有输出当真值。

现有tools/verify/ref_dump.py默认仅4层、固定token序列forward，
专家权重反量化后使用BF16计算，不是现成的完整NVFP4模型HTTP
生成参考。未运行该工具，不能将它包装成HTTP以绕过E2E门禁，
也不能直接把其结果当作当前量化执行的逐位标准。

下一阶段建议先建立完整模型、相同输入和明确量化语义的独立
HTTP参考，再区分位置修正导致的合理生成变化、模型能力问题
与实现错误。保留1K新增失败及4K共同失败；不调整答案来迁就
任何版本。参考建立前不追加性能改动，不接受当前运行时候选。
本轮仅提交诊断文档；工作区候选代码和其他任务改动均不纳入。


## 独立服务可行性审查：量化合同尚不等价

固定SGLang commit `0a79825b7baa3e2aafd54e89097a5aba83d00b4e`
源码已下载到`.q4t-work/prepared/independent-http-reference-20260923/`，
未安装依赖、未运行模型或低层测试，reference目录未改。
其modelopt_quant.py:2411–2439显示：flashinfer_cutlass/trtllm
把w13和w2输入scale各自归并为全专家最大值；CuteDSL分支
w13共享、w2保留逐专家。不能仅凭同为NVFP4认定计算相同。

读取本地checkpoint第0层1536个F32输入scale（只读权重盘点）：
gate/up各512个且各自全部相同，值0.001964750699698925；down
的512个有301个不同值，范围0.000213623046875至
0.0053245909512043。当前moe_decode.cu:86按expert取input_scale，
moe_gemm.cu也保留逐专家scale。因此全专家最大值归并在本模型
确实改变量化配置，并非一个无影响的代码差异。这里只核查第0层，
不推广为全部层；原始值和归档SHA保存在checkpoint-scale-audit.json。

现有ref_dump不量化激活；SGLang上述两个后端改变down scale。
两者均不可直接充当当前执行的逐token真值。下一步优先审查
CuteDSL逐专家down scale路径在Thor上的支持及缓存精度设置，
若用于质量对照，必须完整列出仍有差异的计算合同。独立框架
答对/答错仍是证据而非单独证明本引擎数学正确。未启动新的
性能实验，正式运行时和验收参考不变。


## CuteDSL调用链复核与独立环境准备

继续追踪发现，前述modelopt_quant.py保留逐专家w2只是中间状态。
普通单机CuteDSL路径在moe_runner/flashinfer_cutedsl.py:217调用
cutedsl_quant_scale_to_scalar，再以最小倒数（原始scale最大值）
统一fc2量化，并重算alpha；因此也不等价。v1 masked路径需
DeepEP，不能据其接口声称普通单机保留逐专家scale。Thor实际
kernel支持仍未验证。后续独立服务定位为有明确量化差异的质量
对照，不作为逐token真值，不以它的答案单独验收候选。

在prepared/independent-http-reference-20260923内创建独立Python
3.12环境；固定SGLang源码依赖dry-run退出0，解析205包，包括
Torch2.13.0、FlashInfer0.6.17、Transformers5.12.1。这是独立
参考环境，不替换原固定5.16.1或evalscope环境。安装已启动，
会话76503，日志install.log；下载SSD Stream0.3.0 aarch64 wheel
成功，尚未安装插件。所有缓存和临时构建路径位于prepared内。
未运行模型、前置测试或修改引擎。SSD Stream自动CLI仅识别
RTX/GB10/特定x86配置，Thor需显式SGLang参数；不能套用其
默认含MTP的配置。下一步安装结束后固化实际依赖，配置MTP关、
明确KV/GDN精度，再以冻结请求作首项evalscope HTTP验证。


## 独立HTTP服务启动进展（尚无生成结果）

独立依赖及SSD Stream wheel安装均退出0，installed-requirements.txt
记录实际版本；插件的11个源码摘要全部匹配（package路径按
__init__.py处理，初始错误路径假设清单保留）。准备run_http.py
及reference-plan.json，冻结1K/4K filtered_sum各三次，MTP关、
BF16 KV/GDN、Triton GDN、CUTLASS MoE，max_tokens32；首项推理
由tools/evalscope发起，跳过server生成预热。仅是失败案例诊断。

首启动发现SGLANG_CACHE_DIR未跟随XDG_CACHE_HOME，主动在服务
就绪前终止，server=-15、0请求。v2显式设专用缓存至prepared，
完成197分片和PLE加载，但因max_mamba_cache_size=1而失败：
每请求需4状态槽，实际可服务请求数为0。服务/驱动已终止，
0请求，不是模型正确性失败。kv_cache_configurator.py:1959
确认该容量检查，v3仅改状态槽为5，单请求和题目不变；会话38318
已启动，证据independent-sglang-http-v3-20260923。安装会话76503
及前两次服务均已终止，不重复运行；下一步等待v3并核对HTTP。


## Thor启动兼容与JIT内存边界

v3状态池检查通过后，自动FlashInfer attention被混合GDN后端
合同拒绝（Blackwell允许triton/trtllm_mha/fa4），0请求终止。
v4仅显式选择Triton attention，成功加载81.89GB模型并完成
BF16 KV/GDN缓存、QSA初始化。框架内置启动autotune随即触发
SM110a首次编译；这不是人工另跑的bench，也不作为验收结果。

实际编译进程占用使整机122GiB内存中120GiB被使用、2GiB swap
耗尽。主动终止v4所属服务和编译子进程树（54进程），服务-15、
0请求；controlled-stop.json保留原因和PID，不视为观察超时。
终止后可用内存恢复119GiB，未修改模型或运行时代码。

FlashInfer jit/cpp_ext.py:347确认MAX_JOBS控制Ninja并行数。
v5只新增MAX_JOBS=2，保留已编译缓存，服务会话3212启动。
证据independent-sglang-http-v5-20260923，仍等待首项HTTP；
v1至v4均无答题样本，不计入正确率或性能统计。
