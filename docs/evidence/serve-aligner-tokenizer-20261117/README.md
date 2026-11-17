# 对齐tokenizer与原ASR回归

6项针对性CPU通过，1.414418秒、0GPU。对齐7组token序列与同版
官方Qwen2 tokenizer一致，含中英文音频模板及日/韩/数字文本；
`<timestamp>`固定151705且非special，skip-special仍保留它。
字面文字不能注入模板marker；配置漂移、误用原ASR profile、预算
失败和取消均有反例，失败保留原tokenizer owner。

原ASR官方16组golden和音频marker/词表padding两项回归通过。
本组检查的是tokenizer，不包括日/韩词单元切分、声学对齐计算或
服务时间戳准确性。后续实现和业务验收继续，默认rev276不变。

参考生成单独0.398秒，无模型加载或CUDA初始化；使用已固定开发
环境Transformers5.16.1/tokenizers0.23.2，源码与资产SHA见回执。
这些Python库不是产品运行依赖。
