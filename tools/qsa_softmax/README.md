# 大T sparse softmax HTTP首验

本阶段仅比较已短验R1/R2父版与单文件候选；当前默认单独保护。
范围及条件以`docs/QSA_SOFTMAX_PLAN_2026-11-06.md`为准。
必要构建后先提交源码，再执行prepare；生成plan不得复用或覆盖。

```bash
python3 -B tools/qsa_softmax/prepare.py \
  --original-root /home/rm01/models/dev/qwen4-thor
```

prepare固定两个plain启动、1K/45K各三请求及11质量题，34HTTP；
1140秒工作窗口、每启动570秒、总1200秒含清理。运行时必须用主
仓库`tools/evalscope/gpu_exclusive.py`包住整个`stage.py --plan`，
lease、driver、plan均传绝对路径，避免guard切换cwd后选错来源。
驱动只启动自己负责的服务，继承已交付的最终监控屏障，不做预热、
探针请求、重试或cache drop。

租约结束后`stage.py --plan ABS --analyze-only --lease ABS`核对
输入、监控、封存文件，从SQLite独立重建全部34响应，再作固定收益
判定。45K平均TTFT下降至少3%且E2E下降才进入B；GO仍不等于性能
接受。NO_GO不补样、不推进数值/五档B，也不替换默认。

模型目录和reference只读；禁止脱离GPU租约运行服务。不要提前运行
CPU或GPU测试，必要构建和静态审阅后第一项测试必须是真实HTTP。
