# R02结构化工作进程回收证据

实现`0be7b38e`，接受引擎`c6285915`，非性能可靠性修复。
[清单](index.json)保存106份原件的路径、大小和SHA256，包含失败
以及修复后的针对性结果；构建产物、私有凭据和模型不进公共归档。

- [CPU范围和最终核对](audit.json)：40项不同用例。OFF23项，ON初次
  31/32后仅修复补验1项；另5项HTTP循环、3项验收驱动负例。
- [真实短验](business-01/result.json)：1模型/6Chat、51.999秒；
  [外层租约](business-01/lease/exit.json)COMPLETED，52.428秒，50次
  NVML全OK。工具实际读取本项目资料并带真实引用返回；worker故障
  为唯一`constraint_compile_timeout`错误终态，无自动重放或模型重启。
- [故障期间普通响应](business-01/04-plain-json-during-fault.http.json)
  约266毫秒返回42；故障请求10.008秒结束。只是本次观测，不承诺SLA。
  [最终计数](business-01/06-drain.metrics.json)4启动/4回收、active=0；
  待回收占槽与请求闭包释放另由CPU模拟未确认回收验证。
- [清理](business-01/cleanup.json)：2个直接记录身份均退出，GPU/端口/
  锁释放。不能把SIGSTOP/SIGKILL短验说成真实内核不可中断故障复现。
- [接受决定](release-01/decision.json)与[控制面证据复用](release-01/unchanged-control.json)：
  40份控制源码及Guardian/入口/探测ELF身份未变，仅引擎及协议身份更新。
- [默认交付](delivery-02/result.json)：引擎c6285915/元数据a48dcb67，
  服务包e7652b4c/元数据b08eea25，原服务ID/ledger字节和预算保留STOPPED。
  安装0模型/0Chat；[回退回执](delivery-02/rollback/receipt.json)指向保留
  的60b1c4c1/d2ae8db2引擎及b7743d20服务包。

保留的失败：HTTP目标漏链接健康投影/空PLE符号；新CPU用例漏messages；
生成合同源码摘要待刷新；安装准备误将未知`--help`当成功命令，尚未
修改默认即停止，改用已有`version`入口。均没有追加GPU验证。
三个已知凭据值已扫描排除。其余总计划任务继续推进，当前不等于合main。
