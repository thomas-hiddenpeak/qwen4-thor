# 原4K完整MoE mapping/gather边界（2026-09-24）

## 观测与HTTP

新增最小只读观测，绑定BuildTokenListsKernel、BuildRowOfFlatKernel、
GatherQuantKernel及CombineGroupedKernel的完整4096-token路径。
零警告构建后首项tools/evalscope原4K无/有/无观测HTTP，三次仍
600440、4096输入/7输出、stop，服务0、质量驱动1。48层记录齐全，
只通过观测不改变本请求结果的门禁，不是任务质量或性能验收。

现场BuildTokenLists输入ID逐字节匹配上一轮完整router快照；首个
gather的完整x匹配冻结HC MLP mixed输出，其余gather要求同一
输入指针。每次gather的专家、行数、stride/k/hs、token-list指针
及实际列表内容均检查。CombineGrouped实际使用的反向行映射
匹配本轮映射输出，路由权重完整匹配上一轮router快照。

保存所有专家计数、prefix offsets、有效列表及反向映射；保存
21197次非空专家gather的完整FP4 payload、物理SF布局、逻辑SF
行布局，以及每层全部512个输入尺度。预留4GiB采集、4GiB参考、
20GiB余量；实际观测目录约3.5GiB。未重复保存完整x或未使用的
专家列表槽，未改写既有冻结证据。

## 已完成的核对

48×4096×10=1966080个flat(token,slot)在完整列表中各出现一次，
无遗漏、重复或越界；每个flat属于实际选中的专家，计数匹配
router ID的频数，offsets匹配完整prefix sum。反向映射逐项等于
实际列表的逆置换，后续combine读取的是同一映射。列表由原子
计数构建，不要求其内部token顺序固定，也不以排序替代实际行序。

每层512个实际gu输入尺度逐字节匹配对应gate_proj与up_proj的
input_scale，共49152份F32 checkpoint绑定，实际尺度均有限且
大于0；不是只核对最后token选中的少数专家。

物理SF按(block,group_tile,32,4,4)视图转置为逻辑行，通过不同于
观测器逐坐标offset循环的方式核对全部314572800个有效尺度
字节。保留物理padding原字节，但不要求未写入padding为零，也
不把它们当作有效量化数据。FP4 payload共5033164800个值，
完整字节已采集并检查长度，**本阶段尚未核对其量化算术**。

## 限制与下一步

映射正确、尺度来自checkpoint、布局一致，都不能证明E4M3编码
或FP4舍入正确。此前发现的E4M3高区间/子正规舍入问题仍在，
没有因本轮边界通过而被消除。也未证明FP4 payload被后续GU
GEMM按相同描述符消费，专家矩阵计算和完整shared分支尚待补齐。

下一步复用本轮冻结快照，独立重建完整输入量化，分别报告实际SF
条件下的FP4代码、SF与最近偶数编码的差异、以及设备算术对照；
不修改生产、不用局部一致掩盖原4K错答。

证据`.q4t-work/e2e/moe-full-gather-20260924/`包含三侧HTTP、
prior-binding、router-prior-binding、mapping-reference、
checkpoint-binding、所有原始packed/SF及metadata、summary和
artifact-binding。工具模板`tools/verify/moe_full_gather/`。
