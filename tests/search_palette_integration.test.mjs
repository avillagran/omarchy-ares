import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const read = name => readFileSync(new URL('../' + name, import.meta.url), 'utf8');
const panel = read('Panel.qml');
function harness() {
  const calls = [], deferred = [];
  const focus = {visible: true, enabled: true, forceActiveFocus() { calls.push('focus'); }};
  const ctx = vm.createContext({opened: true, searchOpen: false, searchReturnFocus: null,
    searchShortcut: 'Ctrl+Space', pendingSearchShortcut: '', searchPreferenceInFlight: false,
    errorText: '', extendedMode: false,
    rooms: [{id: 'a', name: 'Alpha'}, {id: 'b', name: 'Beta'}],
    keyCatcher: {Window: {window: {activeFocusItem: focus}}},
    Qt: {callLater(fn) { deferred.push(fn); }},
    selectRecentRoom(room) { calls.push(['select', room.id]); },
    focusExtendedRoom(room) { calls.push(['extended', room.id]); },
    bridge: {request(...args) { calls.push(args); }}});
  ctx.root = ctx;
  for (const name of ['openSearch', 'closeSearch', 'toggleSearch', 'activateSearchRoom',
    'applySearchPreference', 'setSearchShortcut', 'persistSearchShortcut']) {
    const match = panel.match(new RegExp('  function ' + name + '\\([^]*?\\n  }'));
    if (match) vm.runInContext(match[0], ctx);
  }
  return {ctx, calls, deferred};
}

test('opening search is local and cancel restores focus without navigation or transport', () => {
  const {ctx, calls, deferred} = harness();
  assert.equal(typeof ctx.openSearch, 'function');
  ctx.openSearch();
  assert.equal(ctx.searchOpen, true);
  assert.deepEqual(calls, []);
  ctx.closeSearch(true); deferred.forEach(fn => fn());
  assert.equal(ctx.searchOpen, false);
  assert.deepEqual(calls, ['focus']);
  ctx.opened = false; ctx.openSearch();
  assert.equal(ctx.searchOpen, false);
});

test('activation revalidates room identity and navigates through stable controller', () => {
  const {ctx, calls} = harness();
  assert.equal(typeof ctx.activateSearchRoom, 'function');
  ctx.openSearch(); ctx.activateSearchRoom({id: 'a'});
  assert.deepEqual(calls, [['select', 'a']]);
  assert.equal(ctx.searchOpen, false);
  ctx.extendedMode = true; ctx.openSearch(); ctx.activateSearchRoom({id: 'b'});
  assert.deepEqual(calls.at(-1), ['extended', 'b']);
  ctx.openSearch(); ctx.activateSearchRoom({id: 'deleted'});
  assert.equal(ctx.searchOpen, true);
  assert.equal(calls.length, 2);
});

test('search from every non-home section reveals the selected terminal surface', () => {
  for (const section of ['control', 'preferences', 'peers', 'archived']) {
    for (const extended of [false, true]) {
      const {ctx} = harness();
      ctx.section = section;
      ctx.extendedMode = extended;
      ctx.projects = [{id: 'project', node: 'local'}];
      ctx.rooms[1].projectId = 'project';
      ctx.markRoomSeen = () => {};
      ctx.setHostExpanded = () => {};
      ctx.setProjectExpanded = () => {};
      for (const name of ['selectRoom', 'selectRecentRoom', 'focusExtendedRoom']) {
        const match = panel.match(new RegExp('  function ' + name + '\\([^]*?\\n  }'));
        assert.ok(match, name);
        vm.runInContext(match[0], ctx);
      }
      ctx.openSearch(); ctx.activateSearchRoom({id: 'b'});
      assert.equal(ctx.section, 'home', section);
      assert.equal(ctx.selectedRoomId, 'b', section);
      assert.equal(ctx.searchOpen, false, section);
      if (extended) {
        assert.equal(ctx.extendedRoomId, 'b', section);
        assert.equal(ctx.extendedViewMode, 'focus', section);
      }
    }
  }
});

test('a deferred cancel cannot steal focus from a reopened search or a closed app', () => {
  for (const action of ['reopen', 'hide']) {
    const {ctx, calls, deferred} = harness();
    assert.equal(typeof ctx.openSearch, 'function');
    ctx.openSearch(); ctx.closeSearch(true);
    if (action === 'reopen') ctx.openSearch(); else ctx.opened = false;
    deferred.forEach(fn => fn());
    assert.deepEqual(calls, []);
  }
});

test('shortcut writes are serialized and stale snapshots cannot overwrite pending selection', () => {
  const {ctx, calls} = harness();
  assert.equal(typeof ctx.setSearchShortcut, 'function');
  ctx.setSearchShortcut('Alt+Space');
  assert.equal(ctx.searchShortcut, 'Alt+Space');
  assert.equal(calls.length, 1);
  ctx.applySearchPreference({searchShortcut: 'Ctrl+Space'});
  assert.equal(ctx.searchShortcut, 'Alt+Space');
  ctx.setSearchShortcut('Ctrl+Space');
  assert.equal(calls.length, 1);
  calls[0][3]({ok: true, preferences: {searchShortcut: 'Alt+Space'}});
  assert.equal(ctx.searchShortcut, 'Ctrl+Space');
  assert.equal(calls.length, 2);
  assert.equal(calls[1][2].searchShortcut, 'Ctrl+Space');
  calls[1][3]({ok: true, preferences: {searchShortcut: 'Ctrl+Space'}});
  assert.equal(ctx.pendingSearchShortcut, '');
  ctx.setSearchShortcut('Meta+Space');
  assert.equal(calls.length, 2);
});

test('failed shortcut save remains retryable without a request loop', () => {
  const {ctx, calls} = harness();
  assert.equal(typeof ctx.setSearchShortcut, 'function');
  ctx.setSearchShortcut('Alt+Space');
  calls[0][3]({ok: false, error: 'offline'});
  assert.equal(ctx.pendingSearchShortcut, 'Alt+Space');
  assert.equal(calls.length, 1);
  ctx.setSearchShortcut('Alt+Space');
  assert.equal(calls.length, 2);
});

test('palette shortcut and all terminal focus paths are guarded while searching', () => {
  assert.match(panel, /RoomSearchPalette\s*\{/);
  assert.match(panel, /objectName: "roomSearchShortcut"/);
  assert.match(panel, /context: Qt.WindowShortcut/);
  assert.match(panel, /sequence: root.searchShortcut/);
  assert.match(panel, /blocked: root.searchOpen/);
  const compact = read('AresWorkspace.qml');
  assert.equal((compact.match(/focusOnAttach: visible && !roomSearch.expanded && !controller.searchOpen/g) || []).length, 2);
  assert.match(compact, /onPaletteRequested: controller.openSearch\(\)/);
  const extended = read('ExtendedWorkspace.qml');
  assert.match(extended, /focusOnAttach: !extended.controller.searchOpen/);
  assert.match(extended, /focusOnAttach: tile.detailed && !extended.controller.searchOpen/);
});
