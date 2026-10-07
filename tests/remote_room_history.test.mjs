import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';
const read = name => readFileSync(new URL('../' + name, import.meta.url), 'utf8');
const source = read('RemoteRoomHistory.qml');
function harness() {
  const calls = [];
  const view = {roomId:'remote-one', eligible:true, generation:0, rows:[], nextBefore:null,
    hasMore:false, loading:false, errorText:''};
  const context = vm.createContext({historyView:view, bridge:{available:true,
    request(method,url,body,callback) { calls.push({method,url,body,callback}); }}, encodeURIComponent,
    Number, Array, String});
  for (const key of Object.keys(view)) Object.defineProperty(context,key,{get(){return view[key]},set(v){view[key]=v}});
  for (const name of ['clearHistory','validCursor','cursorLess','loadHistory','loadOlder','requestPage']) {
    const method = source.match(new RegExp('  function ' + name + '\\([^]*?\\n  \\}'));
    assert.ok(method, name + ' missing'); vm.runInContext(method[0],context); view[name] = context[name];
  }
  return {view,calls,context};
}
const row = (cursor,text='hello') => ({role:'user',text,cursor});

test('explicit click fetches bounded chronological pages and never requests on instantiation', () => {
  const {view,calls,context} = harness();
  assert.equal(calls.length,0);
  context.loadHistory();
  assert.equal(calls[0].url,'/v1/rooms/remote-one/history');
  context.loadHistory(); assert.equal(calls.length,1);
  calls[0].callback({ok:true,messages:[row('10'),row('11')],next_before:'10',has_more:true});
  assert.equal(view.rows.length,2);
  context.loadOlder(); assert.equal(calls[1].url,'/v1/rooms/remote-one/history?before=10');
  calls[1].callback({ok:true,messages:[row('8'),row('9')],next_before:null,has_more:false});
  assert.deepEqual(Array.from(view.rows,x=>x.cursor),['8','9','10','11']);
  context.loadOlder(); assert.equal(calls.length,2);
});

test('exact i64 strings paginate beyond JS safe integers and reject rounded or malformed cursors', () => {
  const {view,calls,context} = harness();
  const max = '9223372036854775807', oldest = '9007199254740993';
  context.loadHistory();
  calls[0].callback({ok:true,messages:[row(oldest),row(max)],next_before:oldest,has_more:true});
  assert.deepEqual(Array.from(view.rows,x=>x.cursor),[oldest,max]);
  context.loadOlder();
  assert.equal(calls[1].url,'/v1/rooms/remote-one/history?before='+oldest);
  calls[1].callback({ok:true,messages:[row('9007199254740991'),row('9007199254740992')],next_before:null,has_more:false});
  assert.deepEqual(Array.from(view.rows,x=>x.cursor),['9007199254740991','9007199254740992',oldest,max]);
  for (const invalid of [9007199254740993,'01','0','9223372036854775808','+1','1.0']) {
    context.loadHistory();
    calls.at(-1).callback({ok:true,messages:[row(invalid)],next_before:null,has_more:false});
    assert.equal(view.rows.length,0, String(invalid));
    assert.match(view.errorText,/unavailable/i);
  }
  context.loadHistory();
  calls.at(-1).callback({ok:true,messages:[row('9')],next_before:'9',has_more:true});
  view.nextBefore='01';
  const count = calls.length;
  context.loadOlder();
  assert.equal(calls.length,count);
});

test('room switch, close, offline and failed or malformed pages erase private rows', () => {
  const {view,calls,context} = harness();
  context.loadHistory(); calls[0].callback({ok:true,messages:[row('10')],next_before:'10',has_more:true});
  context.loadOlder(); context.clearHistory();
  assert.equal(view.rows.length,0);
  calls[1].callback({ok:true,messages:[row('9')],next_before:null,has_more:false});
  assert.equal(view.rows.length,0);
  context.loadHistory(); calls[2].callback({ok:true,messages:[row('10')],next_before:'10',has_more:true});
  context.loadOlder(); calls[3].callback({ok:false,error:'revoked'});
  assert.equal(view.rows.length,0);
  assert.match(view.errorText,/unavailable/i);
  context.loadHistory(); calls[4].callback({ok:true,messages:[row('10','x'.repeat(501))],next_before:null,has_more:false});
  assert.equal(view.rows.length,0);
  assert.match(view.errorText,/unavailable/i);
  view.eligible=false; context.loadHistory(); assert.equal(calls.length,5);
});

test('QML wiring only exposes user-driven selected remote history and no input', () => {
  const terminal = read('TerminalChat.qml');
  const workspace = read('AresWorkspace.qml');
  assert.match(terminal,/active: historyPanel\.opened/);
  assert.match(terminal,/modal: true/);
  assert.match(terminal,/RemoteRoomHistory\s*\{/);
  assert.match(workspace,/selectedTerminal\.historyOpen = true/);
  assert.match(terminal,/onRoomIdChanged:[^]*?historyOpen = false/);
  assert.match(source,/onRoomIdChanged: clearHistory\(\)/);
  assert.match(source,/onEligibleChanged: if \(!eligible\) clearHistory\(\)/);
  assert.doesNotMatch(source,/\bPOST\b|onClicked:.*send|Component\.onCompleted:.*loadHistory/);
  assert.match(source,/textFormat: Text\.PlainText/);
  assert.match(source,/model: historyView\.rows/);
});
