import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const file = new URL('../TerminalConnectionModel.js', import.meta.url);
const model = vm.createContext({});
if (existsSync(file)) vm.runInContext(readFileSync(file, 'utf8'), model);

function pending(room = 'room-a') { return model.begin(model.initial(), room); }
function ready() {
  const state = pending();
  return model.resolve(state, state.attempt, state.roomId, unified);
}

const unified = { ok: true, tty: { sessionName: 'terminal', agentName: 'term_8',
  sshTarget: 'alice@host', remoteBin: '/opt/bin/herdr', remoteSocket: '/run/herdr.sock', agentStarted: true } };

test('resolving a target activates the loader without claiming started or output', () => {
  assert.equal(typeof model.begin, 'function', 'connection state model is implemented');
  const initial = model.initial();
  const pending = model.begin(initial, 'room-a');
  const resolved = model.resolve(pending, pending.attempt, 'room-a', unified);
  assert.equal(initial.phase, 'idle');
  assert.equal(pending.phase, 'resolving');
  assert.equal(resolved.phase, 'resolved');
  assert.equal(model.loadable(resolved), true);
  assert.equal(resolved.started, false);
  assert.equal(resolved.outputObserved, false);
  assert.equal(model.interactive(resolved, false), false);
  assert.deepEqual(Array.from(model.argumentsFor(resolved.target)),
    ['terminal', 'term_8', 'alice@host', '/opt/bin/herdr', '/run/herdr.sock']);
});

test('attempts reject old replies including A to B to A and deactivation', () => {
  const a = pending();
  const b = model.begin(a, 'room-b');
  const again = model.begin(b, 'room-a');
  assert.equal(model.resolve(again, a.attempt, a.roomId, unified), again);
  assert.equal(model.resolve(b, a.attempt, a.roomId, unified), b);
  const stopped = model.deactivate(again);
  assert.equal(model.resolve(stopped, again.attempt, again.roomId, unified), stopped);
  assert.equal(model.loadable(stopped), false);
});

test('PTY lifecycle distinguishes spawn from output and never verifies the attachment', () => {
  assert.equal(typeof model.event, 'function');
  let state = ready();
  state = model.event(state, state.attempt, state.roomId, 'started');
  assert.equal(state.phase, 'waiting-output');
  assert.equal(model.interactive(state, false), false);
  state = model.event(state, state.attempt, state.roomId, 'output');
  assert.equal(state.phase, 'output');
  assert.equal(model.interactive(state, false), false, 'plain PTY output cannot grant input');
  assert.equal(model.interactive(state, false, false), false);
  assert.equal(model.interactive(state, false, 'true'), false, 'writer authority cannot be inferred from a truthy value');
  assert.equal(model.interactive(state, false, true), true, 'a future protected writer must explicitly pass its verified grant');
  assert.equal(model.interactive(state, true, true), false, 'a preview remains read-only even with a grant');
  assert.equal(model.interactive(state, true), false);
  assert.equal(model.status(state), 'Terminal output · attachment not verified');
  assert.equal(model.event(state, state.attempt, state.roomId, 'timeout'), state, 'no live idle timeout');
  const finished = model.event(state, state.attempt, state.roomId, 'finished');
  assert.equal(finished.phase, 'failed');
  assert.equal(model.loadable(finished), false);
  assert.equal(model.ownsInput(finished), true);
  assert.equal(model.event(finished, finished.attempt, finished.roomId, 'output'), finished);
});

test('startup timeout and stale session signals cannot revive a failed or newer attempt', () => {
  let state = ready();
  const old = state;
  state = model.event(state, state.attempt, state.roomId, 'timeout');
  assert.equal(state.phase, 'failed');
  assert.match(state.error, /startup timed out/);
  assert.equal(model.resolve(state, state.attempt, state.roomId, unified), state);
  state = model.begin(state, 'room-a');
  assert.equal(model.event(state, old.attempt, old.roomId, 'finished'), state);
  assert.equal(model.event(state, old.attempt, old.roomId, 'started'), state);
  assert.equal(model.ownsInput(state), true);
});

test('API failure and invalid targets fail closed; unbound is the only Desktop fallback', () => {
  const state = pending();
  for (const response of [{ ok: false, error: 'offline' }, { ok: true, tty: { agentError: 'busy' } },
      { ok: true, tty: {} }, { ok: true, tty: { sessionName: 'terminal', agentName: 'term_x' } }]) {
    const failed = model.resolve(state, state.attempt, state.roomId, response);
    assert.equal(failed.phase, 'failed');
    assert.equal(model.ownsInput(failed), true);
  }
  const unbound = model.resolve(state, state.attempt, state.roomId, { ok: true, tty: { awaitingBinding: true } });
  assert.equal(unbound.phase, 'unbound');
  assert.equal(model.ownsInput(unbound), false);
  assert.equal(model.loadable(unbound), false);
});

test('local unified and legacy selectors retain exact wrapper contracts', () => {
  const state = pending();
  const local = model.resolve(state, state.attempt, state.roomId, { ok: true, tty: {
    ...unified.tty, sshTarget: '', remoteBin: '/home/alice/bin/herdr', remoteSocket: '/home/alice/herdr.sock' } });
  assert.deepEqual(Array.from(model.argumentsFor(local.target)),
    ['terminal', 'term_8', '', '/home/alice/bin/herdr', '/home/alice/herdr.sock']);
  for (const selector of ['default', 'ares']) {
    const legacy = model.resolve(state, state.attempt, state.roomId, { ok: true, tty: {
      sessionName: selector, agentName: 'pane_4', agentStarted: true } });
    assert.equal(legacy.phase, 'resolved');
    assert.deepEqual(Array.from(model.argumentsFor(legacy.target)), [selector, 'pane_4']);
  }
});

test('peer observation accepts only an exact read-only peer descriptor and never owns a writer', () => {
  const state = pending('remote-room');
  const tty = {sessionName:'peer-observe',agentName:'remote-room',agentStarted:true,readOnly:true,transport:'peer'};
  const good = model.resolve(state,state.attempt,state.roomId,{ok:true,tty});
  assert.equal(good.phase,'resolved');
  assert.deepEqual(Array.from(model.argumentsFor(good.target)), ['remote-room']);
  const observed = model.event(model.event(good,good.attempt,good.roomId,'started'),good.attempt,good.roomId,'output');
  assert.equal(model.interactive(observed, false), false);
  assert.equal(model.interactive(observed, false, true), false, 'remote observation never becomes a writer');
  assert.equal(model.ownsInput(good),true);
  const readOnly = model.resolve(state,state.attempt,state.roomId,{ok:true,tty:{...unified.tty,readOnly:true}});
  const output = model.event(model.event(readOnly,readOnly.attempt,readOnly.roomId,'started'),readOnly.attempt,readOnly.roomId,'output');
  assert.equal(model.interactive(output, false, true), false, 'read-only targets reject a writer grant');
  for (const invalid of [{...tty,agentName:'other'}, {...tty,readOnly:false}, {...tty,transport:'ssh'}, {...tty,agentStarted:false}])
    assert.equal(model.resolve(state,state.attempt,state.roomId,{ok:true,tty:invalid}).phase,'failed');
});

test('an explicit SSH terminal is interactive shared access, not a peer observer', () => {
  const state = pending('remote-room');
  const shared = model.resolve(state, state.attempt, state.roomId, { ok: true, tty: {
    sessionName: 'terminal', agentName: 'term_remote', sshTarget: 'user@192.0.2.10',
    remoteBin: '/home/agent/.local/bin/herdr', remoteSocket: '/home/agent/.config/herdr/herdr.sock',
    transport: 'ssh', agentStarted: true
  }});
  assert.equal(shared.phase, 'resolved');
  assert.equal(shared.target.transport, 'ssh');
  const output = model.event(model.event(shared, shared.attempt, shared.roomId, 'started'), shared.attempt, shared.roomId, 'output');
  assert.equal(model.interactive(output, false, true), true);
  assert.deepEqual(Array.from(model.argumentsFor(shared.target)),
    ['terminal', 'term_remote', 'user@192.0.2.10', '/home/agent/.local/bin/herdr', '/home/agent/.config/herdr/herdr.sock']);
});
