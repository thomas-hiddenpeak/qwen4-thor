// Bounded scenario wait, not a model completion inference from audio events.
import { check } from '../serve_realtime/conversation_checks.mjs';
export function clientHandoff(events, now, deadline) {
  check(Number.isFinite(now) && Number.isFinite(deadline), 'finite handoff deadline');
  const found = events.filter((e) => e.type === 'session.delegation.created');
  check(found.length <= 1, 'ambiguous client handoff');
  if (found.length) {
    check(found[0].delegation?.target === 'client' &&
      typeof found[0].delegation?.id === 'string' && found[0].delegation.id.length > 0,
    'client handoff identity');
    return found[0];
  }
  check(now < deadline, 'client delegation absent at bounded scenario deadline');
  return null;
}
