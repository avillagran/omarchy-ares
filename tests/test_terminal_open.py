"""Isolated opener tests: never connect to a user's Herdr server."""
import importlib
import importlib.util
import hashlib
from contextlib import closing
import json
import os
import pwd
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import ares_remote_probe as probe

REQUEST = {"roomId": "room_test", "profile": "default", "sessionId": "session_test", "title": "Test"}


def terminal(**overrides):
    return {"agentKind": "hermes", "profile": "default", "sessionId": "session_test",
            "terminalId": "term_1", "paneId": "w1:p1", "tabId": "w1:t1",
            "socketPath": "/fixture/herdr.sock", "identitySource": "registry", "status": "idle",
            "herdrSession": "default", "label": "Unrelated title", **overrides}


def inventory(*items, complete=True):
    return {"ok": True, "terminals": list(items), "errors": [], "complete": complete}


class OpenerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("ares_terminal_open"), "owner-host opener is not implemented")
        self.opener = importlib.import_module("ares_terminal_open")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        home_patch = patch.object(Path, "home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def test_reuses_exact_existing_terminal_without_launch_or_title_matching(self):
        state = inventory(terminal())
        with patch.object(probe, "read_inventory", return_value=state):
            result = self.opener.open_session(REQUEST)
        self.assertTrue(result["ok"])
        self.assertEqual(result["openedTerminalId"], "term_1")
        self.assertEqual(result["terminals"], state["terminals"])
    def test_done_terminal_is_not_a_resumed_writable_owner(self):
        with patch.object(probe, 'read_inventory', return_value=inventory(terminal(status='done'))):
            result = self.opener.open_session(REQUEST)
        self.assertFalse(result['ok'])
        self.assertNotIn('openedTerminalId', result)

    def make_history(self, **overrides):
        import sqlite3
        root = self.home / ".hermes"
        root.mkdir(exist_ok=True)
        row = {"id": "session_test", "source": "telegram", "profile_name": "default",
               "session_key": "telegram:fixture", "cwd": str(self.home), "parent_session_id": None,
               "model_config": None, **overrides}
        with closing(sqlite3.connect(root / "state.db")) as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, profile_name TEXT, session_key TEXT, cwd TEXT, parent_session_id TEXT, model_config TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS messages (session_id TEXT, role TEXT, content TEXT)")
            db.execute("INSERT INTO sessions VALUES (:id,:source,:profile_name,:session_key,:cwd,:parent_session_id,:model_config)", row)
            db.execute("INSERT INTO messages VALUES (?, 'user', 'fixture')", (row["id"],))
            db.commit()
        return root

    def test_stored_identity_is_exact_read_only_and_rejects_descendants(self):
        self.make_history()
        self.assertTrue(hasattr(self.opener, "stored_session"), "exact resume preflight is missing")
        row = self.opener.stored_session(REQUEST)
        self.assertEqual(row["session_key"], "telegram:fixture")
        for sid in ("session", "missing"):
            with self.assertRaisesRegex(RuntimeError, "exact stored"):
                self.opener.stored_session({**REQUEST, "sessionId": sid})
        self.make_history(id="child", parent_session_id="session_test")
        with self.assertRaisesRegex(RuntimeError, "descendant"):
            self.opener.stored_session(REQUEST)

    def test_delegate_child_does_not_redirect_exact_parent_resume(self):
        self.make_history(source='cli', session_key=None)
        self.make_history(id='delegate', source='subagent', parent_session_id='session_test',
                          model_config=json.dumps({'_delegate_from':'session_test'}))
        self.assertEqual(self.opener.stored_session(REQUEST)['id'], 'session_test')

    def test_exact_delegated_session_requires_its_verified_parent_link(self):
        self.make_history(source='subagent', session_key=None, parent_session_id='parent',
                          model_config=json.dumps({'_delegate_from':'parent'}))
        self.assertEqual(self.opener.stored_session(REQUEST)['source'], 'subagent')
        with closing(sqlite3.connect(self.home / '.hermes/state.db')) as db:
            db.execute("UPDATE sessions SET model_config=? WHERE id=?",
                       (json.dumps({'_delegate_from':'different'}), REQUEST['sessionId']))
            db.commit()
        with self.assertRaisesRegex(RuntimeError, 'verified delegate parent'):
            self.opener.stored_session(REQUEST)

    def test_plain_cli_never_launches_without_exact_resume_contract(self):
        self.make_history(source='cli', session_key=None)
        with patch.object(probe, 'read_inventory', return_value=inventory()), \
             patch.object(probe, 'iter_process_argv', return_value=[]), \
             patch.object(self.opener, 'prepare_resume', side_effect=RuntimeError('exact resume unavailable')), \
             patch.object(self.opener, 'ensure_server') as ensure_server, \
             patch.object(self.opener, 'herdr') as herdr:
            result = self.opener.open_session(REQUEST)
        self.assertFalse(result['ok'])
        self.assertEqual(result['errors'][-1]['code'], 'EXACT_RESUME_UNAVAILABLE')
        herdr.assert_not_called()

    def test_pending_guarded_pane_never_attaches_without_history_ack(self):
        self.make_history(source='cli', session_key=None)
        key = hashlib.sha256((REQUEST['profile'] + '\0' + REQUEST['sessionId']).encode()).hexdigest()
        marker = self.opener.private_root() / (key + '.json')
        self.opener.save_job(marker, {'request': REQUEST, 'attempted': True,
            'ready':str(self.home/'missing-ack.json'), 'nonce':'a'*32, 'source':'cli',
            'paneId':'w1:p1', 'tabId':'w1:t1', 'terminalId':'term_1'})
        with patch.object(probe, 'read_inventory', return_value=inventory(terminal(herdrSession='ares'))):
            result = self.opener.open_session(REQUEST)
        self.assertFalse(result['ok'], result)
        self.assertNotIn('openedTerminalId', result)

    def test_opened_lease_without_child_history_ack_is_not_writable(self):
        self.make_history(source="cli", session_key=None)
        item = terminal(herdrSession="ares")
        owner = {"pid": os.getpid(), "session_id": REQUEST['sessionId'], "lease_id": "native"}
        launch = {"ready": str(self.home / "ready.json"), "nonce": "fixture-nonce"}
        with self.assertRaisesRegex(RuntimeError, "acknowledgment"):
            self.opener.verified_ready(REQUEST, item, owner, launch, "cli")
        Path(launch['ready']).write_text(json.dumps({"protocol": "hermes-exact-resume-ready-v1",
            "nonce": "fixture-nonce", "sessionId": REQUEST['sessionId'], "source": "cli",
            "pid": os.getpid(), "leaseId": "native", "messageCount": 1,
            "paneId": item['paneId'], "tabId": item['tabId']}))
        Path(launch['ready']).chmod(0o600)
        self.assertTrue(self.opener.verified_ready(REQUEST, item, owner, launch, "cli"))
        Path(launch['ready']).write_text(json.dumps({"protocol": "hermes-exact-resume-ready-v1",
            "nonce": "fixture-nonce", "sessionId": REQUEST['sessionId'], "source": "cli",
            "pid": os.getpid(), "leaseId": "different", "messageCount": 1,
            "paneId": item['paneId'], "tabId": item['tabId']}))
        with self.assertRaisesRegex(RuntimeError, "acknowledgment"):
            self.opener.verified_ready(REQUEST, item, owner, launch, "cli")

    def test_native_start_creates_no_focus_tab_and_verifies_same_terminal(self):
        self.make_history(source="cli", session_key=None)
        calls = []
        raw = {"tabs": [], "panes": [], "agents": [], "workspaces": [{"workspace_id": "w1"}]}
        state = inventory()
        ready = self.home / 'ready.json'
        def endpoint(args):
            calls.append(args)
            if args == ["api", "snapshot"]:
                return {"result": {"snapshot": raw}}
            if args[:2] == ["tab", "create"]:
                self.assertIn("--no-focus", args)
                raw["tabs"] = [{"tab_id": "w1:t1", "label": args[args.index("--label") + 1]}]
                raw["panes"] = [{"tab_id": "w1:t1", "pane_id": "w1:p1", "terminal_id": "term_1",
                                 "agent": None, "agent_session": None, "agent_status": "unknown"}]
                return {"result": {"tab": raw["tabs"][0], "root_pane": raw["panes"][0]}}
            if args[:2] == ["pane", "run"]:
                self.assertIn('--exact-resume', (Path(__file__).resolve().parents[1] / 'bin/ares_terminal_resume.py').read_text())
                state["terminals"] = [terminal(herdrSession="ares")]
                runtime = self.home / ".hermes/runtime"
                runtime.mkdir()
                (runtime / "active_sessions.json").write_text(json.dumps({"entries": [{
                    "lease_id": "native", "session_id": "session_test", "pid": os.getpid(),
                    "process_start_time": probe.process_started_at(os.getpid()), "surface": "cli"}]}))
                ready.write_text(json.dumps({"protocol": "hermes-exact-resume-ready-v1", "nonce": "a"*32,
                    "sessionId": "session_test", "source": "cli", "pid": os.getpid(),
                    "leaseId": "native", "messageCount": 1, "paneId": "w1:p1", "tabId": "w1:t1"}))
                ready.chmod(0o600)
                return {"result": {"agent": {"terminal_id": "term_1"}}}
            self.fail(args)
        with patch.object(probe, "read_inventory", side_effect=lambda: state), \
             patch.object(probe, "iter_process_argv", return_value=[]), \
             patch.object(probe, "process_environment", return_value={"HERDR_PANE_ID": "w1:p1", "HERDR_TAB_ID": "w1:t1", "HERDR_SOCKET_PATH": "/fixture/herdr.sock"}), \
             patch.object(self.opener, "herdr", side_effect=endpoint), \
             patch.object(self.opener, "socket_path", return_value=Path("/fixture/herdr.sock")), \
             patch.object(self.opener, "ensure_server"), \
             patch.object(self.opener, "prepare_resume", return_value={"python":"/fixture/python","hermes":"/fixture/hermes","launcher":"/fixture/launcher","payload":"/fixture/payload", "ready":str(ready), "nonce":"a"*32}), \
             patch.object(self.opener, "save_resume_payload"):
            result = self.opener.open_session(REQUEST)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["openedTerminalId"], "term_1")
        self.assertEqual(sum(args[:2] == ["pane", "run"] for args in calls), 1)
        self.assertFalse(any(args[:2] == ["agent", "start"] for args in calls))
        self.assertFalse(any("focus" in args or "prompt" in args for args in calls))


    def test_existing_identity_does_not_hide_corrupt_registry(self):
        root = self.make_history() / "runtime"
        root.mkdir()
        (root / "active_sessions.json").write_text("{broken")
        with patch.object(probe, "read_inventory", return_value=inventory(terminal())):
            result = self.opener.open_session(REQUEST)
        self.assertFalse(result["ok"])
        self.assertIn("registry", result["errors"][-1]["error"])

    def test_native_serve_transport_is_not_an_unleased_interactive_cli(self):
        self.make_history()
        with patch.object(probe, 'iter_process_argv', return_value=[(12345, ['python3', '/bin/hermes', 'serve'])]):
            self.opener.preflight(REQUEST, inventory(), self.opener.stored_session(REQUEST))
        with patch.object(probe, 'iter_process_argv', return_value=[(12345, ['hermes', '--query', 'serve'])]):
            with self.assertRaisesRegex(RuntimeError, 'unknown owner'):
                self.opener.preflight(REQUEST, inventory(), self.opener.stored_session(REQUEST))

    def test_unleased_hermes_process_is_unknown_owner_not_absence(self):
        self.make_history()
        for argv in (["hermes", "--cli"], ["hermes", "--resume=session_test"]):
            with self.subTest(argv=argv), patch.object(probe, "iter_process_argv", return_value=[(12345, argv)]):
                with self.assertRaisesRegex(RuntimeError, "owner|process"):
                    self.opener.preflight(REQUEST, inventory(), self.opener.stored_session(REQUEST))


    def test_end_to_end_native_cli_with_fake_herdr_and_lease_endpoints(self):
        import socket
        import subprocess
        import time
        self.make_history(source="cli", session_key=None)
        fixtures = self.home / "fixture"
        fixtures.mkdir()
        fake_bin = self.home / ".local/bin"
        fake_bin.mkdir(parents=True)
        package = fixtures / "hermes_cli"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "active_sessions.py").write_text('''
import fcntl, json, os, uuid
from pathlib import Path
from contextlib import contextmanager
from ares_remote_probe import process_started_at
PER_SESSION_EXCLUSIVE_SUBMIT = True
@contextmanager
def registry(root):
    p = Path(root) / 'runtime'
    p.mkdir(parents=True, exist_ok=True)
    with (p / 'active_sessions.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        f = p / 'active_sessions.json'
        entries = json.loads(f.read_text())['entries'] if f.exists() else []
        yield entries
        tmp = p / 'fixture.new'
        tmp.write_text(json.dumps({'entries': entries}))
        tmp.replace(f)
class Lease:
    enabled = True
    def __init__(self, root, key): self.root, self.lease_id = root, key
    def release(self):
        with registry(self.root) as entries:
            entries[:] = [e for e in entries if e['lease_id'] != self.lease_id]
def try_acquire_active_session(*, session_id, surface, config, metadata=None, registry_home=None, track_liveness=False):
    root = registry_home or Path.home() / '.hermes'
    with registry(root) as entries:
        if any(e['session_id'] == session_id for e in entries): return None, 'owned'
        key = uuid.uuid4().hex
        entries.append({'lease_id': key, 'session_id': session_id, 'surface': surface,
                        'pid': os.getpid(), 'process_start_time': process_started_at(os.getpid())})
        return Lease(root, key), None
''')
        # This probe fixture explicitly disables host process enumeration; all
        # remaining discovery is confined to the disposable HOME/socket/registry.
        fake_probe = fixtures / "ares_remote_probe.py"
        fake_probe.write_text(Path(probe.__file__).read_text() + "\ndef iter_process_argv():\n    return iter(())\n")
        (fake_bin / "hermes").write_text("#!" + sys.executable + "\n# fixture interpreter discovery only\n")
        (fake_bin / "hermes").chmod(0o700)
        native = fixtures / "native.py"
        native.write_text('''
import json, os, subprocess, sys, time
from pathlib import Path
from hermes_cli.active_sessions import try_acquire_active_session
if '--supervise' in sys.argv:
    child = subprocess.Popen([sys.executable, __file__])
    child.wait()
    raise SystemExit
root = Path.home()
lease, error = try_acquire_active_session(session_id='session_test', surface='cli', config={})
assert not error
p = root / 'snapshot.json'
s = json.loads(p.read_text())
s['agents'] = [{'agent':'hermes','pane_id':'w1:p1','tab_id':'w1:t1','terminal_id':'term_1',
                'agent_status':'idle', 'agent_session': {'kind':'id','value':'session_test',
                'agent':'hermes','source':'herdr:hermes','profile':'default'}}]
p.write_text(json.dumps(s))
while not (root / 'stop-native').exists(): time.sleep(.02)
lease.release()
''')
        herdr_script = fake_bin / "herdr"
        herdr_script.write_text("#!" + sys.executable + "\n" + '''
import json, os, subprocess, sys, time
from pathlib import Path
root = Path.home()
a = sys.argv[1:]
p = root / 'snapshot.json'
s = json.loads(p.read_text())
with (root / 'calls.jsonl').open('a') as f: f.write(json.dumps(a) + '\\n')
if a == ['api','snapshot']:
    print(json.dumps({'result': {'snapshot': s}}))
elif a[:2] == ['agent','start']:
    assert a[a.index('--') + 1:] == ['--profile','default','--cli','--resume','session_test']
    env = dict(os.environ, HERDR_PANE_ID='w1:p1', HERDR_TAB_ID='w1:t1')
    subprocess.Popen([sys.executable, str(root / 'fixture/native.py'), '--supervise'], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            if json.loads(p.read_text())['agents']: break
        except ValueError: pass
        time.sleep(.02)
    print(json.dumps({'result': {'agent': {'terminal_id': 'term_1'}}}))
else:
    raise SystemExit(2)
''')
        herdr_script.chmod(0o700)
        name, label, _ = self.opener.room_identity(REQUEST)
        (self.home / "snapshot.json").write_text(json.dumps({
            "workspaces": [{"workspace_id": "w1"}], "tabs": [{"tab_id": "w1:t1", "label": label}],
            "panes": [{"tab_id": "w1:t1", "pane_id": "w1:p1", "terminal_id": "term_1", "agent": None,
                       "agent_session": None, "agent_status": "unknown"}], "agents": []}))
        sockpath = self.home / ".config/herdr/sessions/ares/herdr.sock"
        sockpath.parent.mkdir(parents=True)
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(sockpath))
        self.addCleanup(sock.close)
        self.addCleanup(lambda: (self.home / "stop-native").touch())
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
               "HERMES_HOME": str(self.home / ".hermes"), "PATH": str(fake_bin) + ":" + os.environ["PATH"],
               "PYTHONPATH": str(fixtures)}
        with patch.dict(os.environ, env), patch.object(probe, "iter_process_argv", return_value=[]), \
             patch.object(probe, "__file__", str(fake_probe)):
            result = self.opener.open_session(REQUEST)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["errors"][-1]["code"], "EXACT_RESUME_UNAVAILABLE")
        calls = [json.loads(line) for line in (self.home / "calls.jsonl").read_text().splitlines()]
        self.assertFalse(any(a[:2] == ["agent", "start"] for a in calls))
        self.assertFalse(any(a[:2] == ["pane", "run"] for a in calls))

    def test_room_specific_idle_shell_reuse_rejects_restore_different_agent_and_splits(self):
        _, label, _ = self.opener.room_identity(REQUEST)
        base = {"tabs": [{"tab_id": "w1:t1", "label": label}], "agents": [], "workspaces": [],
                "panes": [{"tab_id": "w1:t1", "pane_id": "w1:p1", "terminal_id": "term_1",
                           "agent": None, "agent_session": None, "agent_status": "unknown"}]}
        with patch.object(self.opener, "snapshot", return_value=base), patch.object(self.opener, "herdr") as endpoint:
            self.assertEqual(self.opener.room_pane(REQUEST)["pane_id"], "w1:p1")
            endpoint.assert_not_called()
        import copy
        for change in ("restore", "agent", "split", "pending"):
            state = copy.deepcopy(base)
            if change == "restore":
                state["panes"][0]["agent_session"] = {"value": "different"}
            elif change == "agent":
                state["agents"] = [{"tab_id": "w1:t1", "agent": "codex"}]
            elif change == "split":
                state["panes"].append(dict(state["panes"][0], pane_id="w1:p2"))
            else:
                state["panes"][0]["agent_status"] = "working"
            with self.subTest(change=change), patch.object(self.opener, "snapshot", return_value=state), \
                 patch.object(self.opener, "herdr") as endpoint:
                with self.assertRaises(RuntimeError):
                    self.opener.room_pane(REQUEST)
                endpoint.assert_not_called()

    def test_live_or_unknown_stored_and_routing_owners_block_launch(self):
        root = self.make_history() / "runtime"
        root.mkdir()
        registry = root / "active_sessions.json"
        for key in ("session_test", "telegram:fixture"):
            for started in (probe.process_started_at(os.getpid()), None):
                registry.write_text(json.dumps({"entries": [{"lease_id": "existing", "pid": os.getpid(),
                    "session_id": key, "process_start_time": started, "surface": "gateway:telegram"}]}))
                with self.subTest(key=key, started=started), patch.object(probe, "read_inventory", return_value=inventory()), \
                     patch.object(probe, "iter_process_argv", return_value=[]), \
                     patch.object(self.opener, "herdr") as endpoint:
                    result = self.opener.open_session(REQUEST)
                    self.assertFalse(result["ok"])
                    endpoint.assert_not_called()

    def test_timeout_never_reports_success_or_repeats_dispatch(self):
        self.make_history(source="cli", session_key=None)
        with patch.object(probe, "read_inventory", return_value=inventory()), \
             patch.object(probe, "iter_process_argv", return_value=[]), \
             patch.object(self.opener, "ensure_server"), \
             patch.object(self.opener, "room_pane", return_value={"pane_id": "w1:p1", "tab_id":"w1:t1", "terminal_id":"term_1"}), \
             patch.object(self.opener, "prepare_resume", return_value={"python":"/fixture/python","hermes":"/fixture/hermes","launcher":"/fixture/launcher","payload":"/fixture/payload","ready":str(self.home/'ready.json'),"nonce":"a"*32}), \
             patch.object(self.opener, "save_resume_payload"), \
             patch.object(self.opener, "herdr", side_effect=RuntimeError("fixture timeout")) as endpoint:
            result = self.opener.open_session(REQUEST)
            again = self.opener.open_session(REQUEST)
        self.assertFalse(result["ok"])
        self.assertNotIn("openedTerminalId", result)
        self.assertFalse(again["ok"])
        self.assertIn("automatic retry disabled", again["error"])
        self.assertEqual(endpoint.call_count, 1)


    def test_server_timeout_cannot_spawn_another_server_for_a_different_room(self):
        with patch.object(self.opener, "socket_path", return_value=self.home / "absent.sock"), \
             patch.object(self.opener, "herdr_program", return_value="fixture-herdr"), \
             patch.object(self.opener.subprocess, "Popen") as spawn, \
             patch.object(self.opener.time, "monotonic", side_effect=[0, 11, 0, 11]):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    self.opener.ensure_server()
        self.assertEqual(spawn.call_count, 1)

    def test_same_numeric_pane_in_a_different_socket_is_not_success(self):
        self.make_history(source="cli", session_key=None)
        with patch.object(probe, "read_inventory", side_effect=[inventory(), inventory(), inventory(terminal())]), \
             patch.object(probe, "iter_process_argv", return_value=[]), \
             patch.object(self.opener, "ensure_server"), \
             patch.object(self.opener, "room_pane", return_value={"pane_id": "w1:p1", "tab_id": "w1:t1", "terminal_id": "term_1"}), \
             patch.object(self.opener, "herdr", return_value={}), \
             patch.object(self.opener, "verified_owner", return_value={"pid": 123}):
            result = self.opener.open_session(REQUEST)
        self.assertFalse(result["ok"])
        self.assertNotIn("openedTerminalId", result)

    def test_owner_appearing_during_locked_recheck_blocks_dispatch(self):
        self.make_history()
        with patch.object(probe, "iter_process_argv", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "appeared|existing"):
                self.opener.preflight(REQUEST, inventory(terminal()), self.opener.stored_session(REQUEST))

    def test_supervisor_requires_child_pipe_ack_before_writing_ready_file(self):
        import ares_terminal_resume as supervisor
        ready = {'path': str(self.home/'ready.json'), 'nonce':'a'*32,
                 'paneId':'w1:p1', 'tabId':'w1:t1', 'terminalId':'term_1', 'sessionId':'session_test', 'source':'cli'}
        bad = [sys.executable, '-c', 'import sys; sys.exit(3)']
        with self.assertRaisesRegex(RuntimeError, 'acknowledgment'):
            supervisor.supervise(self.home, None, bad, lambda lease: None, ready=ready)
        self.assertFalse(Path(ready['path']).exists())
        child = '''import json, os, sys, time
fd = int(sys.argv[-1])
os.write(fd, (json.dumps({'protocol':'hermes-exact-resume-ready-v1', 'sessionId':'session_test',
    'source':'cli', 'pid':os.getpid(), 'leaseId':'lease-test', 'messageCount':1})+'\\n').encode())
time.sleep(.1)
'''
        observed = {'agents':[{'agent':'hermes','pane_id':'w1:p1','tab_id':'w1:t1',
                    'terminal_id':'term_1','agent_status':'idle',
                    'agent_session':{'kind':'id','agent':'hermes',
                                     'source':'herdr:hermes','value':'session_test'}}]}
        with patch.object(self.opener, 'herdr', return_value={}) as endpoint, \
             patch.object(self.opener, 'snapshot', return_value=observed):
            self.assertEqual(supervisor.supervise(self.home, None, [sys.executable, '-c', child],
                                                  lambda lease: None, ready=ready), 0)
        self.assertTrue(any(call.args[0][:2] == ['pane','report-agent'] for call in endpoint.call_args_list))
        self.assertTrue(any(call.args[0] == ['pane', 'report-agent-session', 'w1:p1',
            '--source', 'herdr:hermes', '--agent', 'hermes', '--agent-session-id', 'session_test']
                            for call in endpoint.call_args_list))
        self.assertTrue(any(call.args[0][:2] == ['pane','release-agent'] for call in endpoint.call_args_list))
        ack = json.loads(Path(ready['path']).read_text())
        self.assertEqual(ack['nonce'], 'a'*32)
        self.assertEqual(ack['paneId'], 'w1:p1')
        self.assertEqual(ack['leaseId'], 'lease-test')

    def test_supervisor_equal_identity_does_not_claim_its_own_child_lease(self):
        import ares_terminal_resume as supervisor
        with patch.object(supervisor.subprocess, 'Popen') as spawn:
            spawn.return_value.poll.return_value = 0
            spawn.return_value.wait.return_value = 0
            self.assertEqual(supervisor.supervise(self.home, None, ['/fixture/hermes'], lambda lease: self.assertIsNone(lease)), 0)
            spawn.assert_called_once()

    def test_supervisor_requires_exact_stored_session_and_source(self):
        import ares_terminal_resume as supervisor
        self.make_history(source='cli', session_key='route:fixture')
        pane = {'pane_id':'w1:p1','tab_id':'w1:t1','terminal_id':'term_1'}
        payload = {'request': REQUEST, 'row': self.opener.stored_session(REQUEST),
                   'pane': pane, 'home': str(self.home/'.hermes'), 'hermes':'/native/hermes',
                   'ready': str(self.home/'ready.json'), 'nonce':'a'*32}
        path = self.home/'request.json'
        path.write_text(json.dumps(payload))
        with patch.dict(os.environ, {'HERMES_HOME':str(self.home/'.hermes'),
                 'HERDR_PANE_ID':'w1:p1','HERDR_TAB_ID':'w1:t1'}), \
             patch.object(supervisor, 'supervise', return_value=0) as launch:
            self.assertEqual(supervisor.main(str(path)),0)
        self.assertEqual(launch.call_args.args[2],
            ['/native/hermes','--profile','default','chat','--cli','--resume','session_test',
             '--exact-resume','--source','cli'])

    def test_foreground_native_lease_excludes_gateway_until_child_exit(self):
        import subprocess
        import time
        native = next(iter(sorted(Path(pwd.getpwuid(os.getuid()).pw_dir).glob(
            '.hermes/installs/*/environments/*/venv/bin/python'))),
            Path('/nonexistent/python'))
        if not native.exists():
            self.skipTest('installed native Hermes required')
        self.assertIsNotNone(importlib.util.find_spec('ares_terminal_resume'), 'foreground launcher missing')
        root = self.make_history()
        code = '''
import os, sys
from pathlib import Path
from ares_terminal_resume import supervise
from hermes_cli.active_sessions import try_acquire_active_session
root=Path(sys.argv[1])
def check(lease):
    assert lease.enabled
supervise(root, 'telegram:fixture', [sys.executable, '-c', "import os,time; from pathlib import Path; Path(os.environ['HOME']+'/ready').write_text(str(os.getpid())); time.sleep(1)"], check)
'''
        env = {**os.environ, 'HOME':str(self.home), 'HERMES_HOME':str(root),
               'PYTHONPATH':str(Path(__file__).resolve().parents[1] / 'bin')}
        child = subprocess.Popen([str(native), '-c', code, str(root)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            until = time.monotonic()+5
            while not (self.home/'ready').exists() and child.poll() is None and time.monotonic()<until:
                time.sleep(.02)
            if not (self.home/'ready').exists():
                self.fail(str(child.communicate(timeout=2)))
            check = "from hermes_cli.active_sessions import try_acquire_active_session as a; from pathlib import Path; l,e=a(session_id='telegram:fixture',surface='gateway:telegram',config={},registry_home=Path.home()/'.hermes',track_liveness=True); print('owned' if l else 'blocked'); l and l.release()"
            blocked = subprocess.run([str(native), '-c', check], env=env, capture_output=True, text=True, timeout=3)
            self.assertEqual(blocked.stdout.strip(), 'blocked', blocked.stderr)
            self.assertEqual(child.wait(timeout=5), 0)
            opened = subprocess.run([str(native), '-c', check], env=env, capture_output=True, text=True, timeout=3)
            self.assertEqual(opened.stdout.strip(), 'owned', opened.stderr)
        finally:
            if child.poll() is None: child.kill()
            child.communicate(timeout=3)

    def test_native_resume_refuses_cli_without_exact_resume_support(self):
        self.make_history()
        with patch.object(self.opener.shutil, 'which', return_value='/fixture/bin/hermes'), \
             patch.object(self.opener.subprocess, 'run', side_effect=lambda args, **kwargs: type('Exit', (),
                 {'returncode':0, 'stdout':'<PANE_ID> --env' if args[0] == '/fixture/herdr' else ''})()), \
             patch.object(self.opener, 'herdr_program', return_value='/fixture/herdr'), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(os, 'access', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'exact resume'):
                self.opener.prepare_resume()

    def test_prepare_resume_falls_back_from_old_launcher_to_exact_checkout(self):
        old = self.home / '.local/bin/hermes'
        source = self.home / '.hermes/hermes-agent/venv/bin/hermes'
        source.parent.mkdir(parents=True)
        source.write_text('fixture')
        (source.parent / 'python').write_text('fixture')
        source.chmod(0o700)
        (source.parent / 'python').chmod(0o700)
        def runner(args, **kwargs):
            text = '' if str(old) == args[0] else '--exact-resume --exact-resume-ready-fd' if str(source) == args[0] else '<PANE_ID> --env'
            return type('Exit', (), {'returncode':0, 'stdout':text})()
        with patch.object(self.opener.shutil, 'which', return_value=str(old)), \
             patch.object(self.opener.subprocess, 'run', side_effect=runner), \
             patch.object(self.opener, 'herdr_program', return_value='/fixture/herdr'):
            launch = self.opener.prepare_resume()
        self.assertEqual(launch['hermes'], str(source))

    def test_prepare_resume_prefers_isolated_hermes_over_live_checkout(self):
        isolated = self.home / '.local/share/omarchy-ares/hermes-exact/bin/hermes'
        isolated.parent.mkdir(parents=True)
        isolated.write_text('fixture')
        (isolated.parent/'python').write_text('fixture')
        isolated.chmod(0o700)
        (isolated.parent/'python').chmod(0o700)
        observed = []
        def runner(args, **kwargs):
            observed.append(args[0])
            content = ('--exact-resume --exact-resume-ready-fd'
                       if args[0] == str(isolated) else '<PANE_ID> --env')
            return type('Exit', (), {'returncode':0, 'stdout':content})()
        with patch.object(self.opener.shutil, 'which', return_value='/live/hermes'), \
             patch.object(self.opener.subprocess, 'run', side_effect=runner), \
             patch.object(self.opener, 'herdr_program', return_value='/fixture/herdr'):
            launch = self.opener.prepare_resume()
        self.assertEqual(launch['hermes'], str(isolated))
        self.assertNotIn('/live/hermes', observed)

    def test_prepare_resume_refuses_incomplete_isolated_runtime_without_live_fallback(self):
        isolated = self.home / '.local/share/omarchy-ares/hermes-exact/bin/hermes'
        isolated.parent.mkdir(parents=True)
        isolated.write_text('fixture')
        isolated.chmod(0o700)
        live = self.home / '.hermes/hermes-agent/venv/bin/hermes'
        live.parent.mkdir(parents=True)
        live.write_text('fixture')
        (live.parent / 'python').write_text('fixture')
        live.chmod(0o700)
        (live.parent / 'python').chmod(0o700)
        observed = []

        def runner(args, **kwargs):
            observed.append(args[0])
            return type('Exit', (), {'returncode': 0,
                                     'stdout': '--exact-resume --exact-resume-ready-fd'})()

        with patch.object(self.opener.subprocess, 'run', side_effect=runner), \
             patch.object(self.opener, 'herdr_program', return_value='/fixture/herdr'):
            with self.assertRaisesRegex(RuntimeError, 'exact resume'):
                self.opener.prepare_resume()
        self.assertNotIn(str(live), observed)

    def test_embedded_templates_stage_privately_without_file_global(self):
        import types
        root=Path(__file__).resolve().parents[1]/'bin'
        sources={name:(root/name).read_text() for name in ('ares_terminal_open.py','ares_terminal_resume.py','ares_remote_probe.py')}
        namespace={'__name__':'ares_terminal_open','TERMINAL_SOURCES':sources}
        exec(compile(sources['ares_terminal_open.py'],'ares_terminal_open.py','exec'),namespace)
        def supported_cli(args, **kwargs):
            output = '--exact-resume --exact-resume-ready-fd' if args[0] == '/fixture/bin/hermes' else '<PANE_ID> --env'
            return type('Exit', (), {'returncode': 0, 'stdout': output})()
        with patch.object(namespace['shutil'], 'which', return_value='/fixture/bin/hermes'), \
             patch.object(namespace['subprocess'], 'run', side_effect=supported_cli), \
             patch.object(namespace['Path'], 'is_file', return_value=True), \
             patch.object(namespace['os'], 'access', return_value=True), \
             patch.dict(namespace, {'herdr_program': lambda: '/fixture/herdr'}):
            launch=namespace['prepare_resume']()
        self.assertNotIn('__file__',namespace)
        for name,text in sources.items():
            staged=Path(launch['launcher']).parent/name
            self.assertEqual(staged.read_text(),text)
            self.assertEqual(staged.stat().st_mode & 0o777,0o600)

    def test_supervisor_sigkill_kills_child_and_native_registry_reopens(self):
        import subprocess, time, signal
        native=next(iter(sorted(Path(pwd.getpwuid(os.getuid()).pw_dir).glob(
            '.hermes/installs/*/environments/*/venv/bin/python'))),
            Path('/nonexistent/python'))
        root=self.make_history()
        env={**os.environ,'HOME':str(self.home),'HERMES_HOME':str(root),
             'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'bin')}
        code="from ares_terminal_resume import supervise; import sys; from pathlib import Path; supervise(Path.home()/'.hermes','telegram:fixture',[sys.executable,'-c',\"import os,time; from pathlib import Path; Path.home().joinpath('pid').write_text(str(os.getpid())); time.sleep(60)\"],lambda lease: None)"
        parent=subprocess.Popen([str(native),'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not (self.home/'pid').exists() and time.monotonic()<deadline: time.sleep(.02)
            pid=int((self.home/'pid').read_text())
            parent.kill(); parent.wait(timeout=3)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                try: state=Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[0]
                except (FileNotFoundError, ProcessLookupError): break
                if state=='Z': break
                time.sleep(.02)
            else: self.fail('child survived supervisor death')
            result=subprocess.run([str(native),'-c',"from hermes_cli.active_sessions import try_acquire_active_session as a; l,e=a(session_id='telegram:fixture',surface='gateway:telegram',config={},track_liveness=True); assert l and not e; l.release(); print('reopened')"],env=env,capture_output=True,text=True,timeout=3)
            self.assertEqual(result.stdout.strip(),'reopened',result.stderr)
        finally:
            if parent.poll() is None: parent.kill()
            parent.communicate(timeout=3)

    def test_routed_open_waits_for_identity_without_redispatch(self):
        self.make_history()
        pane = {'pane_id':'w1:p1', 'tab_id':'w1:t1', 'terminal_id':'term_1'}
        ready = self.home / 'ready.json'
        ready.write_text('fixture')
        with patch.object(probe, 'read_inventory', side_effect=[inventory(), inventory(), inventory(), inventory(terminal(herdrSession='ares'))]), \
             patch.object(probe, 'iter_process_argv', return_value=[]), \
             patch.object(self.opener, 'prepare_resume', return_value={'python':'/p','launcher':'/l','payload':'/j','ready':str(ready),'nonce':'a'*32}), \
             patch.object(self.opener, 'ensure_server'), patch.object(self.opener, 'room_pane', return_value=pane), \
             patch.object(self.opener, 'save_resume_payload'), patch.object(self.opener, 'verified_owner', return_value={}), \
             patch.object(self.opener, 'verified_ready', return_value=True), \
             patch.object(self.opener, 'socket_path', return_value=Path('/fixture/herdr.sock')), \
             patch.object(self.opener, 'herdr', return_value={}) as run:
            result = self.opener.open_session(REQUEST)
        self.assertTrue(result['ok'],result)
        self.assertEqual(run.call_count,1)

    def test_native_herdr_fresh_pane_runs_foreground_command_with_profile_env(self):
        import subprocess, time
        env = {k:v for k,v in os.environ.items() if not k.startswith('HERDR_')}
        env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home/'.config'),
                   XDG_STATE_HOME=str(self.home/'.state'), XDG_DATA_HOME=str(self.home/'.data'),
                   SHELL='/bin/bash')
        sock = self.home/'.config/herdr/sessions/proof/herdr.sock'
        env['HERDR_SOCKET_PATH'] = str(sock)
        server = subprocess.Popen(['/usr/bin/herdr','--session','proof','server'], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def cli(*args):
            result = subprocess.run(['/usr/bin/herdr',*args], env=env, capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout) if result.stdout.strip() else {}
        try:
            deadline=time.monotonic()+5
            while not sock.is_socket() and server.poll() is None and time.monotonic()<deadline: time.sleep(.02)
            self.assertTrue(sock.is_socket())
            made=cli('workspace','create','--label','Isolated proof','--no-focus','--env','HERMES_HOME='+str(self.home/'.hermes'))
            snap=cli('api','snapshot')['result']['snapshot']
            pane=snap['panes'][0]['pane_id']
            output=self.home/'proof-output'
            cli('pane','run',pane,'printf %s "$HERMES_HOME" > '+str(output))
            deadline=time.monotonic()+4
            while not output.exists() and time.monotonic()<deadline: time.sleep(.02)
            self.assertEqual(output.read_text(),str(self.home/'.hermes'))
        finally:
            if server.poll() is None:
                try: cli('server','stop')
                except Exception: server.terminate()
            try: server.communicate(timeout=4)
            except subprocess.TimeoutExpired: server.kill(); server.communicate()

    def test_unleased_wrapper_of_verified_other_owner_is_not_unknown(self):
        self.make_history()
        other = terminal(sessionId='other')
        env = {'HERDR_PANE_ID':other['paneId'], 'HERDR_TAB_ID':other['tabId'], 'HERDR_SOCKET_PATH':other['socketPath']}
        with patch.object(self.opener, 'live_entries', return_value=[{'pid':222, 'session_id':'other', 'lease_id':'x', 'surface':'cli'}]), \
             patch.object(probe, 'iter_process_argv', return_value=[(111, ['python3','hermes'])]), \
             patch.object(probe, 'process_environment', return_value=env), \
             patch.object(self.opener, 'process_parent', side_effect=lambda pid: {222:111,111:1}.get(pid,1), create=True):
            self.opener.preflight(REQUEST, inventory(other), self.opener.stored_session(REQUEST))

    def test_verified_other_sid_without_profile_is_not_target_owner(self):
        self.make_history()
        with patch.object(probe, 'iter_process_argv', return_value=[]):
            self.opener.preflight(REQUEST, inventory(terminal(sessionId='different', profile='')), self.opener.stored_session(REQUEST))

    def test_routed_open_uses_only_fresh_pane_and_staged_foreground_launcher(self):
        self.make_history()
        calls = []
        pane = {'pane_id':'w1:p1', 'tab_id':'w1:t1', 'terminal_id':'term_1'}
        ready = self.home / 'ready.json'
        ready.write_text('fixture')
        with patch.object(probe, 'read_inventory', side_effect=[inventory(), inventory(), inventory(terminal(herdrSession='ares'))]), \
             patch.object(probe, 'iter_process_argv', return_value=[]), \
             patch.object(self.opener, 'prepare_resume', return_value={'python':'/native/python', 'launcher':'/private/launch.py', 'payload':'/private/job.json', 'hermes':'/native/hermes', 'ready':str(ready),'nonce':'a'*32}) as prepare, \
             patch.object(self.opener, 'ensure_server'), \
             patch.object(self.opener, 'room_pane', return_value=pane) as create, \
             patch.object(self.opener, 'save_resume_payload'), \
             patch.object(self.opener, 'verified_owner', return_value={'pid':123}), \
             patch.object(self.opener, 'verified_ready', return_value=True), \
             patch.object(self.opener, 'socket_path', return_value=Path('/fixture/herdr.sock')), \
             patch.object(self.opener, 'herdr', side_effect=lambda args: calls.append(args) or {}):
            result = self.opener.open_session(REQUEST)
        self.assertTrue(result['ok'], result)
        self.assertTrue(create.call_args.kwargs['fresh'])
        self.assertEqual(calls, [['pane','run','w1:p1','/native/python','/private/launch.py','/private/job.json']])

    def test_telegram_launch_is_blocked_without_supported_native_lifetime_fence(self):
        self.make_history()
        with patch.object(probe, "read_inventory", return_value=inventory()), \
             patch.object(probe, "iter_process_argv", return_value=[]), \
             patch.object(self.opener.subprocess, "Popen") as detached, \
             patch.object(self.opener, "prepare_resume", side_effect=RuntimeError("unsupported")), \
             patch.object(self.opener, "herdr") as endpoint, \
             patch.object(self.opener.time, "monotonic", side_effect=[0, 100]):
            result = self.opener.open_session(REQUEST)
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][-1].get("code"), "EXACT_RESUME_UNAVAILABLE")
        detached.assert_not_called()
        endpoint.assert_not_called()

    def test_in_memory_ssh_bootstrap_needs_no_file_or_daemon(self):
        import types
        root = Path(__file__).resolve().parents[1]
        module = types.ModuleType("ares_remote_probe")
        exec(compile((root / "bin/ares_remote_probe.py").read_text(), "ares_remote_probe.py", "exec"), module.__dict__)
        namespace = {"__name__": "ares_terminal_open"}
        with patch.dict(sys.modules, {"ares_remote_probe": module}):
            exec(compile((root / "bin/ares_terminal_open.py").read_text(), "ares_terminal_open.py", "exec"), namespace)
            module.read_inventory = lambda: inventory(terminal())
            result = namespace["open_session"](REQUEST)
        self.assertEqual(result["openedTerminalId"], "term_1")
        self.assertNotIn("__file__", namespace)
        self.assertNotIn("guardian", namespace)
        self.assertIsInstance(namespace["open_session"]({})["error"], str)

    def test_host_flock_and_journal_serialize_concurrent_opens(self):
        import threading
        self.make_history(source="cli", session_key=None)
        with patch.object(probe, "read_inventory", return_value=inventory()), \
             patch.object(probe, "iter_process_argv", return_value=[]), \
             patch.object(self.opener, "prepare_resume", return_value={"python":"/fixture/python","hermes":"/fixture/hermes","launcher":"/fixture/launcher","payload":"/fixture/payload"}), \
             patch.object(self.opener, "ensure_server", side_effect=RuntimeError("fixture timeout")) as endpoint:
            workers = [threading.Thread(target=self.opener.open_session, args=(REQUEST,)) for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
        self.assertEqual(endpoint.call_count, 1)

    def test_identity_uncertainty_never_becomes_permission_to_open(self):
        cases = [inventory(terminal(), complete=False), inventory(terminal(), terminal()),
                 inventory(terminal(identitySource="conflict")),
                 inventory(terminal(identitySource="")), inventory(terminal(status="unknown"))]
        for state in cases:
            with self.subTest(state=state), patch.object(probe, "read_inventory", return_value=state):
                result = self.opener.open_session(REQUEST)
                self.assertFalse(result["ok"])
                self.assertNotIn("openedTerminalId", result)

    def test_invalid_identifiers_are_rejected_before_discovery(self):
        for field, value in [("profile", "../other"), ("sessionId", "latest"),
                             ("sessionId", ""), ("roomId", "room;evil")]:
            with self.subTest(field=field, value=value), patch.object(probe, "read_inventory") as read:
                result = self.opener.open_session({**REQUEST, field: value})
                self.assertFalse(result["ok"])
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
