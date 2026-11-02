# SRV-02b收尾回退工具检查

运行时/真实HTTP冻结919cc01f、c16caf05/be4cd021已交付后，修正回退动作
对当前工作区源码的依赖。rollback只接受原封存package、旧pair/cache、
已知新旧默认身份；保留原GPU独占、外来marker及未知身份保护。接受执行
仍保留严格源码核对，不放宽实际模型/业务验收。

CPU2项通过：当前工作区后续变动不阻断已封存回退、外来root/旧身份拒绝。
未实际替换默认程序、未追加GPU/HTTP/模型请求。此前最终封存不改写；
本目录的stage-after-delivery.py是收尾工具版本，不能冒充冻结驱动。

默认仍c16caf05/be4cd021，完整原证据见
[交付归档](../serve-admission-20261103/README.md)。
