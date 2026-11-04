# SRV-01c：工具/结构化错误位置与默认交付

2026-11-04。实现冻结`ac1a3e4d`，按[固定范围](SERVE_ERROR_PARAMS_2026-11-04.md)
完成本机短验并交付默认ELF`49cc78cd`、sidecar`055f4f3e`、protocol
`fc63db28`。这是输入错误定位修复，不是schema能力扩展或性能优化。

## 实际行为

工具定义、`tool_choice`、`parallel_tool_calls`、`response_format`和递归
schema校验现在显式传递错误位置至共用请求规划与HTTP层。例如多工具
重复名称定位`tools[1].function.name`；嵌套数组错误可以定位
`response_format.json_schema.schema.properties["a.b[0]"].items.type`。
标识符键使用点号，其他键使用JSON字符串括号，避免把键内的点号、
括号、引号或换行误当层级。组合/容量限制定位相关容器或节点。

保留原接受条件、检查顺序、错误码/message、有限schema profile、
模板和约束语法。auto/raw的schema仍可为建议，none仍不编译工具参数
schema。成功清空错误位置；既有不传位置指针的调用仍可用。模型输出
错误及无法归属某个输入字段的运行错误仍可能为`param:null`。

## CPU与真实业务证据

生产与CPU构建零警告；287项公共host、253项生产必需host、9项结构化
解码检查通过，这些集合有重叠，不相加为独立用例数。91项差分使用
前一默认已测源码与新源码：77拒绝位置全部符合独立预期，错误码、
message和接受/拒绝结果保持；14正例的完整prompt/grammar逐字节相同。
字节/深度/节点边界、错误路径转义、普通与严格模式均包含在语料中。

最终4项CPU驱动检查通过：GPU占用前拒绝、早期身份失败的自有child
回收、已回收child不阻断剩余清理、错误param不能被驱动判通过。
原两轮各3项收据保留，最终是4项有效覆盖，不算10个独立场景。

一次模型、一个本机TLS网关、两个独立测试Key。12类错误各经过普通
JSON chat、stream=true chat和preflight，共36次HTTP400；均为JSON
错误封套，param/code/type与请求关联头正确。两个客户端交替执行。
负例前后成功生成数及grammar启动/回收数均为0，未进入生成。

随后同一后端完成：

| 业务 | 成功响应 | 实际结果 |
|---|---:|---|
| 普通SSE答案 | 1 | 精确`710003`，7输出token |
| strict JSON | 1 | 精确`{"ok":true}`，6输出token |
| alpha项目资料查询 | 2 | 一次真实工具＋带引用最终答案，55/58输出token，预算512 |
| beta项目资料查询 | 2 | 一次真实工具＋带引用最终答案，55/58输出token，预算64 |

两个工具均实际读取`docs/MODEL.md`第1至24行，答案含准确架构名
`Qwen4ExpForConditionalGeneration`与工具引用。6份完整wire重新解析
与保存响应一致，grammar启动/回收5/5，正常shutdown为0 in-flight。
30次chat尝试包含24拒绝＋6成功，另12次preflight拒绝计入16次控制；
不能把30次尝试写成30次生成。

GPU同锁54.676966秒、52次guard观察无竞争，COMPLETED；业务段
53.344640秒，在300秒/1模型/6生成/36拒绝范围内。轮询不能排除短于
采样间隔的干扰。模型与nginx两个自有子进程回收，GPU/监听释放；
原锁dev66305/inode3294067保持且可取得，容器空，SOAR恢复服务仍
masked/inactive/dead/MainPID0。

## 原失败与限定重验

- 首轮机器语义文档一致性检查失败：6项源码摘要未同步。原结果保持
  FAILED，仅重新生成摘要和检查一致性，已通过的原生/差分未重跑。
- 首次停止后立即绑定端口返回EADDRINUSE；当时未记录具体端口的
  socket状态，原因未证明，不追认该次检查通过。后续确认无监听/
  自有进程，修正收尾检查使用SO_REUSEADDR绑定并独立读取LISTEN状态。
- 第二次收尾的普通`docker ps`没有权限，未取得容器状态。修正为本机
  既有`sudo -n docker ps`只读入口，实际查询为空，没有操作容器。
- 归档初检把报告源码中的私钥标记字符串误判为PEM；未创建归档。
  改为匹配独立PEM头行并继续扫描实际API Key/token，之后归档通过。
  随后因归档未创建导致的一次verify缺文件失败也记录在案。

以上收尾修正没有追加模型/HTTP。原部分postflight、工具输出观察
记录与原冻结报告源码均保留。原调用没有重定向的stderr明确注明来自
tool结果，不能称为原进程日志。运行源码与修正分析器分别固定：旧
`stage.py check`继续严格核对冻结报告SHA；当前离线修正使用
`report.frozen_plan()`验证其余来源及原报告副本，不放宽运行来源。

## 发布、回退与材料

默认程序已经按既有双文件发布事务替换，sidecar同步；完整保留前一
默认`7619966e`及`a2f9cb8f`。安装后核对同一已验ELF和CLI，复用同SHA
HTTP证据，未再启动模型。222项运行来源中只有8项错误位置相关文件
变化；数学/分词/模板/缓存/调度来源保持，不声称新的数值或性能验收。
旧托管包、原私有输入与三份day计划共13项固定产物保持，day未执行。
此交付不自动升级固定包，也不重绑持久任务的协议身份。

本机包位于`.q4t-work/serve-error-params-20261104/package/`；回退必须
通过共享GPU guard和独立新输出目录执行`stage.py rollback --output`，
它只接受已保存旧/新身份，未知默认文件拒绝覆盖。示例（从仓库根目录）：

```bash
python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-error-params-20261104/rollback-01/lease \
  -- python3 -B /home/rm01/models/dev/qwen4-thor/tools/serve_error_params/stage.py rollback \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-error-params-20261104/rollback-01
```

没有实际回退已通过默认。回退包与事务身份已核对，双文件替换不等于
断电原子事务；恢复标记和已知身份回退合同沿用原实现。

| 材料 | SHA256 |
|---|---|
| 默认ELF | `49cc78cdf03ba56f7a9b51f682cc61e636f6b36d2784289071b3c6ac5352c38a` |
| 默认sidecar | `055f4f3e406e05cdded906ae64ec72eaf687e381d3f09974be0e3a94c04d2067` |
| plan.json | `ca14712bfaf8c3aca4985f49de4d49d41cd7229d2065c12404d748268225ac7c` |
| 归档 | `2cac5cd4f6e85f9742438403d05d4076f71754dd5372c67853cf79fdc7bd583c` |
| 重建结果 | `7038825618857625e15ff86202374791451f2baaa7422efbb13b04836281b64f` |
| 修正报告源码 | `8e16b51962999fa0ee17dbcdb72dec97ec1277c6ee9efbad556cf53ab85463b1` |

[归档](evidence/serve-error-params-20261104/records.tar.gz)包含355份记录，
321580字节；[清单](evidence/serve-error-params-20261104/manifest.json)
另保留5个本机二进制路径身份；[离线结果](evidence/serve-error-params-20261104/result.json)
已重建通过。不含API Key、网关token或PEM私钥。离线复核不启动模型
或HTTP：`python3 -B tools/serve_error_params/report.py verify`。

实际LAN接入、正式通知/容量/备份介质、源盘丢失灾备、完整物理RAM
和用户小时/天级长验仍待完成，持续工业化目标保持active。
