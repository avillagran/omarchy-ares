import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../AresBridge.qml', import.meta.url), 'utf8');
const requestSource = source.match(/^  function request\(method, path, body, callback\) \{[\s\S]*?^  \}/m);
assert.ok(requestSource, 'AresBridge request function exists');

function harness() {
  let xhr;
  let reconnects = 0;
  let connectionEvents = 0;
  class XMLHttpRequest {
    static DONE = 4;
    constructor() { xhr = this; this.readyState = 0; this.status = 0; this.responseText = ''; this.headers = {}; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    send(body) { this.body = body; }
  }
  const bridge = {available: true, endpoint: 'http://127.0.0.1:9420', token: 'fixture-token', error: ''};
  const reconnect = {restart() { reconnects++; }};
  const context = vm.createContext({bridge, XMLHttpRequest, encodeURIComponent, reconnect,
    connectionChanged() { connectionEvents++; }});
  for (const key of ['available', 'endpoint', 'token']) {
    Object.defineProperty(context, key, {get: () => bridge[key], set: () => { throw new Error('global ' + key + ' write'); }});
  }
  // No writable global is available to asynchronous XHR handlers.
  vm.runInContext('"use strict";\n' + requestSource[0], context);
  const received = [];
  context.request.call(bridge, 'GET', '/v1/rooms/fixture/history', null, value => { received.push(value); });
  // QML may invalidate an ID lookup during an asynchronous callback.
  context.bridge = null;
  return {xhr, bridge, received, context, latestXhr: () => xhr, reconnects: () => reconnects, connectionEvents: () => connectionEvents};
}

test('successful XHR finishes callback and clears the bridge error', () => {
  const {xhr, bridge, received} = harness();
  bridge.error = 'stale';
  xhr.readyState = 4;
  xhr.status = 200;
  xhr.responseText = '{"ok":true,"messages":[]}';
  xhr.onreadystatechange();
  assert.equal(bridge.error, '');
  assert.equal(received.length, 1);
  assert.equal(received[0].ok, true);
});

test('failed HTTP XHR finishes callback and records bridge error', () => {
  const {xhr, bridge, received} = harness();
  xhr.readyState = 4;
  xhr.status = 503;
  xhr.responseText = '{"ok":false,"error":"fixture unavailable"}';
  xhr.onreadystatechange();
  assert.equal(bridge.error, 'fixture unavailable');
  assert.equal(received.length, 1);
  assert.equal(received[0].error, 'fixture unavailable');
});

test('network XHR finishes callback and marks bridge unavailable', () => {
  const {xhr, bridge, received, reconnects, connectionEvents} = harness();
  xhr.onerror();
  assert.equal(bridge.available, false);
  assert.equal(received.length, 1);
  assert.equal(received[0].error, 'Ares service connection lost');
  assert.equal(reconnects(), 1);
  assert.equal(connectionEvents(), 1);
});

test('a finished request cannot mutate state after a subsequent request', () => {
  const {xhr: first, bridge, received, context, latestXhr, reconnects, connectionEvents} = harness();
  first.readyState = 4;
  first.status = 200;
  first.responseText = '{"ok":true}';
  first.onreadystatechange();
  assert.equal(received.length, 1);
  const nextReplies = [];
  context.request.call(bridge, 'GET', '/next', null, value => nextReplies.push(value));
  const next = latestXhr();
  assert.notEqual(next, first);
  first.onerror();
  assert.equal(bridge.available, true);
  assert.equal(reconnects(), 0);
  assert.equal(connectionEvents(), 0);
  assert.equal(received.length, 1);
  next.readyState = 4;
  next.status = 200;
  next.responseText = '{"ok":true}';
  next.onreadystatechange();
  assert.equal(nextReplies.length, 1);
});

test('a network failure followed by DONE cannot trigger reconnect twice', () => {
  const {xhr, received, reconnects, connectionEvents} = harness();
  xhr.onerror();
  xhr.readyState = 4;
  xhr.status = 0;
  xhr.onreadystatechange();
  assert.equal(received.length, 1);
  assert.equal(reconnects(), 1);
  assert.equal(connectionEvents(), 1);
});

test('401 marks the owner unavailable and invokes callback exactly once', () => {
  const {xhr, bridge, received, reconnects, connectionEvents} = harness();
  xhr.readyState = 4;
  xhr.status = 401;
  xhr.responseText = '{"ok":false,"error":"unauthorized"}';
  xhr.onreadystatechange();
  assert.equal(bridge.available, false);
  assert.equal(bridge.error, 'unauthorized');
  assert.deepEqual(received.map(value => value.error), ['unauthorized']);
  assert.equal(reconnects(), 1);
  assert.equal(connectionEvents(), 1);
});
