# Responses工具身份与结果合同

总计划A07的连续实施。命名空间、output_schema、async、caller及延迟
发现均是必须解决的缺口；本页将实现按可审查提交拆分，不改变完成条件。
本组已验能力当前默认rev102，剩余高级工具任务继续。

## 合同来源

已核对项目固定的官方JS SDK 7.47.0 `responses.d.ts`、官方
[Function calling](https://developers.openai.com/api/docs/guides/function-calling)
及[Responses创建参考](https://developers.openai.com/api/reference/resources/responses/methods/create)。
namespace包含name、description及function/custom子工具；调用item公开
name与namespace。output_schema描述字符串输出编码的JSON值，不约束
内容数组。具体来源身份在本组原始记录中固定。

SDK的function/custom选择器只有type与name，没有namespace字段。
本地采用精确全局名优先，然后`namespace.name`限定拼写、最后唯一
裸名；重复裸名拒绝为歧义，不任意选择。限定拼写是明确的本地适配，
不声称官方文档承诺这种消歧规则。公开tool_choice原样保留。

## 实现边界与顺序

1. namespace端到端：模型内部名称绑定(namespace,name,type)，顺序及
   后续声明变化不改身份。公开工具树、JSON/SSE、原始历史和存储仍
   保留客户端身份。预留内部前缀的全局工具也重新编码，避免伪造别名。
   展开后的可调用工具共用128项预算；错误定位保留原嵌套路径。
   原生file_search继续绑定原顶层位置，不能被展开下标影响。
2. output_schema：校验定义及实际回填的JSON字符串；支持JSON值根，
   不擅自收窄为object。结果合同必须绑定发起调用时的声明并持久化，
   后续更换tools不能改变既有调用合同。内容数组按官方合同独立处理。
   pattern/format等须复用有界语义执行，不静默忽略也不引入主线程
   无界正则；普通文本不因校验辅助故障而停止。
3. async/caller/defer_loading、其他内建工具与媒体按master继续；
   不能将上面任一提交或阶段通过称为A07或总目标完成。

## 第一组冻结验证

非性能改动，先成组实现再验证：8项namespace新增CPU用例，加直接
受影响的输入、流和持久执行旧用例；精确名称在测试前写入required-list，
不运行完整性能或全模型质量矩阵。覆盖定义/缺省、选择歧义、原路径、
128/129预算、无当前声明的历史、file_search位置、JSON/SSE截断、
未知内部别名、存储重启/客户端隔离/queued恢复。

真实业务在两组合同接线完成后冻结必要HTTP短验与发行检查；当前没有
因CPU检查而启动模型。单组完成立即继续下一项，全部master保持active。

## 当前验证与下一步

[CPU归档](evidence/serve-tool-identity-cpu-20261112/README.md)记录37项不同
C++用例、官方SDK及公开schema通过；标准custom grammar转换、真实
结果worker和SDK分页修复分别有针对性记录。
[实际工具业务](evidence/serve-tool-identity-business-20261112/README.md)、
[主轻资格及跨停机存储](evidence/serve-tool-identity-profiles-20261113/README.md)、
[保持新能力的真实服务回退](evidence/serve-tool-identity-managed-20261113/README.md)
均已通过并[可回退发行rev102](evidence/serve-tool-identity-delivery-20261113/README.md)；尚不等于完整A07。

结果校验复用当前schema dialect，完整官方支持范围仍须落实。
async/caller/defer_loading、typed内容数组及其余master任务继续。
