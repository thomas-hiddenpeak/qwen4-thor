# 元数据批次接入：精确父候选多路径差分

状态：FROZEN_BEFORE_RUN。父48185ed，候选b6870c0；默认c8f4b241
保持，新候选753d9769未部署。非性能测试，不新增五档计时样本。

两侧使用完全相同夹具，分别加载4层主模型一次和完整MTP层一次，
共2个进程/4次模型加载；每进程900秒上限，无重试。主模型max_seq4、
max_prefill8、max_len32、PLE容量8；draft max_seq4、max_prefill3、
max_len32，默认stream。全部运行及比较由同一NVML lease保护。

每侧固定轨迹：
1. 四槽位reset，RoPE未写区域显式清零，避免比较未初始化数据。
2. text prefill T4槽0；设置delta=2后single step pos4；single batch
   T3 pos5；每步保存logits和完整4层状态、KV/indexer/RoPE及delta。
3. ragged prefill lens2/3槽1/2；mixed lens1/3槽1/3、base2/0；保存同上。
4. 人工BF16视觉特征4行、grid1×4×4（merge2），T6含4个image token
   槽2；独立检查image位置1/2/3/4、三轴RoPE和delta=-2；再decode
   pos6并核对三轴位置4。不是视觉塔或多模态任务质量验收。
5. 同提示配错误num_tokens=3，要求明确拒绝；随后合法T4 prefill
   复用槽2，覆盖源已提交但未Finish的早退；保存恢复状态。
6. T8槽3 prefill保存trunk；MTP用此固定trunk、ids和positions，槽3
   和槽1分别执行T8（3+3+2）chunked extend，保存argmax、g和完整
   draft KV/indexer/RoPE。两次分别reset对应槽，所有状态比较有定义。

每侧主模型9次成功前向、1次预期拒绝，draft extend2次（共6个chunk）。
计划比较78个文件，必须逐位一致；每步另外检查已上传ids/positions，
ragged descriptor和多模态位置的独立预期值。父结果是兼容参考，
独立字段断言是布局合同；二者不替代整模型质量或跨形状确定性。

先编译两侧同一fixture并记录源码/binary/cache，构建不是测试。
运行前后校验模型文件stat身份，复用原模型hash清单，不声称重新
读取全部权重。若失败保留原现场，按具体失败定位，不自动增加轨迹。
通过后再冻结新身份的HTTP质量/取消范围；旧HTTP不冒充新候选通过。
输出.q4t-work/metadata-followup-20261020/batch-replay-v1。

构建准备记录：首次两侧fixture因int64 EOS到int32列表初始化的窄化
被-Werror拒绝，尚未执行模型；保留build-v1日志与失败fixture。增加
EOS范围检查和显式转换后，只重建受影响夹具，运行时源码不改。
原主模型201文件清单不含mtp；驱动在首个模型加载前补hash MTP
config/index和index引用的权重文件，并在运行前后检查stat身份。

## 结果

PASS_MULTIPATH_DIFFERENTIAL。两侧完成全部冻结轨迹；78文件逐位
一致，独立ids/positions/ragged/多模态位置断言通过。主模型2加载、
draft2加载，单lease约30秒正常退出；关停后174文件封存核验一致。
构建初次fixture窄化失败及修正后零警告记录均保留。
见[审计](evidence/metadata-followup-20261020/batch-replay-v1/audit.json)。

覆盖固定4层轨迹与实际draft层，不扩大为完整模型质量、视觉塔质量、
所有输入/设备错误或跨形状确定性。默认c8f4b241保持；新候选随后
按[HTTP卡](METADATA_BATCH_HTTP_2026-10-20.md)统一质量/取消回归。
