# 数值诊断阶段1（2026-10-16）

生产源码未改，q4t仍使用26639190711e冻结binary；只扩展诊断测试。
原Part C及其0.02阈值保留，当前测试仍FAIL，不作为五项收口完成。

- 新Part F不保存/恢复checkpoint，只执行已接受前缀。a=0/1的完整
  recurrent state分别与checkpoint0/1逐位相同，提供独立执行参考。
- partial1相对prefill参考的第一处已采样差异为layer0 beta投影head41：
  输入x/QKV/z/a逐位相同，beta为0x4036与0x4035，相差1 BF16 ULP。
- 原始只读权重及相同输入的高精度点积2.8359382581111276522；两输出
  中点2.8359375，差7.5811112765e-7。partial的2.84375比prefill的
  2.828125更接近该结果。不能把prefill输出自动作为更准确的oracle。
- 拒绝与全接受样例的已采样张量逐位相同。partial在后续attention/
  MoE放大；这尚不证明整个放大过程或全部其他样例正确。

逐层capture仅存本机.q4t-work原路径，manifest给出摘要，不复制模型权重。
下一步：建立输出边界的相同算术路径独立参考，验证舍入差异的因果，
再处理B=3 prefill与旧MTP API；不放宽阈值、不重跑未变化的性能矩阵。
