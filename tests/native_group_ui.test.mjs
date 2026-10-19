import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const read = name => readFileSync(new URL('../' + name, import.meta.url), 'utf8');
const terminal = read('TerminalChat.qml');

test('native groups are not scalar terminal rooms', () => {
  const body = terminal.match(/readonly property bool terminalRoom: \{([^]*?)\n  \}/)[1];
  for (const source of ['hermes-native-group', 'hermes', 'ares', 'control']) {
    const ctx = vm.createContext({controller: {rooms: [{id: 'r', source}]}, roomId: 'r',
      nativeGroupRoom: source === 'hermes-native-group', orchestratorRoom: false});
    assert.equal(vm.runInContext('(function() {' + body + '})()', ctx),
      !['hermes-native-group', 'control'].includes(source));
  }
  const orchestrator = vm.createContext({controller: {rooms: [{id: 'r', source: 'hermes'}]},
    roomId: 'r', nativeGroupRoom: false, orchestratorRoom: true});
  assert.equal(vm.runInContext('(function() {' + body + '})()', orchestrator), false);
});

test('group send is rejected before draft mutation, uploads or transport', () => {
  const calls = [];
  const ctx = vm.createContext({selectedProject: {id: 'p'},
    selectedRoom: {id: 'g', source: 'hermes-native-group'}, sendingImage: false,
    roomText: 'keep my draft', pendingImages: [], roomSendKeys: {}, errorText: '',
    sendBotForRoom() { calls.push('bot'); return {id: 'default'}; },
    bridge: {request() { calls.push('request'); }, send() { calls.push('send'); }}});
  vm.runInContext(read('Panel.qml').match(/  function appendLocalEvent\([^]*?\n  \}/)[0], ctx);
  ctx.appendLocalEvent(false, false);
  assert.deepEqual(calls, []);
  assert.equal(ctx.roomText, 'keep my draft');
  assert.equal(ctx.sendingImage, false);
  assert.match(ctx.errorText, /group.*read.only/i);
});

test('native group renders discovery details, never legacy transcript', () => {
  assert.ok(/readonly property bool nativeGroupRoom:/.test(terminal));
  assert.match(terminal, /legacyTranscriptNeeded: active && !nativeGroupRoom/);
  assert.match(terminal, /active: terminalChat.active && terminalChat.nativeGroupRoom/);
  assert.match(terminal, /NativeGroupDetails\s*\{/);
  for (const [file, guard] of [['AresWorkspace.qml', 'workspace.selectedNativeGroup'],
                              ['ExtendedWorkspace.qml', 'extended.currentNativeGroup']]) {
    const source = read(file);
    assert.ok(source.includes('!'+guard), file + ' must hide group composers');
  }
});



test("native member attachment requires explicit selection and remains read-only", () => {
  const details = read("NativeGroupDetails.qml");
  assert.match(details, /signal memberSelected\(string memberKey\)/);
  assert.match(details, /onClicked: details.memberSelected\(/);
  assert.match(terminal, /selectedGroupMemberKey/);
  assert.match(terminal, /\/v1\/tty\/groups\//);
  assert.match(terminal, /readOnlyInput:.*nativeGroupRoom/);
  assert.match(terminal, /active: terminalChat.active && terminalChat.nativeGroupRoom && terminalChat.selectedGroupMemberKey === ""/);
});

test("group history is fetched only from the authenticated source and is paginated read-only", () => {
  const http = read("server/src/http.rs");
  const details = read("NativeGroupDetails.qml");
  assert.match(http, /\["v1", "groups", id, "history"\]/);
  assert.match(http, /crate::profile_yaml::load_default_profiles\(\)/);
  assert.match(details, /\/v1\/groups\//);
  assert.match(details, /function loadOlder\(/);
  assert.match(details, /oldestSeq/);
  assert.doesNotMatch(details, /onClicked:.*send|groups\.send/);
});

// Execute the actual QML handlers against properties with QML-compatible numeric coercion.
function historyHarness() {
  const source = read('NativeGroupDetails.qml');
  const type = source.match(/property (int|real) oldestSeq: 0/);
  assert.ok(type, 'oldestSeq must declare a QML numeric property');
  const requests = [];
  const details = {roomId: 'group-1', historyGeneration: 0, messages: [],
    hasOlder: false, loading: false, historyError: ''};
  let storedCursor = 0;
  Object.defineProperty(details, 'oldestSeq', {
    get() { return storedCursor; },
    set(value) { storedCursor = type[1] === 'int' ? value | 0 : Number(value); },
  });
  const context = vm.createContext({details, bridge: {request(method, url, body, callback) {
    requests.push({method, url, body, callback});
  }}, encodeURIComponent, qsTr: text => text});
  for (const key of ['roomId', 'historyGeneration', 'messages', 'oldestSeq', 'hasOlder',
                     'loading', 'historyError']) {
    Object.defineProperty(context, key, {
      get() { return details[key]; }, set(value) { details[key] = value; },
      configurable: true,
    });
  }
  for (const name of ['refreshHistory', 'loadOlder', 'loadPage']) {
    const handler = source.match(new RegExp('  function ' + name + '\\([^]*?\\n  \\}'));
    assert.ok(handler, `missing QML ${name}`);
    vm.runInContext(handler[0], context);
  }
  return {details, requests, context};
}

test('53-bit history cursor survives QML property storage and loads older pages exactly', () => {
  const {details, requests, context} = historyHarness();
  const high = 9007199254734608 + 100; // 39-bit fingerprint and 1-based index
  context.refreshHistory();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/v1/groups/group-1/history');
  requests[0].callback({ok: true, roomId: 'group-1', readOnly: true,
    oldestSeq: high, hasOlder: true,
    messages: [{seq: high, text: 'first'}, {seq: high + 1, text: 'second'}]});
  assert.equal(details.oldestSeq, high, 'QML property must preserve the exact cursor');
  context.loadOlder();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].url, `/v1/groups/group-1/history?before=${high}`);
  assert.doesNotMatch(requests[1].url, /e\+/i);
  requests[1].callback({ok: true, roomId: 'group-1', readOnly: true,
    oldestSeq: high - 2, hasOlder: false,
    messages: [{seq: high - 2, text: 'older'}, {seq: high - 1, text: 'middle'}]});
  assert.deepEqual(Array.from(details.messages, m => m.seq),
    [high - 2, high - 1, high, high + 1]);
  assert.equal(details.oldestSeq, high - 2);
  assert.equal(details.hasOlder, false);
  context.loadOlder();
  assert.equal(requests.length, 2, 'exhausted history must not request again');
});

test('older history rejects a non-decreasing cursor without changing visible messages', () => {
  const {details, requests, context} = historyHarness();
  const high = 9007199254734608 + 100;
  context.refreshHistory();
  requests[0].callback({ok: true, roomId: 'group-1', readOnly: true,
    oldestSeq: high, hasOlder: true, messages: [{seq: high, text: 'first'}]});
  context.loadOlder();
  requests[1].callback({ok: true, roomId: 'group-1', readOnly: true,
    oldestSeq: high, hasOlder: true, messages: [{seq: high, text: 'duplicate'}]});
  assert.deepEqual(Array.from(details.messages, m => m.text), ['first']);
  assert.equal(details.oldestSeq, high);
  assert.match(details.historyError, /unavailable/i);
});

test('older pages remain chronological and deduplicate overlapping sequence numbers', () => {
  const {details, requests, context} = historyHarness();
  const high = 9007199254734608 + 100;
  context.refreshHistory();
  requests[0].callback({ok: true, roomId: 'group-1', readOnly: true,
    oldestSeq: high, hasOlder: true,
    messages: [{seq: high, text: 'existing'}, {seq: high + 1, text: 'newer'}]});
  context.loadOlder();
  requests[1].callback({ok: true, roomId: 'group-1', readOnly: true,
    oldestSeq: high - 1, hasOlder: false,
    messages: [{seq: high - 1, text: 'older'}, {seq: high, text: 'overlap'}]});
  assert.deepEqual(Array.from(details.messages, m => [m.seq, m.text]),
    [[high - 1, 'older'], [high, 'existing'], [high + 1, 'newer']]);
  assert.equal(details.oldestSeq, high - 1);
  context.loadOlder();
  assert.equal(requests.length, 2);
});


test('second initialization refresh replaces an in-flight request and ignores its late callback', () => {
  const {details, requests, context} = historyHarness();
  context.refreshHistory(); // onRoomIdChanged
  context.refreshHistory(); // Component.onCompleted, before the first response
  assert.equal(requests.length, 2, 'latest generation needs its own request');
  requests[0].callback({ok: true, roomId: 'group-1', readOnly: true,
    oldestSeq: 10, hasOlder: true, messages: [{seq: 10, text: 'stale'}]});
  assert.equal(details.loading, true, 'stale response cannot finish current request');
  assert.equal(details.messages.length, 0);
  requests[1].callback({ok: true, roomId: 'group-1', readOnly: true,
    oldestSeq: 11, hasOlder: false, messages: [{seq: 11, text: 'current'}]});
  assert.equal(details.loading, false);
  assert.deepEqual(Array.from(details.messages, m => m.text), ['current']);
});


test('rapid group A to B to A keeps only the newest group response', () => {
  const {details, requests, context} = historyHarness();
  context.refreshHistory();
  details.roomId = 'group-2';
  context.refreshHistory();
  details.roomId = 'group-1';
  context.refreshHistory();
  assert.deepEqual(requests.map(r => r.url), [
    '/v1/groups/group-1/history', '/v1/groups/group-2/history',
    '/v1/groups/group-1/history']);
  const result = (roomId, text) => ({ok: true, roomId, readOnly: true,
    oldestSeq: 8, hasOlder: false, messages: [{seq: 8, text}]});
  requests[0].callback(result('group-1', 'old-A'));
  requests[1].callback(result('group-2', 'old-B'));
  assert.equal(details.loading, true);
  assert.equal(details.messages.length, 0);
  requests[2].callback(result('group-1', 'new-A'));
  assert.equal(details.loading, false);
  assert.deepEqual(Array.from(details.messages, m => m.text), ['new-A']);
});
