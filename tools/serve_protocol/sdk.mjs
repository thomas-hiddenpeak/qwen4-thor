// Resolve only the explicitly installed, pinned SDK; no global fallback.
import { createRequire } from 'node:module';
import { readFileSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';

export function loadSdk(directory) {
  if (!directory) throw new Error('explicit SDK directory is required');
  const root = realpathSync(resolve(directory, 'node_modules/openai'));
  const require = createRequire(resolve(root, 'package.json'));
  const metadata = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
  if (metadata.version !== '7.47.0') throw new Error('SDK version drift');
  if (!require.resolve('openai').startsWith(root + '/')) throw new Error('SDK resolved outside pinned package');
  return { OpenAI: require('openai').OpenAI, version: metadata.version };
}
