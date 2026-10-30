# Serve审计两项收尾：验证与默认交付

2026-10-31，`ACCEPT_BOUNDED_SERVE_CLOSEOUT`。响应头状态缺陷已修复，
私有缓存失败发布的HTTP证据已补齐。默认程序与发布元数据已替换，
两种head的实际4槽安装健康通过，恢复标记清除，未建立常驻服务。
本轮在`codex/serve-closeout`提交推送，尚未合入main。

| 身份 | SHA256 / commit |
|---|---|
| 默认程序 | `037328bb5c8788920ef44d1dfac2df94f908760f5626a95f1a674568c0dac4f7` |
| 发布元数据 | `3200266ea8778507b9e9acdfc0cf0a8b2bb97ae02aa8262cd572fd0a9b060d06` |
| 实际运行时源码 | `9b9931193ff8a9abe9107a6d700dc2a8728a8464` |
| 本轮回退程序 | `27a483a31e5799b67342efbabea6812c6ec7efce1f0b03ffc304f31318fcbf28` |
| 本轮回退元数据 | `af6b3a4466390b11b01034a9194fbc41fe5e8d522d90b44107496473fdc97a6a` |

## 修复与证据更正

原实现先设`kStreaming`再构造header字符串，分配异常可能产生裸SSE。
现在固定header经无字符串分配的生产辅助路径发送；仅在实际写入成功后
进入`kHeadersStarted`，完整写完后才进入`kStreaming`。未写出可返回
HTTP500，部分header写出后关闭连接，完整header后可返回SSE错误；
终态开始后仍不追加第二个终态。

私有缓存发布条件没有重写。旧9216-token探针短于实际保存边界，因此
旧负向miss不能证明失败状态未发布；原102HTTP/23启动及所有原始记录
保留，撤回该私有负向推断，公共负向证据仍有效。本轮验证构建观察
实际保存token IDs并定向阻断发送，生产构建关闭全部fault入口。

四组均先用独立session保存正常参考，再验证同一continuation的正向
命中。失败请求保存后，逐ID确认与参考相同，随后RST并放行；失败key
首次成功续接不带公共scope，且先于任何成功seed覆盖。由此排除短提示
必然miss、公共缓存干扰及覆盖失败key三种无效验证。

| head | 增量保存 | 实际保存IDs | continuation IDs | 正向命中 | 失败后私有命中 |
|---|---|---:|---:|---:|---:|
| full | OFF | 9231 | 9253 | 9230 | 0 |
| full | ON | 9231 | 9253 | 9230 | 0 |
| subset | OFF | 9231 | 9253 | 9230 | 0 |
| subset | ON | 9231 | 9253 | 9230 | 0 |

保存IDs含一个pending token，实际恢复边界为9230。CPU真实分词验证
continuation长度足够，前9230 IDs匹配保存前缀；四组正负续接均正确
回答44。公共负向另测，均命中0。可审计摘要见
[私有发布证据](evidence/serve-closeout-20261031/private-publication.json)。

## 验证与预算

72项host合同、3项直接编译生产header的故障合同、4项探针/日志解析
合同通过；发布与独立fault构建零警告。header合同使用真实socket及
send/operator-new包装，覆盖实际短写后EPIPE、异常状态迁移与禁止分配。

| 阶段 | 生成HTTP | 启动 | 结果 |
|---|---:|---:|---|
| 首次header组 | 1 | 1 | HTTP500及释放正确；驱动解析失败，原COMMAND_FAILED保留 |
| header四故障＋恢复 | 8 | 4 | before/partial/after header、after finish，线协议及同服务恢复通过 |
| plain正常对照 | 2 | 1 | 1K流式/非流式完整文本、usage、终止原因与已接受输出相同 |
| 四组私有/公共缓存 | 20 | 4 | 真实前缀正向命中、失败key首次续接miss及公共miss通过 |
| 默认安装健康 | 6 | 2 | 两head实际4槽，种子、公共与私有恢复通过 |
| 合计 | 37 | 12 | 28成功、5预期error、4预期cancelled；全部请求释放 |

首次驱动把`[prefix-cache] enabled=...`启动行按请求行解析，触发
`KeyError('request')`。补充日志类型判断及回归合同后，完整重验该
失败＋恢复组；重复的1请求/1启动消耗原预留额度。旧脚本、旧plan、
失败日志和独立复核保留，不把原COMMAND_FAILED改成通过。见
[原失败复核](evidence/serve-closeout-20261031/retained-failure.json)。

遵循[原冻结合同](SERVE_CLOSEOUT_2026-10-31.md)：计划36/11，最大
40HTTP/13启动/90分钟。从原prepare到安装guard结束975.44秒，到
finalize及恢复标记清除983.21秒（约16分23秒），截止未重置。
每个执行阶段完整持共享GPU锁，无ENV_ABORT；服务正常退出，跟踪分配
全释放，最终GPU查询无上下文。合作式锁/轮询仍不能证明不存在短于
采样间隔的外部干扰。

安装健康采用`208896,65536,65536,65536`四槽，prefill4096，增量保存
ON；full/subset均公共命中8192、私有恢复9230，答案831907/44正确。
这些是顺序健康请求，不宣称四槽并发负载验收。默认完整head、MTP k3
及其他显式开关保持，未启动额外常驻服务。

## 交付、回退与边界

169项运行时源码与实际构建提交、当前工作区及封存副本逐项一致；
仅4项改变，165项未变。模型数学、分词、调度、缓存storage布局的
既有证据按依赖复用。本轮不是性能优化，也不扩展为200K、suffix
或SSD容量的重新验收，不宣称吞吐提升或总体统计非劣。

全部文件位于`.q4t-work/serve-closeout-20261031/`：

- `delivery-package/`封存候选、父程序/sidecar、真实构建配置、169项
  源码、子集head及交付工具，共194项封条核验通过。
- `evidence-archive/`独立复制171份原始测量、CPU、构建与失败证据；
  `health-archive/`在部署guard结束后复制并封存安装健康记录。
- `plan.json`与`resume-plan.json`保留原截止及工具身份；`frozen/`
  保留fault程序、tokenizer和修复前脚本；原更早阶段包及回退链不变。
- `final-audit.json`重新核对源码、默认身份、原证据、封条、实际计数、
  健康和绝对截止；其仓库副本见
  [交付核验](evidence/serve-closeout-20261031/final-audit.json)。

如需回退，从仓库根目录使用尚未存在的独占证据目录执行：

```bash
python3 -B tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-closeout-20261031/rollback-lease \
  -- python3 -B /home/rm01/models/dev/qwen4-thor/tools/serve_reliability/closeout_delivery.py rollback
```

回退预先核对程序和sidecar均为已知身份，未知并发更新不覆盖；本轮
回退目标是27a483a3/af6b3a44。逐文件原子替换配合持久恢复标记，
不构成跨两个文件的断电事务。

仍保留合作式deadline、all-active慢同伴等待及本地完整发送不保证
对端消费的边界。本轮两项审计反馈已闭合；下一步按已有示例接入
真实agent，再按实际失败或等待来源选择单一任务。
