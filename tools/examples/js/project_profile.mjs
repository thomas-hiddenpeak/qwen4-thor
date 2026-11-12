// Pure business checks; execution stays in the existing bounded Python store.
import { requireValue } from './chat_client.mjs';

const keys = (value, expected) => Object.keys(value).length === expected.length &&
  expected.every(k => Object.hasOwn(value, k));
export function validateRead(args) {
  requireValue(keys(args, ['path', 'start_line', 'line_count']) && typeof args.path === 'string' &&
    [...args.path].length > 0 && [...args.path].length <= 512, 'read expects path/start_line/line_count');
  requireValue(Number.isSafeInteger(args.start_line) && args.start_line >= 1 && args.start_line <= 1000000 &&
    Number.isSafeInteger(args.line_count) && args.line_count >= 1 && args.line_count <= 80,
  'read range must be 1-based and at most 80 lines');
}
export function validateSearch(args) {
  const whitespace = '[\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]';
  const query = typeof args.query === 'string' ? args.query.replace(new RegExp('^'+whitespace+'+|'+whitespace+'+$', 'gu'), '') : '';
  requireValue(keys(args, ['query']) && [...query].length >= 1 && [...query].length <= 128 &&
    !args.query.includes('\n'), 'search expects a literal 1..128 query');
}
export const VALIDATORS = Object.freeze({ read_project_record: validateRead, search_project_docs: validateSearch });

export function parseCustomRead(value) {
  requireValue(typeof value === 'string' && value.length <= 1024, 'custom read input must be bounded text');
  const match = /^READ (docs\/[A-Za-z0-9_./-]+\.(?:md|json)) ([1-9][0-9]{0,5}) ([1-9][0-9]?)$/.exec(value);
  requireValue(match !== null && match[0] === value, 'expected READ path start_line line_count');
  const args = { path: match[1], start_line: Number(match[2]), line_count: Number(match[3]) };
  validateRead(args); return args;
}
export const CUSTOM_VALIDATORS = Object.freeze(Object.fromEntries(
  ['text', 'regex', 'lark'].map(syntax => ['read_project_record_' + syntax,
    value => { parseCustomRead(value); }])));
