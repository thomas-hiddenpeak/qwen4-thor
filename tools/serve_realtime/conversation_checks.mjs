// Assertions shared by the real SDK actor and its negative CPU fixtures.
export function check(ok, message) {
  if (!ok) throw new Error(message);
}
export function terminal(events, responseId, status) {
  const matching = events.filter((e) => e.type === 'response.done' &&
    e.response?.id === responseId);
  check(matching.length === 1, 'exactly one response terminal required');
  const response = matching[0].response;
  check(response.status === status, `response status: ${response.status}`);
  return response;
}
export function usage(response) {
  const value = response.usage;
  check(value && ['input_tokens', 'output_tokens', 'total_tokens'].every(
    (key) => Number.isSafeInteger(value[key]) && value[key] >= 0),
  'actual usage required');
  check(value.input_tokens > 0 && value.output_tokens > 0 &&
    value.total_tokens === value.input_tokens + value.output_tokens,
  'actual usage arithmetic');
  return value.total_tokens;
}
export function text(response) {
  check(Array.isArray(response.output), 'actual output required');
  return response.output.flatMap((item) => item.type === 'message' ?
    (item.content || []).filter((part) => part.type === 'output_text')
      .map((part) => part.text) : []).join('');
}
export function functionCall(response) {
  check(response.status === 'completed', 'incomplete tool is not executable');
  const calls = response.output.filter((item) => item.type === 'function_call');
  check(calls.length === 1 && calls[0].status === 'completed' &&
    calls[0].name === 'lookup_project_record' &&
    typeof calls[0].call_id === 'string' && calls[0].call_id.length > 0,
  'actual completed function required');
  const value = JSON.parse(calls[0].arguments);
  check(value && Object.keys(value).length === 1 && value.record_key === 'release',
    'tool arguments must select the real release record');
  return calls[0];
}
export function streamAgrees(events, response) {
  const parts = events.filter((event) => event.type === 'response.output_text.delta' &&
    event.response_id === response.id).map((event) => event.delta).join('');
  check(parts === text(response), 'actual text deltas differ from terminal');
  const calls = response.output.filter((item) => item.type === 'function_call');
  for (const item of calls) {
    const deltas = events.filter((event) =>
      event.type === 'response.function_call_arguments.delta' &&
      event.response_id === response.id && event.item_id === item.id);
    check(deltas.map((event) => event.delta).join('') === item.arguments,
      'actual function arguments differ from terminal');
  }
}
