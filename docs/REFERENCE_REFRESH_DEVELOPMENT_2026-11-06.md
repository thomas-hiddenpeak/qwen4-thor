# 参考项目增量首轮：开发准备结果

结论：**SOURCE_READY_ACCEPTANCE_DEFERRED**。本轮完成全量参考盘点、
重点增量静态审查和两个工作包开发；12项新增测试定义尚未执行。
不是已验收发布，也没有新的性能收益结论。

研究与取舍见[全量矩阵](REFERENCE_REFRESH_2026-11-06.md)，
来源/构建/默认身份见[阶段记录](evidence/reference-refresh-20261106/development.json)。

## R1：配置兼容与拒绝

提交 `631cc3c5`，干净运行父提交 `92f5abaf`。

- [配置解析器](../src/io/model_config.cpp)在qwen4_exp文本层列表入口
  将`full_attention`、`qwen_sparse_attention`、`indexed_attention`
  统一到原内部名称，保持层数、顺序和GDN名称。未知/非字符串条目、
  缺失或非数组、非正层数及长度不符明确拒绝，失败不发布部分配置。
- [5项CPU用例](../tests/io_model_config_test.cpp)覆盖上述边界及混合
  别名；合成配置只写`.q4t-work/`，不触碰模型目录。已有真实配置和
  缺失文件用例保留。这里只支持别名，不承诺任意HF新配置均能加载，
  StartupPlan的固定模型几何与3:1层序约束仍生效。
- 解析器与测试翻译单元编译退出0、日志为空；没有执行这些用例或
  加载配置夹具。见[r1-build](evidence/reference-refresh-20261106/r1-build.json)。

## R2：QSA细分观察准备

R2在R1之后独立提交。只增加诊断，未替换投影、top-k或attention算法。

- [FullAttentionForward](../src/model/full_attention.cu)将core划分为
  主Q/K准备、indexer投影、indexer准备、score/select、sparse五段；
  [观察器](../include/q4t/model/layer_subtiming.h)复用原事件机制，
  原12项输出格式保留，新增`[qsa-subtiming]`行。
- [RunLayers](../src/model/model.cu)仅在原LAYER观察开启、新开关精确
  等于1、T>8且无checkpoint写入时启用额外事件。关闭时仍分配原
  事件集合；MTP多槽verify总T>8也不启用。T不是纯prefill的证明，
  mixed batch需要按调用角色另分组；既有单流45K目标选真实prefill行。
- 五段的开始/结束与预期QSA层数必须完整，记录/同步/计时失败及早退
  都标记`valid=0`。早退清理避免部分记录进入下个forward。不能将
  缺行或无效记录记0，不能把五段与外层full_core相加，也不能据
  CUDA event区间证明纯kernel耗时、带宽或观察无扰动。
- [独立CPU合同程序](../tools/verify/qsa_subtiming/README.md)有7项
  定义：开关/小T/checkpoint边界、关闭事件集合、只对实际QSA层
  计时、缺失边界/层、event错误、部分创建失败及早退后新forward。
  使用隔离fake CUDA；没有设备依赖，不能证明GPU行为。
- 两个受影响CUDA翻译单元以CUDA13.3/SM110a/C++23/Wall/Wextra编译，
  独立CPU程序以g++14/Wall/Wextra/Werror编译链接；最终均退出0、
  日志为空。见[r2-build](evidence/reference-refresh-20261106/r2-build.json)。
  没有运行CPU程序，也未链接新引擎ELF。

首次构建筛选脚本因外部XGrammar路径的relative_to失败，编译器启动0；
随后model.cu因未引入的diagnostic辅助函数编译失败。改为本地明确开关
判断并重编受影响文件。之后静态审查补充checkpoint排除和无效状态，
相应单元重新编译。原失败及被替代构建记录保留在证据`builds/`中，
不改写为首次通过；没有因此执行任何测试、GPU探针或推理。

## 来源、服务边界与回退

接受来源清单223项与root main逐项一致；研究树只改5个运行来源：
model_config.cpp/.h、layer_subtiming.h、full_attention.cu、model.cu。
旧3项GDN差异已在 `92f5abaf` 独立恢复，NO_GO实现/原证据仍由
`f9488f70`及其历史包含，不在当前候选中。

Serve清单仅复审并更新既有model.cu锚点；字段、路由、模板、工具/
推理输出解析、配额和调度未改。清单检查及其变异用例**未运行**，
该登记不认证语义。model.cu属于协议来源：后续正式CMake配置会产生
新的协议digest；对象编译不生成可部署新协议身份，旧托管包/任务
仍固定旧身份，不能直接兼容性放行。

默认程序SHA：
`c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636`；
sidecar SHA：
`0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3`。
二者保持原位；主线工作区干净。上游210份原件与冻结索引摘要一致；
完整源码只在`.q4t-work/upstream-refresh-20261106`，reference只读保持。

回退边界：撤销R2提交可只去掉诊断；撤销R1提交可只恢复配置入口，
两者均不必恢复GDN研究。没有新默认程序，当前无需部署回退。
后续合入只提取已验工作包及配套校验/文档，不整体合并研究历史。

## 恢复测试后的有界顺序

以下是待执行清单，不是已执行记录。本轮继续遵守“不追加测试”。

1. 先全量构建候选并刷新来源/协议身份；R1执行5新+2原配置用例，
   R2执行7项独立CPU合同，再运行受影响的来源清单检查。
2. 第一组真实GPU工作全程同租约，暂定上限6模型启动、12 HTTP、
   1200秒：接受默认/候选关闭开关各两head的1K/45K（8请求），候选
   打开观察各head一个45K（2请求），正常完整head复用启动做一次
   真实资料工具两跳（2请求）。执行前固定实际驱动、输入和输出预算。
3. 核对固定旧配置输出、工具闭环、预期事件层数/完整性与关闭/开启
   墙钟差异。只作有界正确性/观察有效性判断，不据这组宣布性能
   优化通过；具体失败才修复重验受影响项，不因波动反复扩样。
4. 有效细分耗时足以指向一个保合同机制时，才开发下一项性能候选；
   另冻结收益目标，按HTTP优先及适用五档/数值合同验收。

本轮首轮目标至此收口：所有登记参考均有版本/取舍，两项源码准备有
独立边界；GPU/HTTP/测试/部署均0。其余候选继续按矩阵暂缓，长稳仍
由用户负责。后续测试恢复及性能实现不由本次编译记录自动授权。
