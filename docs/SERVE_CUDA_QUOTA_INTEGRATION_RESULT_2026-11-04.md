# SRV-02j2 CUDA申请额度接线限定交付

生产额度接线已通过本机短验并作为默认交付。实现c69e4adc，原生
flags对照修正22594b59；后者仅改验证探针，生产ELF不变。代码与
边界见[接线合同](SERVE_CUDA_QUOTA_INTEGRATION_2026-11-04.md)，
原失败见[flags修正](SERVE_CUDA_QUOTA_FLAGS_CORRECTION_2026-11-04.md)。

默认ELF `a479d8ab02d133000bc7a029c307b60c9f42504faf20e679f5d292137550cb8d`，
sidecar `9ed141adb2fbb9f325959fea833bb0ed8949a9dc2010ad750296df6d7dece658`，
protocol `847aeb46483c46404a67d835ea62e5903d8738948fd64cccf9b2e941d74826d5`。
217项运行来源冻结。回退pair在
`.q4t-work/serve-cuda-quota-integration-20261104/package-v2/previous/`；
原d66f2bb9/42d7672a及旧day包保持，原day未执行。

## 实现与覆盖

默认`--cuda-allocation-limit-mib auto`取mem_fraction×MemTotal；0
显式关闭，正整数指定MiB，独立于no-budget。device、两类pinned、
managed、async和启动预留统一计额；Take不重复记，既有进程缓存
继续计额。固定表、申请前预占、异步pending、stream代次及未知
锁存接入生产wrapper。可恢复serve缓冲拒绝区分quota错误，未知
设备状态继续停止服务；config/metrics公开边界和各状态字节。

## 验证与原失败

- 原构建和v2均零警告。有效28 wrapper、268公共host、234必需host、
  9结构化后端、5驱动、3flags验收器检查通过。原zero_owner失败、
  CLI夹具缺model_dir导致的两配置失败、误前置负例均保留；定向
  重验2 wrapper及两配置各2 CLI，未受影响检查复用。
- 原GPU轮1 native在flags回显等值断言失败；没有完整buffer通过，
  没有模型/HTTP。COMMAND_FAILED、1.277353秒、2 monitor+1 preflight
  保持，不覆盖或追认通过。修正轮相同输入1，wrapper与直接CUDA
  查询均为3，确认原等值假设不成立；生产传参由CPU和直接对照验证。
- 修正轮6 native通过，12个完整buffer/88MiB/15额度检查点，覆盖
  混合类型、flags、零值、async跨流、handle代次、启动交付/回滚及
  显式关闭。小型直接flags对照4096字节先释放，mixed峰值仍32MiB。
  累计7 native启动，仅6记通过。
- 25原生HTTP生命周期通过；实际3次serve启动尝试，1MiB额度在
  首次大分配前拒绝，charged/unknown归零，未监听。其余2次成功。
- auto/full-head MTP k3、seq1完成普通JSON/SSE、strict JSON、
  真实read_project_record及引用回填5生成；显式100352MiB额度、
  startup-plan off/no-budget/no-MTP、seq2完成2并发客户端生成。
  7/7成功，无自动重试；3 grammar全部回收，8控制请求。
- 修正轮同锁84.085230秒、78 monitor+1 preflight，guard COMPLETED。
  10子进程保留到新guard扫描后回收；两轮共11自有进程已退出。
  安装同一已验ELF，CLI健康通过，完整HTTP证据复用，事务DELIVERED。

| 配置 | 额度bytes | 请求字节峰值 | server析构后live |
|---|---:|---:|---:|
| auto/MTP/seq1 | 118728574156 | 85736868120 | 111346176 |
| 显式/no-MTP/seq2 | 105226698752 | 79544561340 | 102964736 |

两配置unknown/pending归零，未发生额度/表拒绝或后端故障。析构后
live继续保留进程级所有权，不宣称进程内零驻留；进程退出后GPU
计算上下文为空，原66305/3294067锁可取得，容器空，SOAR恢复unit
保持masked/inactive/dead，后续观察无复起。

本轮属于可靠性工作。普通JSON/SSE耗时约0.434/0.328秒，strict
0.606秒，工具调用/回填3.756/4.032秒，两并发请求0.737/0.786秒。
仅为该次业务记录，不声明吞吐收益或性能非劣，不外推五档性能。

## 证据与用户交接

完整原件位于`.q4t-work/serve-cuda-quota-integration-20261104/`，
原cpu、execution及各固定包均保持。归档入口为
`tools/serve_cuda_quota_integration/report.py`；它同时核对原失败、
wire终态/工具引用、计数、guard、安装及退出，不重新运行模型。
首个离线归档核对误用finalization.json路径的失败另保存在report-audit，
修正为现有事务实际输出finalized.json，不影响任何运行时验收。

新默认的长验入口为`tools/serve_cuda_quota_integration/handoff.py`。
准备/check只生成和核对独立operator-package与operator-soak/day；
旧11项soak CPU及不变控制器代码证据复用。新包尚未运行托管E2E，
用户手动运行时会同时验证这一集成与长稳，不能将旧包结果改称
新包已通过。具体启动、停止和只读检查见
[长验说明](SERVE_CUDA_QUOTA_OPERATOR_2026-11-04.md)。

仍未覆盖DSO内部、host堆、文件缓存、driver与CUDA池保留，额度不是
整机物理RAM硬限额；无新媒体/SDK广泛兼容或小时/day稳定性结论。
实际LAN地址、证书、来源限制与正式通知渠道仍待配置，工业化目标继续。
