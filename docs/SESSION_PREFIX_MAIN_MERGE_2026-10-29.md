# 完整会话缓存与跨会话prefix共享主线合入

2026-10-29，按用户明确授权，两项已交付功能已快进合入并推送远端main。
原main为`cb4b085ec5429613dd7c693632fbfbbfcec5e44e`，交付锚点为
`1a5cf4131e96876a9b021a352c3dc855a74c8abc`。19个提交、161个差异文件
完整保留，无squash、rebase、合并冲突或新推理改动；中间自动多检查点
候选的NO_GO及其失败证据也保留，最终启用行为以显式单种子版本为准。

## 本项接受与证据复用

用户明确“当前的负向观察请保留记录,但是我可以接受”。该接受适用于
这次已交付的完整会话缓存和显式公共prefix共享，不改变原测量数据，
不建立总体统计非劣，也不扩大为未来候选的统一损失容差。

完整会话缓存原最大负向观察：plain4K E2E−0.1378%、plain45K decode
−0.2722%、full200K TTFT+0.0305%；首轮保存增加约8.8～9.4秒。
显式prefix原最大负向观察：plain8K E2E−0.18679%、decode−0.33189%，
subset8K TTFT+0.19204%；plain父/候选采样RSS峰值相差851460096B，
原因未隔离。8K/32K公共种子分别约368MB/1.125GB，保存成本、SSD状态
变化、模型锁等待、原夹具失败及监控覆盖限制均继续保留。

复用完整会话缓存233HTTP、显式prefix279HTTP（均含各自部署健康），
以及身份/依赖未变的直接合同；两轮数量不是同一候选的合并性能样本。
旧prefix研究104HTTP保留为NO_GO证据，不冒充已通过验收。本次没有
本机构建、模型加载、HTTP请求、GPU测试或性能重采样。
受影响状态已由显式prefix阶段重新验证，不能把旧版全部依赖一概称为未变。

主线自动触发的[公共host合同检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/40757136659)
通过，仅代表实际执行的无CUDA/无模型CPU合同，不替代Thor状态或性能证据。

原结果及机器可读证据83文件保持原哈希，原报告不追改为零回退。
详见[完整会话缓存](SESSION_CACHE_RESULT_2026-10-27.md)、
[显式公共种子](PREFIX_SEED_RESULT_2026-10-28.md)和
[旧候选NO_GO](PREFIX_CACHE_RESULT_2026-10-28.md)。

## 程序、回退与归档

安装程序SHA256保持
`6f3722b26e5b9d1b08d405e873d1f90d72f4b5c1aec2dd14589d1e27390b7965`，
发布sidecar保持
`5fdca879f84da6795b66113833de7167df6edbcff0d97e09d474113e47a67de2`。
165项运行时源码在交付Git、主线Git、工作区和封存源码之间一致；
发布来源仍为dc52dff及其实际构建清单，不伪改为本次合入或文档提交。
完整head/MTP k3仍默认，suffix、子集、私有缓存和公共缓存均显式选择。
[使用方式](../README.md#跨会话公共prefix共享)及容量边界保持。

两个发布包共399项封条逐项核验；三个归档共7037文件、3291292448字节
逐项SHA复核通过。它们包括完整会话缓存、旧prefix失败与显式种子交付。
回退链继续保留：

- `.q4t-work/prefix-seed-20261028/delivery-package/previous/`保存09fe/5027。
- `.q4t-work/session-cache-20261027/delivery-package/previous/`保存f2a6及其元数据。
- 各阶段原始日志、SSD子集产物、构建缓存、完整状态参考与发布包路径不变。

另有`.q4t-work/session-prefix-main-merge-20261029/premerge.bundle`额外备份
19提交，bundle verify通过，依赖仍保留在main中的原cb4b085历史。
合入本身没有替换已交付二进制，也没有启动常驻服务。

## 分支收尾

清理范围仅限本地及远端`codex/session-cache`，旧tip为上述1a5cf41。
删除前重新核对远端main包含全部提交、独有提交0、head/base均无打开PR、
工作区干净且已切换main、其他worktree未占用该分支。远端删除采用精确
旧tip的force-with-lease，本地使用普通`git branch -d`，均已完成。
所有worktree及绝对证据路径保持，未清理其他研究目录；当前工作分支main。
本次明确授权更新了旧报告“尚未自动合main”的阶段状态，原报告保持不变。

预检、主线身份、清理条件及实际结果见
[合入证据](evidence/session-prefix-main-merge-20261029/)。后续工作从main继续。
