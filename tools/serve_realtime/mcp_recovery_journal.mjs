// Public development receipts never retain MCP bearer credentials.
import { check } from './conversation_checks.mjs';

export function publicEvent(event, direction, secrets) {
  const value = structuredClone(event);
  function visit(node) {
    if (!node || typeof node !== 'object') return;
    for (const [key, child] of Object.entries(node)) {
      if (direction === 'client' && key === 'authorization')
        node[key] = '<redacted>';
      else visit(child);
    }
  }
  visit(value);
  const raw = JSON.stringify(value);
  check(secrets.every((secret) => typeof secret === 'string' && secret.length >= 24 &&
    !raw.includes(secret)), 'credential in public receipt');
  return value;
}
