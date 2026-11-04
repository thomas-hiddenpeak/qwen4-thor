# SRV-02h+02i：媒体失败、准入与取消限定交付

2026-11-03，按[冻结合同](SERVE_MEDIA_COMPOSITE_2026-11-03.md)首轮完成
组合GPU/HTTP验收并交付。原[接线CPU](SERVE_MEDIA_INTEGRATION_CPU_2026-11-03.md)
与[夹具/驱动CPU](SERVE_MEDIA_COMPOSITE_CPU_2026-11-03.md)按相同来源复用，
原失败、计数纠正、ENV_ABORT及旧包均保留。本批属于业务可靠性任务。

## 身份与实际行为

| 项 | 交付身份 |
|---|---|
| 默认ELF | `280b95dceabe5a3b1fd794019102de0dc9f0b62e8a3aa9fcc30eb5c57fdfd793` |
| release sidecar | `18c9d2c50c847c90c6b033b4350fa0cf10c59777bf3a9dacd01391250a341f19` |
| protocol | `29810d03d0cb0603bd86222b18d8a53693d888a28d2033a3de1c73d4677a86e8` |
| 来源 | 运行时39cb0ba3、211来源；组合工具冻结abb653c8 |
| 验证ELF | `00718deab9fd07729cd0bd1409bf04180297db3312fd493f2e6eb547cab87897` |
| 直接回退pair | `3fd31a21… / 3ba0b8d3…`，完整包与规则保留 |

共享媒体计划在重解码和设备分配前检查格式、全部帧、几何与展开预算；
decoder设置256MiB所有权额度并复核实际尺寸。支持字段、严格base64、
槽位准入和协作取消已接线。视觉加载失败拒绝启动，媒体资源所有权、
可恢复内存不足、设备故障停服及host固定错误响应形成受测路径。

生产文本使用完整head、MTP k3、单槽8192/inflight8/queued2；媒体为
显式实验配置`--allow-media --no-mtp`、单槽8192/max-prefill2048。
媒体与显式`--seq-capacities`仍禁止，不能把CPU假想组合当上线支持。

## 实际验收

全阶段同一GPU lease，guard为COMPLETED、未发现竞争。原生HTTP生命周期
25项通过；设备探针8个查询/属性拒绝通过，实际attention动态shared
上限48,128字节（block49,152、static1,024），与服务配置发现一致。
tiny视觉权重33 OOM/3 host/1缺失/1成功、workspace20 OOM/4 copy失败
及333真实视觉权重上传/回读通过。设备状态注入不等于物理OOM或硬件损坏。

| 业务组 | 结果 |
|---|---|
| 生产普通JSON/SSE、strict JSON、Node SDK真实工具 | 5成功；真实读取`docs/MODEL.md:L1-L24`并回填，正确引用`Qwen4ExpForConditionalGeneration`；3个grammar worker均回收 |
| 红色图片、两相同帧、坏图 | 图片/视频回答符合夹具，坏图按新格式合同400拒绝 |
| 视觉输出分配OOM | 固定503后同PID恢复正常图片 |
| 复制、视觉后host异常、释放错误 | 完整错误响应后按合同退出1；未继续接受请求 |
| host早期异常与SSE头后异常 | 固定错误及完整终态，同PID后续正常回答 |
| 视觉加载失败 | 未就绪，退出1 |
| 11类媒体准入负例 | base64/MIME/detail/源边长/帧尺寸/累计RGB/patch/prefill/context/attention/decoder配额的code、param、完整wire均符合合同 |
| 准入拒绝后的分配哨兵与恢复 | 11负例未提前消耗故障；合法图才触发预置OOM，随后不同请求同PID成功；计数13总/12错误/1成功 |
| 媒体取消 | 视觉完成后取消API202；SSE唯一取消错误与DONE，无成功终态；同PID正常图恢复，计数2总/1中止/1成功 |

实际32次生成尝试、21负场景、11启动，精确符合计划，首轮通过，无自动
重试或扩样。21负场景含一次启动拒绝；32完整wire含12成功/20错误，
52封套通过。103次业务控制观察、329条连续样本按场景留存，静止点
资源所有者归零、slot1/1 free。不声称采样覆盖退出瞬间或全部资源峰值。

业务371.464秒，从lease开始到finalize380.076秒，未扩2400秒硬限。
普通JSON/SSE约0.466/0.349秒、strict JSON0.624秒、真实工具/回答
3.941/4.225秒，图片/两帧约0.528/0.411秒。仅为有界业务观察，没有
父候选五档性能比较，不作性能非劣或全模型数值等价声明。

## SOAR服务交接、安装与回退

此前占用GPU的SOAR恢复脚本于16:05:32启动；候选试验16:21:52因
`disk_start_below_49gib`在停止基线前退出，常驻监督器继续持锁。
调查时运行/排队与累计推理token均0，属于模型驻留，不是活跃推理业务。

用户随后明确授权停止该恢复服务、清理容器、释放GPU/锁、防自动拉起
并继续项目验收。19:06:26 CST核对正常停机清理完成：所属容器、8000
监听、GPU进程及所属恢复标记均无；原锁inode保持且可取得。持久mask
`soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service`，原Docker/
systemd均Restart=no，未发现关联timer/cron。原批准、一次性claim及
terminal保留；没有修改SOAR源代码、模型或其它服务。

验收通过后按已有授权安装默认ELF/sidecar。CLI与独立grammar worker
握手通过，HTTP复用同SHA候选证据，无额外安装模型请求。新/旧回退包
只读核验及42个完整pair历史链通过，未实际切换回退；最早明确stale
边界仍不可安装。

19:15:06 CST最终检查：11个owned进程均退出，cgroup为空或移除，
NVML无计算PID，原锁可取得，无部署或外部恢复标记；MemAvailable
115.715GiB。SOAR服务仍masked/inactive，所属容器未再出现。

[证据目录](evidence/serve-media-composite-20261103/README.md)保留669份
原件，15份Git文件共1,213,881字节，22个本机ELF/bundle身份；原CPU
结果及早期ENV_ABORT保持历史状态。[保管核验](evidence/serve-media-composite-custody-20261103.json)
核对原件、压缩成员、暂存blob和发布pair。完整本机目录为
`.q4t-work/serve-media-composite-20261103/`；SOAR交接原件在
`.q4t-work/vllm-release-20261103/`。归档不追加模型或HTTP测试。

交付后收尾曾误调用要求旧默认仍在的准备阶段CPU gate，按设计收到
`default changed`；[原拒绝记录](evidence/serve-media-composite-post-delivery-audit-rejection-20261103.json)
保留。随后使用[交付后核对](evidence/serve-media-composite-delivery-check-20261103.json)
检查运行时/工具来源、历史CPU包seal、已安装新pair、执行seal与回退包，
全部通过。没有改门禁、回写旧默认或重新执行测试；该收尾入口错误与
首轮GPU验收结果分开记录。

## 剩余边界

本批只证明固定媒体输入和故障路径，取消仍是协作检查，不抢占单个
解码器调用或kernel。所有权配额、全局内存准入与提前拒绝不能证明
进程或整机物理RAM硬限额。广泛多模态质量、新ELF小时/天级长稳、
真实部署鉴权/TLS/通知/多人策略和通用任务恢复仍独立待办。
工业化持续目标未因本批交付而完成。
