# E4M3 修复接入验证

`run.py` 从当前生产源码逐字提取通用激活量化、MoE Gather/SwiGLU
量化和decode模板kernel；提取源码、头文件、测试工具、编译日志
与结果均留在新输出目录。编译零警告，实际在Thor运行。

```bash
python3 tools/verify/quant_integration/run.py \
  --output .q4t-work/my-quant-integration
```

参考采用CUDA原生饱和E4M3转换和独立FP4最近偶数码表。
130行覆盖32/128行尺度布局边界，七个正有限全局尺度覆盖高值、
次正规与零编码。核对通用输入、prefill输入/中间、decode输入/
中间的有效尺度字节、FP4字节及decode alpha，共341600个组。
SwiGLU使用相同设备数学表达式，**不独立验证非线性或GEMM**。
各专家测试尺度相同，不能据此宣称不同专家尺度寻址已验证。
未使用/填充字节不作比较，不充当越界检测或全部FP32穷举。

`--header OLD_HEADER --expect-failure`可用历史header作负对照。
失败预期只接受测试退出1，编译错误或CUDA错误不当成发现缺陷。
`--captures INPUT_BF16 GU_BF16`读取两个捕获文件前130行，分别为
2560列输入与1280列GU；仍使用七档受控尺度，**不是原请求完整重放**。
捕获文件被复制并绑定哈希；测试域仍限有限激活和正有限尺度。

`audit_scales.py --model PATH --output .q4t-work/scales.json`只读检查
checkpoint全部input_scale/weight_scale_2为标量F32、正有限；核对
存在的gate/up标量相等。它不证明运行时激活始终有限，不提供NaN
恢复策略；无效模型/运行时非有限值的fail-fast属于独立待办。

本次初版参考码表在大值上用FP32距离比较，发现饱和区距离分辨率
不足；已补充显式FP4饱和，再用同输入重跑新旧版本。初版失败和
构建错误保留在本地，不能将这些参考/工具错误归因于生产kernel。
