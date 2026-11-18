# Chat音频输入已合入主线

[PR #47](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/47)于
2026-11-18合入main `0f809a0c66530248b8ea7edb5ec328d22262edfe`。
接受HEAD `90ffceb053d6c840838370c594a39a3a4d8d2e1f`的PR与push
公共合同全部通过，包含零警告构建、输入/资源合同、音频资格恢复、
版本保留与来源检查。原CI失败及修复记录保留。

实际Chat音频输入和恢复业务复用rev306已有验收及可回退交付，
本次主线收尾0新增GPU；尚未验收的TTS计算改动未混入PR47。
A10全部音频/Realtime等剩余工作仍推进，总目标仍active。
