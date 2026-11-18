# PR47旧说话人资格夹具修复

最终9837ff8e公开检查已通过构建、公共host合同与来源门，在音频
恢复55项中仅旧真实speaker观察用例抛KeyError: separator。
夹具对应已发行的speaker-only配置，循环却遍历了后加的separator。

改为本文件已有的alignment/vad/speaker固定集合；不伪造历史发行
具有separator，不删除任何声音预算或隔离负控，新separator独立
用例继续保留。仅该失败用例重验通过，0.164秒、0GPU；其余54项
及既有真实业务复用，接着最终HEAD公开检查/合main与全部总任务。
