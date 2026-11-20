// Actual default-conversation request: VAD must leave side responses alone.
export function bargeInResponse(base) {
  return { ...base, conversation: 'auto', output_modalities: ['audio'],
    max_output_tokens: 3457,
    instructions: '用几句话介绍本地语音助手如何识别录音、查询资料和回答问题。',
    input: [{ type: 'message', role: 'user', id: 'speech_request',
      content: [{ type: 'input_text', text: '请介绍一下这些功能。' }] }],
  };
}
