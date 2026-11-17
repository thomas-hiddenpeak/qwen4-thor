# Silero VAD 参考与许可

原项目[snakers4/silero-vad](https://github.com/snakers4/silero-vad)，MIT，
Copyright (c) 2020-present Silero Team。原[许可](LICENSE)随项目保留。
固定revision `066409f3d9db45a30d21dfcbf9147826ffdd7052`。

`src/audio/vad.cpp`原生实现参考该版本`tinygrad_model.py`的16k架构：
右侧reflect STFT、4层卷积、LSTM及语音概率。没有链接tinygrad、
PyTorch或ONNX Runtime。原生权重由部署显式提供，失能只影响相应
音频能力；JIT仅用于开发CPU差分，不进入默认发行或运行依赖。

官方`silero_vad_16k.safetensors`1,239,748字节，
SHA256 `c59271c284ae9c8335d795d60e0bfdb71aaaceec578d9bd9ffc1b8153c319ea1`。
`tests/fixtures/silero_vad_header.json`为该实际资产1208字节头部。
固定来源、各文件摘要及取得记录保留在本项证据，权重不提交仓库。

计算组件正在开发，服务接入/恢复/发行仍待验收；声明不代替可用性。

注意：同版发行JIT与上述safetensors有14/15张量不同。实际数值
参考保留JIT计算图并显式载入固定safetensors；两种原文件不改写，
差异和最初FAILED记录均保留。组件通过不等于完整服务已交付。
