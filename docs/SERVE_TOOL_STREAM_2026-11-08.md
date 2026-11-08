# A04工具选择与真实增量流

A03已交付，继续总目标。当前代码只支持auto/none/required/named
function；工具参数要等整个XML调用关闭后一次发出，strict工具还
会等整条响应最终校验。这两处是实际待修缺口。

本组实现allowed_tools的auto/required子集、定义关联/精确错误路径、
模板/grammar/最终校验共同限制；支持实际逐片工具参数SSE，稳定
index/id/name，字符串安全转义，非字符串参数按完成边界转换，
多调用顺序与JSON结果一致。严格工具也发送增量，但成功终态必须
等待整条响应校验；客户端只在成功终态及EOF后执行整个工具批次，
截断、取消、后续错误均不得执行已有部分调用。

先成组开发再统一针对性CPU：选择/历史、模板、增量边界与错误、
schema/客户端重组。真实短验计划1启动/最多7Chat，上限2启动/
8Chat/900秒，覆盖实际资料工具闭环、增量先于调用完成、选择限制、
strict/多调用、取消/后续普通请求与资源清理。具体请求在CPU完成
后冻结；不做完整性能与全模型质量矩阵。

本组之后继续legacy functions/function_call及custom工具text/grammar，
并完成A05等其他总计划，不能将本子组当A04或总目标完成。

## 候选CPU完成，业务待验

最终引擎构建零警告；31项C++、28个输入/schema、9项Python和
4项实际JS SDK检查通过，另3项验收驱动检查通过。实际XGrammar
掩码拒绝不在allowed_tools中的函数；逐字节/UTF-8/转义分片和
多调用重组保持JSON一致。流已出现完整参数但尚无成功终态/EOF时，
客户端仍不交付可执行结果；后来错误会使整批失败。

保留build-01路径准备错误（尚未执行构建）、cpu-01能力数量旧夹具
及Python导入路径错误；只重验1项C++/9项Python。生产用例首轮
通过，默认仍d61c98b7。业务冻结1启动/7Chat，上限2/8/900秒：
allowed required严格资料工具与真实引用终轮、allowed auto JSON、
并行2条真实资料调用、参数截断、工具片段出现后取消、同PID普通
MTP继续。GPU证据未取得前不更新默认，也不宣称A04完成。

## 真实验收与交付

源码9d2324b9，1启动/7Chat通过：驱动61.929秒，严格租约62.504秒，
59次NVML全OK。allowed required工具发15个参数片段；并行两调用
分别15/17片段，稳定id/index，均实际读取项目文档。auto JSON选择
同一允许工具；真实结果终轮准确返回标题与行号引用。

截断输出已经出现工具参数，但无成功finish/usage；显式取消在参数
只收到`{`时启动，最终错误/DONE且工具执行数0。随后同PID普通MTP
回答42，统计7请求/5成功/1取消/1预期截断错误。模型退出0，GPU/
端口/锁释放；无请求重放或额外GPU尝试。

[133份原件](evidence/serve-tool-stream-20261108/index.json)保留全部准备/
CPU失败，581来源与Git/副本核对。原生入口/Guardian零警告且字节
不变，44控制面文件及已验CUDA探针不变，复用有效恢复/TLS证据。

默认945a05ea/元数据6c167898，协议35a9d29d；v4服务d2cc6b9b/
发布dda2ef99。安装持三锁，原ledger/Key/配额保持STOPPED，help/
status 0启动/0Chat。旧d61c98b7/b96c0f35及ee95045b/9e17f6c0作为
[可核验回退](evidence/serve-tool-stream-20261108/delivery-01/rollback/receipt.json)。

本组已交付。A04余项legacy functions/function_call、custom text/
grammar继续实施，A05及其余总计划保持开放；不宣称性能、全模型
质量、全OpenAI接口或长稳完成。
