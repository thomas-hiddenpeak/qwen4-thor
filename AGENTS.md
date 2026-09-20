# AGENTS.md — Qwen4-Thor 开发入口

任何 agent 进入本项目, 先读本文, 再按需要读 docs/ 下对应文档。
**代码是最高事实来源**; 文档与代码不一致时, 以代码为准并修正文档。

## 项目一句话

在 Jetson AGX Thor (SM110a) 上用 C++23 (host + device, CUDA 13.3) 实现
Qwen3.8-Flash-Next (qwen4_exp) 的原生推理引擎,
核心特性是 PLE SSD Stream (51.2 GB FP8 查找表从 NVMe 异步流式读取)。

## 文档导航

| 想了解什么 | 读哪个文档 |
|---|---|
| 当前进展到哪了、下一步做什么 | [docs/STATUS.md](docs/STATUS.md) |
| 整体架构设计 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 分阶段计划与范围 | [docs/PHASES.md](docs/PHASES.md) |
| 开发日志 (按日期分文件, 时间倒序) | [docs/log/](docs/log/README.md) |
| 已完成 / 已解决归档 | [docs/DONE.md](docs/DONE.md) |
| 目标模型架构细节 (qwen4_exp) | [docs/MODEL.md](docs/MODEL.md) |
| 参考项目说明 | [docs/REFERENCE.md](docs/REFERENCE.md) |
| 文档总索引 | [docs/README.md](docs/README.md) |

## 硬性约定

1. **模型目录只读**。模型位于
   `~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream`,
   绝不写入、移动、修改。
2. **构建产物只进 `build/` 或 `.q4t-work/`** (已被 .gitignore 排除)。
3. **reference/ 目录只读**, 存放外部参考项目源码, 不参与构建,
   不修改其内容。
4. **代码风格**: Google C++ Style, 2 空格缩进, 80 列, 指针靠左
   (见 .clang-format)。命名: PascalCase 类型/方法, lower_snake_case
   局部变量, 成员变量尾下划线 (`member_`), I 前缀接口。
5. **编译零警告**: `-Wall -Wextra`。
6. **每次有意义的改动后**, 更新 `docs/STATUS.md` 并追加一条开发日志到
   当天 `docs/log/<日期>.md` 顶部 (不存在则新建并在 `docs/log/README.md`
   索引登记); 每条: 日期、做了什么、为什么、下一步。日志只追加不改历史。
7. **验证以真实环境为准**: 开发过程直接在本机 Thor 上构建、运行、
   测试, 不假设 CI 环境。

## 快速上手

```bash
# 构建 (要求: CMake >= 4.0 [pip 装 4.4.3] + g++-14 [apt]; C++23 host+device)
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_CUDA_HOST_COMPILER=g++-14 \
  -DQ4T_CUDA_ARCHITECTURES=110a
cmake --build build --parallel

# 运行（默认关闭 MTP；评估参数与顺序见 docs/EVALUATION.md）
./build/q4t serve --max-seq 1 --max-prefill 8192 --max-len 208896
```

## 当前评估约定（2026-09-20 用户更新）

先读 docs/STATUS.md 和 docs/EVALUATION.md。改动后第一项测试必须是
tools/evalscope HTTP E2E；不运行任何前置单测、微测试、bench 或 profile。
E2E 通过后才细分析。必要构建不属于测试。基线 MTP 关闭，单流五档上下文，
保持精度与各档性能；复杂度下降且性能持平也接受。规则覆盖下文旧流程。

## 当前状态入口

当前基线与待处理问题只维护在 [docs/STATUS.md](docs/STATUS.md)，不在本文件
重复记录吞吐、完成状态或优化路线。进入项目先读状态，再按用途读其他文档。
旧状态段保存在 [历史入口快照](docs/HISTORY_AGENT_ENTRY_2026-09-20.md)，
其中的旧测试顺序、性能结论和“已闭合”不能替代当前 E2E 验收。
