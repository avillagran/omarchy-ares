import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';
const read = name => readFileSync(new URL('../' + name, import.meta.url), 'utf8');
const qml = read('TerminalChat.qml');
const history = read('RemoteRoomHistory.qml');

test('local Hermes history is a selected, explicit read-only fallback, never a new terminal', () => {
  assert.match(qml, /readonly property bool localHermesRoom: !!roomRecord && roomRecord.source === "hermes" && !remotePeerRoom/);
  assert.match(qml, /modal: true/);
  assert.match(qml, /active: historyPanel.opened/);
  assert.match(qml, /terminalChat.remotePeerRoom \|\| terminalChat.localHermesRoom/);
  assert.match(qml, /readonly property bool terminalOwnsInput: targetResolved/);
  assert.match(qml, /readonly property bool terminalInteractive:[\s\S]*?!localHermesRoom/);
  assert.match(qml, /onRoomIdChanged:[^]*?historyOpen = false/);
  assert.match(qml, /openIfMissing: !!\(terminalChat\.remotePeerRoom \|\| terminalChat\.localHermesRoom\) && !terminalChat\.previewReadOnly/);
  assert.doesNotMatch(history, /\bPOST\b|Component\.onCompleted:.*loadHistory/);
  assert.match(history, /textFormat: Text.PlainText/);
});

test('local history uses bounded GET only on click and clears on unavailable or room switch', () => {
  const calls = [];
  const view = {roomId:'local-one', eligible:true, generation:0, rows:[], nextBefore:null,
    hasMore:false, loading:false, errorText:''};
  const context = vm.createContext({historyView:view, bridge:{available:true,
    request(method,url,body,callback) { calls.push({method,url,body,callback}); }}, encodeURIComponent,
    Number, Array, String});
  for (const key of Object.keys(view)) Object.defineProperty(context,key,{get(){return view[key]},set(v){view[key]=v}});
  for (const name of ['clearHistory','validCursor','cursorLess','loadHistory','loadOlder','requestPage']) {
    const method = history.match(new RegExp('  function ' + name + '\\([^]*?\\n  \\}'));
    assert.ok(method, name + ' missing'); vm.runInContext(method[0],context); view[name] = context[name];
  }
  assert.equal(calls.length,0);
  context.loadHistory();
  assert.deepEqual(calls[0].method,'GET');
  assert.equal(calls[0].url,'/v1/rooms/local-one/history');
  assert.equal(calls[0].body,null);
  calls[0].callback({ok:true,messages:[{role:'user',text:'hello',cursor:'10'}],next_before:'10',has_more:true});
  context.loadOlder();
  assert.equal(calls[1].url,'/v1/rooms/local-one/history?before=10');
  calls[1].callback({ok:false,error:'source hidden'});
  assert.equal(view.rows.length,0);
  assert.match(view.errorText,/unavailable/i);
  context.loadHistory();
  view.roomId='other'; context.clearHistory();
  calls[2].callback({ok:true,messages:[{role:'user',text:'stale',cursor:'10'}],next_before:null,has_more:false});
  assert.equal(view.rows.length,0);
});

test('composer follows terminal ownership: hidden only while a terminal target owns the room', () => {
  const expression = qml.match(/readonly property bool terminalOwnsInput: ([^\n]*)/);
  assert.ok(expression);
  const unbound = vm.createContext({targetResolved:false});
  assert.equal(vm.runInContext(expression[1],unbound),false);
  const resolved = vm.createContext({targetResolved:true});
  assert.equal(vm.runInContext(expression[1],resolved),true);
  const compact = read('AresWorkspace.qml');
  const expanded = read('ExtendedWorkspace.qml');
  assert.match(compact,/visible: controller.selectedRoom !== null && !workspace.selectedNativeGroup\s*&& \(!workspace.selectedTerminalOwnsInput \|\| workspace.editMode\)/);
  assert.match(compact,/visible: controller.selectedRoom !== null && !workspace.selectedNativeGroup\s*\n?\s*spacing: Style.space\(6\)/);
  assert.match(expanded,/visible: controller.editMode \|\| !extended.currentTerminalOwnsInput/);
  assert.match(expanded,/readonly property bool currentTerminalOwnsInput: controller.extendedViewMode === "grown"\s*\n?\s*\? masterTile.terminalOwnsInput/);
});

test('source refusal offers a clear safe reason without displaying raw error contents', () => {
  const error = 'History unavailable at source (sign in, hidden session, or source offline).';
  assert.ok(history.includes(error));
});
