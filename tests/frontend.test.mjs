import './frontend-loader.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { parseCommand, redactData, idForms } = await import('../frontend/src/lib/util.ts');
const { validate } = await import('../frontend/src/lib/rules.ts');
const { Runner } = await import('../frontend/src/lib/runner.ts');
const { Medals } = await import('../frontend/src/lib/medals.ts');

test('console preserves upstream free-text and quoted argument handling', () => {
  assert.deepEqual(parseCommand('say hello everyone'), ['say', 'hello everyone']);
  assert.deepEqual(parseCommand('tell "Player One" please join the red team'), ['tell', 'Player One', 'please join the red team']);
  assert.deepEqual(parseCommand('map "The Pit"'), ['map', 'The Pit']);
  assert.deepEqual(parseCommand('kick #3 reason with spaces'), ['kick', '#3', 'reason with spaces']);
  assert.throws(() => parseCommand('map "unclosed'), /quotation/);
});

test('player address redaction is recursive', () => {
  const raw = { nested: [{ ip: '203.0.113.44', text: 'Player joined from 203.0.113.44' }] };
  assert.ok(!JSON.stringify(redactData(raw, true)).includes('203.0.113.44'));
  assert.ok(JSON.stringify(redactData(raw, false)).includes('203.0.113.44'));
});

test('long player IDs resolve across decimal and hexadecimal representations', () => {
  const forms = idForms('12345678901234567890');
  assert.ok(forms.has(BigInt('12345678901234567890').toString(16)));
});

test('automation cannot execute delayed steps after its owner stops', async () => {
  let release;
  let waiting;
  const reached = new Promise((r) => { waiting = r; });
  const calls = [];
  const runner = new Runner({
    call: async (server, command, args) => { calls.push([server, command, args]); return { ok: true }; },
    discord: async () => 204, changed() {},
  }, () => 0, () => { waiting(); return new Promise((r) => { release = r; }); });
  runner.engine.connection('s', true, 'Server');
  runner.load([validate({ name: 'Delayed greeting', when: { type: 'join' },
    then: [{ type: 'wait', seconds: 5 }, { type: 'say', to: 'this', message: 'hello' }], cooldown: 0, per_hour: 60 })], {});
  runner.event('s', { event: 'join', name: 'A', player_id: '1' });
  await reached;
  runner.stop();
  release();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(calls, []);
});

test('automation enforces the original 20 chat lines per minute budget', async () => {
  const calls = [];
  const runner = new Runner({
    call: async (server, command, args) => { calls.push([server, command, args]); return { ok: true }; },
    discord: async () => 204, changed() {},
  }, () => 0, async () => {});
  runner.engine.connection('s', true, 'Server');
  runner.load([validate({ name: 'Greeting', when: { type: 'join' },
    then: [{ type: 'say', to: 'this', message: 'hello' }], cooldown: 0, per_hour: 600 })], {});
  for (let n = 0; n < 25; n++) runner.event('s', { event: 'join', name: 'A', player_id: String(n) });
  await new Promise((r) => setImmediate(r));
  assert.equal(calls.filter(([, command]) => command === 'say').length, 20);
});

test('observed kill feed produces the original double-kill medal', () => {
  const medals = new Medals();
  medals.kill('A', 'B', 10);
  const result = medals.kill('A', 'C', 12);
  assert.deepEqual(result, ['DOUBLE KILL']);
});

test('browser bridge refreshes its snapshot when handing setup progress over to the console', async () => {
  const old = { window: globalThis.window, location: globalThis.location, WebSocket: globalThis.WebSocket, fetch: globalThis.fetch };
  const streams = [], progress = [], batches = [], status = [];
  globalThis.window = new EventTarget();
  globalThis.location = new URL('http://127.0.0.1:8000');
  window.addEventListener('oni-web-status', (e) => status.push(e.detail.connected));
  class Socket {
    constructor(url) { this.url = String(url); streams.push(this); }
    close() { this.onclose?.({ code: 1000 }); }
    receive(payload) { this.onmessage?.({ data: JSON.stringify(payload) }); }
  }
  globalThis.WebSocket = Socket;
  let sent;
  globalThis.fetch = async (url, options) => {
    sent = { url, body: JSON.parse(options.body) };
    return new Response('{"ok":true}', { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  const bridge = await import('../frontend/src/web/bridge.ts');
  let offSetup, offConsole;
  try {
    offSetup = await bridge.listen('setup-line', (e) => progress.push(e.payload));
    streams[0].receive([{ kind: 'stream', generation: 1, instance: 'backend' }, { kind: 'setup-line', text: 'Ready' }]);
    assert.deepEqual(progress, ['Ready']);
    offConsole = await bridge.listen('oni', (e) => batches.push(e.payload));
    assert.equal(streams.length, 2);
    streams[1].onopen();
    streams[0].onclose({ code: 1006 });
    assert.equal(status.at(-1), true, 'an old socket cannot mark a new stream offline');
    streams[0].receive([{ kind: 'event', ev: { text: 'stale' } }]);
    assert.equal(batches.length, 0);
    streams[1].receive([{ kind: 'stream', generation: 1, instance: 'backend' }, { kind: 'link', state: 'online', index: 0 }]);
    assert.equal(batches[0][0].state, 'online');
    await bridge.invoke('call', { index: 0, command: 'status', args: [] });
    assert.equal(sent.url, '/api/rpc/call');
    assert.equal(sent.body._generation, 1);
    assert.ok(!streams[1].url.includes('game-box'));
  } finally {
    offSetup?.(); offConsole?.(); bridge.closeEvents();
    for (const [key, value] of Object.entries(old)) {
      if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
    }
  }
});
