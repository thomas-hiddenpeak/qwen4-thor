# 完整 4K MoE gate/up 边界与记录算法重放

仅用于已冻结原 4K HTTP 请求的离线诊断。生产二进制不改变；
`600440` 仍是已知错答，三侧一致不等于质量验收。

模板放入 `.q4t-work/prepared/moe-full-gu-20260924/` 后去掉 `.in`。
同名证据存在时不能覆盖，重跑必须整体改用新目录。

执行顺序：

1. `prepare.py` 读取 checkpoint，生成每个活跃专家的九段来源与摘要。
   模型只读，不复制大权重；这一步准备观测输入，不运行算术测试。
2. 观测器以 C++23、`-O2 -ffp-contract=off -Wall -Wextra`、CUDA
   include、`-shared -fPIC -ldl` 构建；空构建日志才继续。
3. `run.py` 首先做 `tools/evalscope` 真实 HTTP：before / observed /
   after，关闭 MTP。检查请求、输出、停止原因、退出码及观测完整性。
4. 三侧观测不干扰检查通过后，`analyze.py` 验证全部文件来源、
   实际行重排、权重、尺度及交接。它不宣称 GEMM 或 SwiGLU 算术正确。
5. `replay.cpp` 用同样编译选项，链接 cudart/cublasLt；零警告后，
   `run_replay.py` 重建实际 FP4 输入，以现场记录的算法逐专家重放。
   使用实际错误 SF，不能借此宣布输入量化正确。所有输出及差异保留。

6. `audit.py` 重读完整 manifest、全部重放输出，并补充旧末行文件
   对照。旧 manifest 未列末行输出，历史摘要保证的缺口必须保留。

有效 SF 独立重排，补齐的未使用行清零；实际观测的中间 SF padding
原样保存，不将 padding 解释为有效数据。原子生成的 token 行序可能
改变，必须用 `(token, slot)` 对应，不能假定两次顺序相同。

这里的重放复用 cuBLASLt；独立高精度 GU 算术、SwiGLU/中间量化、
down/shared/combine 完整链与整模型错答因果另行核对。
