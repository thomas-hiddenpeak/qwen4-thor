# MCP 资源冻结与恢复 CPU 证据

2026-11-16，16项不同C++首过，0失败/skip，耗时9.347秒；7项来源
负控通过，主机两次必要构建均零警告。完整引擎零警告构建通过，SHA为
`1d92a7466192d5cdc576c445fdaaec0972dae5e6e5175b2f9ece8e066b47a978`。0GPU，
rev126默认及发布记录保持，全部master继续。

## 实际合同

- 资源必须出自该已完成调用原始content中的resource_link；禁止
  凭任意URI创建权限，原模型/客户/服务身份和当前工具权限相交。
  当次凭据必须重新提供，新收紧的approval不能被历史调用绕过。
- ProposeResource在联网前持久保存ready；PermitResource以CAS
  保存唯一dispatch身份，resources/read许可之后才发生。Finish
  必须属于获准者；成功、已知失败、未知均为终态，不重放。
- 资源结果与原工具terminal分开，仍在同一加密调用中，32项和
  总4MiB限制；pending各预留1024字节用于派发/有界失败收尾，
  大结果不能吃掉兄弟任务的收尾空间。资源容量/协议/网络失败
  不能删除已成功工具结果或其他已冻结资源。
- 每次资源准备记录自身execution_owner和response_id。精确
  Response清理或已证明进程退出才收尾；旧工具请求结束不能
  误清理较新的资源准备。已完成调用的Reconcile同样覆盖准备项。

本地真实HTTP覆盖列表、一次tools/call、一次resources/read的
实际许可和断连不重放。file:///etc/hostname始终是原MCP endpoint
的RPC参数；成功数据为fixture远端文本，不读取本机该路径。
其他用例覆盖重启原值、跨客户/模型拒绝、并发唯一赢家、原调用
结果不变、资源数量/共享容量及精确拥有者恢复。

## 尚未交付的部分

这是资源权限/冻结部件的针对性证据，尚须接模型媒体准备、共享
源数据/像素/输出预算、审核/计数/压缩/冻结恢复等组合。跨操作
会话、connector/tunnel实际映射、完整SDK/真实业务/恢复资格、
可回退发行及main合并也继续；不以本组通过宣称MCP完整交付。

required列表、原始构建/测试/来源日志和源码SHA随本目录保留。
