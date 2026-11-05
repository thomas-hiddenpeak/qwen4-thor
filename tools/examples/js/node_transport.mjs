// Node-only, one-origin SDK Fetch adapter. No proxy, pool, redirect or retry.
import { constants, openSync, closeSync, fstatSync, readSync } from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import { isIP } from 'node:net';
import { Readable } from 'node:stream';
import { createSecureContext } from 'node:tls';
import { X509Certificate } from 'node:crypto';

function requireValue(ok, message) {
  if (!ok) throw new TypeError(message);
}

export function readCa(file) {
  requireValue(typeof file === 'string' && file.length > 0, 'explicit CA path required');
  const fd = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  let raw;
  try {
    const before = fstatSync(fd, { bigint: true });
    requireValue(before.isFile() && before.size > 0n && before.size <= 65536n, 'bounded CA file required');
    const buffer = Buffer.alloc(65537);
    let count = 0;
    while (count < buffer.length) {
      const size = readSync(fd, buffer, count, buffer.length - count, null);
      if (!size) break;
      count += size;
    }
    const after = fstatSync(fd, { bigint: true });
    requireValue(count === Number(before.size) && ['dev', 'ino', 'size', 'mtimeNs', 'ctimeNs']
      .every(key => before[key] === after[key]), 'CA file changed during read');
    const bytes = buffer.subarray(0, count);
    requireValue(bytes.every(value => value < 128), 'ASCII PEM CA bundle required');
    raw = bytes.toString('ascii');
  } finally { closeSync(fd); }
  const pattern = /-----BEGIN CERTIFICATE-----[\r\n]+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----/g;
  const blocks = raw.match(pattern) ?? [];
  requireValue(blocks.length > 0 && blocks.length <= 8 && !raw.replace(pattern, '').trim(), 'bounded PEM CA bundle required');
  try {
    requireValue(blocks.every(block => new X509Certificate(block).ca), 'CA certificate required');
    createSecureContext({ ca: raw, minVersion: 'TLSv1.2' });
  } catch { throw new TypeError('invalid CA certificate bundle'); }
  return raw; // Snapshot used for every request; no second file read.
}

export function nodeFetch(baseURL, { caFile, connectHost } = {}) {
  const origin = new URL(baseURL).origin;
  requireValue(!caFile || new URL(baseURL).protocol === 'https:', 'CA requires HTTPS');
  requireValue(connectHost === undefined || typeof connectHost === 'string' && isIP(connectHost), 'connection IP required');
  const ca = caFile === undefined ? undefined : readCa(caFile);
  return async (input, init = {}) => {
    const url = new URL(input);
    requireValue(url.origin === origin && !url.username && !url.password, 'foreign SDK request origin');
    requireValue(['GET', 'POST'].includes(init.method ?? 'GET'), 'unsupported SDK request method');
    requireValue(init.body == null || typeof init.body === 'string' || init.body instanceof Uint8Array,
      'bounded SDK request body required');
    const body = init.body == null ? undefined : Buffer.from(init.body);
    requireValue(!body || body.length <= 16 * 1024 * 1024, 'request byte limit');
    const headers = new Headers(init.headers);
    headers.set('host', url.host);
    headers.set('connection', 'close');
    if (body) headers.set('content-length', String(body.length));
    const lookup = connectHost === undefined ? undefined : (_name, options, callback) => {
      const family = isIP(connectHost);
      if (options?.all) callback(null, [{ address: connectHost, family }]);
      else callback(null, connectHost, family);
    };
    return new Promise((resolve, reject) => {
      const send = url.protocol === 'https:' ? https.request : http.request;
      const request = send(url, { method: init.method ?? 'GET', headers: Object.fromEntries(headers),
        agent: false, ca, minVersion: 'TLSv1.2', lookup, signal: init.signal }, response => {
        try {
          const fields = new Headers();
          for (let i = 0; i < response.rawHeaders.length; i += 2)
            fields.append(response.rawHeaders[i], response.rawHeaders[i + 1]);
          resolve(new Response(Readable.toWeb(response), {
            status: response.statusCode, statusText: response.statusMessage, headers: fields }));
        } catch (error) { response.destroy(); reject(error); }
      });
      request.on('error', reject);
      request.end(body);
    });
  };
}
