# 视觉应用后续收口结果

2026-11-07。[冻结计划](VISION_CLOSEOUT_PLAN_2026-11-07.md)内完成限定
应用交付；默认 ELF ea1b474d、sidecar 3eb46385、协议 91b619c9 不变。
没有证据要求改变视频时序、视觉数值或结构化解码算法。
机器摘要见[审计结果](evidence/vision-closeout-20261107/result.json)。

## 视频方向：输入理解与枚举语义分开接受

Phase A复制上一轮四个失败请求，仅换请求身份与response_format；媒体
字节/时间戳、问题、greedy、256输出预算和原JSON/SSE模式不变。CPU和
实际HTTP preflight核对展开token相同。方向真值独立从红色物体像素
质心与实际时间顺序计算，未取模型答案为真值。

| 输入 | 真值 | 原strict复合枚举 | 同题JSON/自由回答 | 自然枚举 | 显式schema含义 |
|---|---|---|---|---|---|
| 正序8帧 | 向右 | right_to_left，错误 | right，正确 | right，正确 | left_to_right，正确 |
| 逆序8帧 | 向左 | left_to_right，错误 | left，正确 | left，正确 | right_to_left，正确 |
| 奇数3帧 | 向右 | right_to_left，错误 | right，正确 | right，正确 | left_to_right，正确 |
| H.264经CPU采样 | 向右 | right_to_left，错误 | right，正确 | right，正确 | left_to_right，正确 |

源码 `StructuredDecoder::Select` 对合法token做greedy选择，schema本身
不进入聊天提示。模型偏好的自然方向词right/left，与复合枚举的**起点**
前缀含义相反；只剩下合法后缀时会形成反向答案。这是代码与同题对照
支持的解释，没有完整候选序列概率证据，不声称排除其他模型敏感性。
不能倒转帧或事后翻转答案来“修复”。Schema语法正确也不等于任务正确。

Phase B改自然方向枚举4/4；应用明确展示原复合枚举含义后4/4。后二者
是独立的新请求合同，**原四个请求仍未通过**。覆盖同一简单红圆运动
系列，不扩为任意视频、复杂动作或stationary质量保证。

新增可选 `vision_query --schema-file`：同一schema送约束后端，并加入
用户提示以传达字段含义。已有不带schema的行为保持，拒绝覆盖已有
response_format，限制schema文件/序列化大小；不自动重试或改写响应。

```bash
python3 -m tools.examples.vision_query --video /path/clip.mp4 --frames 8 \
  --question 'Which direction does the red circle move over the video? Return JSON direction.' \
  --schema-file tools/examples/vision_direction.schema.json
```

该schema适合单一被询问物体、明确图像坐标的水平运动。输入应先确认
实际帧时间顺序；新视频仍需以业务真值验收。

## 真实资料与工具闭环

从已提交的视觉v1 `result.json` 渲染实际总量表和阶段柱图：启动尝试6、
Chat尝试70、控制67、原生5；选定三阶段Chat为24/13/5。表格、图表和
多图读取3/3正确。这是**真实记录的程序渲染**，不是自然扫描文档。

模型发出真实 `read_project_record` 调用，读取该记录1～12行，返回
完整文件SHA和精确引用。最终回答256token不足，服务器按合同返回
502 `structured_output_incomplete`，未发布伪成功。保留A整轮FAILED。
B只提高最终回答预算到768，沿用真实工具消息、原问题和媒体，不重复
执行工具；289token完整结束，四项图中值/文件值全部一致且引用准确。
审计逐项核对数值与真实源文本，不只查找答案中的某个数字。

## 验证、资源与证据

- 16项不同CPU合同（6退出/预算、3对照/累计预算、7视觉客户端）及
  3项审计负控通过。首次准备的32位取消令牌被CPU preflight拒绝，已修
  成既有64位合同；原 `prepare-failed-01` 保留，0模型/HTTP/GPU。
- A为1启动17Chat17控制、93.997秒租约，工具最终回答失败；B为
  1启动9Chat13控制、67.825秒租约，全部通过。合计**2启动、26Chat、
  30控制、0原生、161.821秒租约**，小于冻结总预算。不补足剩余预算。
- 两轮监控共150次，无冲突/不可读进程；两自有模型身份waitable退出、
  reap与最终GPU空、退出后的guard观察核对。原custody ack基于注册后
  采样，时间早于waitable；不把它称为退出后ack，另查真实退出后采样。
  轮询仍不能证明不存在短于采样间隔的干扰。
- 4720阶段文件绑定、4366 Git源码绑定、原始HTTP/SSE/错误、提示token、
  媒体像素和真实工具文件重新核对；原件放
  `.q4t-work/vision-closeout-20261107/originals/`，按内容SHA保存。
  [绑定索引](evidence/vision-closeout-20261107/bindings.json)供本机复核。
- 241运行来源与默认/回退包封印不变，复用上一轮HF完整塔、文本/MTP、
  治理等未变身份的证据。本轮没有推理构建、默认替换或性能优化接受。
- 收尾检查两自有PID消失、18146无监听、NVML空、共享锁可用；未安装
  常驻服务或自动恢复任务。不恢复vLLM，不迁移旧托管/任务/Key/day包。

旧四请求失败、JPEG非逐像素一致、Jetson硬解码未接受与用户长稳边界
保持。全局 `media_acceptance:false`、`experimental:true` 保守声明不变。
可按[使用矩阵](VISION_SERVICE_V1.md)进入受控图片业务和已验明确语义
请求；完整视觉生产准确率与长期稳定性仍不能由这些有限用例推出。
