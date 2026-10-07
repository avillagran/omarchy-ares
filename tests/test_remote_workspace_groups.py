"""Remote Hermes sessions carry workspace identity into Ares project sync."""

import runpy
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


RUNTIME = Path(__file__).resolve().parents[1] / "bin" / "ares-hermes-runtime"


class RemoteWorkspaceGroupsTests(unittest.TestCase):
    def test_default_remote_sync_does_not_import_unpaired_gateway(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["list_remote_sessions"]
        calls = []
        with patch.dict(fn.__globals__, {
            "_read_env_values": lambda: {},
            "_configured_peers": lambda: {"host": {"url": "http://192.0.2.1:8642"}},
            "_remote_sessions_for_peer": lambda *args: calls.append(args) or [{"id": "remote::host::default::s1"}],
            "_paired_aliases": lambda: [],
        }):
            self.assertEqual(fn(), [])
        self.assertEqual(calls, [])

    def test_mesh_discovery_never_enrolls_a_key_in_process_arguments(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["sync_peers"]
        calls = []
        with patch.dict(fn.__globals__, {
            "sync_omarchy_docs_skills": lambda: {},
            "discover_peers": lambda: {"discovered": [{"url": "http://192.0.2.1:8642", "ip": "192.0.2.1"}]},
            "_read_env_values": lambda: {"API_SERVER_KEY": "test-private-key"},
            "_configured_peers": lambda: {},
            "probe_peer": lambda *args, **kwargs: {"authenticated": True},
            "list_peers": lambda: {"peers": []},
            "subprocess": type("NoProcess", (), {"run": staticmethod(lambda *args, **kwargs: calls.append(args))}),
        }):
            result = fn()
        self.assertEqual(calls, [])
        self.assertEqual(result["added"], [])

    def test_unpaired_gateway_path_does_not_assert_project_identity(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_remote_sessions_for_peer"]
        with patch.dict(fn.__globals__, {
            "_peer_key": lambda *args: "test-key",
            "probe_peer": lambda *args, **kwargs: {"authenticated": True},
            "_peer_profile_rows": lambda *args: [{"profile": "default"}],
            "_peer_ui_metadata": lambda: {},
            "_peer_json": lambda *args: {"data": [{"id": "one", "message_count": 1,
                "last_active": time.time(), "cwd": "/claimed/by/gateway"}], "has_more": False},
            "_paired_session_paths": lambda *args: {},
        }):
            rows = fn("host", {"url": "http://example.test:8642"}, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workspacePath"], "")

    def test_paired_archive_and_legacy_gateway_cannot_assert_archive(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["list_remote_sessions"]
        gateway = {"id": "remote::host::default::s1", "externalArchived": True}
        paired = dict(gateway, externalArchived=False)
        with patch.dict(fn.__globals__, {
            "_read_env_values": lambda: {}, "_configured_peers": lambda: {"host": {"url": "http://192.0.2.1"}},
            "_remote_sessions_for_peer": lambda *a: [dict(gateway)],
            "_paired_aliases": lambda: ["host"], "_paired_session_rows": lambda *a: [dict(paired)],
        }), patch.dict(os.environ, {"ARES_LEGACY_GATEWAY_IMPORT": "1"}):
            self.assertIs(fn()[0]["externalArchived"], False)
        with patch.dict(fn.__globals__, {
            "_read_env_values": lambda: {}, "_configured_peers": lambda: {"host": {"url": "http://192.0.2.1"}},
            "_remote_sessions_for_peer": lambda *a: [dict(gateway)], "_paired_aliases": lambda: [],
        }), patch.dict(os.environ, {"ARES_LEGACY_GATEWAY_IMPORT": "1"}):
            self.assertIs(fn()[0]["externalArchived"], False)

    def test_malformed_archive_page_is_rejected_but_old_catalog_defaults_false(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_paired_session_rows"]
        def control(archived):
            return lambda request: {"ok": True, "result": {"data": [
                {"id": "s1", "profile": "default", "title": "Chat", "message_count": 1,
                 **({} if archived is None else {"archived": archived})}], "has_more": False}}
        for malformed in (1, "true", [], {}):
            with self.subTest(malformed=malformed), patch.dict(fn.__globals__, {"_peer_control_json": control(malformed)}):
                self.assertEqual(fn("host"), [])
        with patch.dict(fn.__globals__, {"_peer_control_json": control(None)}):
            self.assertIs(fn("host")[0]["externalArchived"], False)
        with patch.dict(fn.__globals__, {"_peer_control_json": control(True)}):
            self.assertIs(fn("host")[0]["externalArchived"], True)

    def test_large_authenticated_catalog_pages_beyond_400(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_paired_session_rows"]
        calls = []
        def control(request):
            offset = request["request"]["offset"]
            calls.append(offset)
            page = [{"id": f"s{i}", "profile": "default", "title": "Session",
                     "message_count": 1, "last_activity_at": 500,
                     "cwd": "/source/repo" if i == 1100 else "", "git_repo_root": ""}
                    for i in range(offset, min(offset + 75, 1154))]
            return {"ok": True, "result": {"data": page,
                "has_more": offset + len(page) < 1154}}
        with patch.dict(fn.__globals__, {
            "_peer_control_json": control,
            "_peer_ui_metadata": lambda: {},
        }):
            rows = fn("host")
        self.assertEqual(len(rows), 1154)
        self.assertEqual(len({row["id"] for row in rows}), 1154)
        self.assertEqual(next(r for r in rows if r["remoteSessionId"] == "s1100")["workspacePath"],
                         "/source/repo")
        self.assertGreater(len(calls), 5)

    def test_remote_listing_pages_past_workspace_free_recent_sessions(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_remote_sessions_for_peer"]
        globals_ = fn.__globals__
        now = time.time()
        paths = []

        def peer_json(url, path, key, timeout):
            paths.append(path)
            if "offset=0" in path:
                return {"data": [
                    {"id": "no-workspace", "source": "cron", "title": "Scheduled",
                     "message_count": 1, "last_active": now},
                ], "has_more": True}
            return {"data": [
                {"id": "project", "source": "cli", "title": "Project",
                 "message_count": 1, "last_active": now - 1,
                 "cwd": "/home/agent/work/project"},
            ], "has_more": False}

        with patch.dict(globals_, {
            "_peer_key": lambda *args: "test-key",
            "probe_peer": lambda *args, **kwargs: {"authenticated": True},
            "_peer_profile_rows": lambda *args: [{"profile": "default", "title": "Hermes"}],
            "_peer_ui_metadata": lambda: {},
            "_peer_json": peer_json,
        }):
            rows = fn("host", {"url": "http://example.test:8642"}, {})

        self.assertEqual([row["workspacePath"] for row in rows], ["", ""])
        self.assertEqual(len(paths), 2)
        self.assertTrue(all("include_workspace=1" in path for path in paths))

    def test_paired_source_joins_dense_gateway_pages_by_exact_id(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_remote_sessions_for_peer"]
        now = time.time()
        calls = []
        def gateway(url, path, key, timeout):
            offset = int(path.split("offset=")[1].split("&")[0])
            return {"data": [{"id": f"s{i}", "title": f"Untrusted title {i}",
                              "message_count": 1, "last_active": now}
                             for i in range(offset, min(offset + 100, 229))],
                    "has_more": offset + 100 < 229}
        def control(request):
            calls.append(request)
            offset = request["request"]["offset"]
            return {"ok": True, "result": {
                "data": [{"id": f"s{i}", "profile": "default",
                          "cwd": f"/source/work/{i}" if i >= 176 else "",
                          "git_repo_root": f"/source/repo/{i}" if i >= 176 else ""}
                         for i in range(offset, min(offset + 100, 229))],
                "has_more": offset + 100 < 229}}
        with patch.dict(fn.__globals__, {
            "_peer_key": lambda *a: "key", "probe_peer": lambda *a, **kw: {"authenticated": True},
            "_peer_profile_rows": lambda *a: [{"profile": "default"}],
            "_peer_ui_metadata": lambda: {}, "_peer_json": gateway,
            "_peer_control_json": control,
        }):
            rows = fn("node", {"url": "http://example.test"}, {})
        self.assertEqual(len(rows), 229)
        self.assertEqual(sum(bool(r["workspacePath"]) for r in rows), 53)
        self.assertEqual(rows[0]["workspacePath"], "")
        self.assertEqual(rows[175]["workspacePath"], "")
        self.assertEqual(rows[176]["workspacePath"], "/source/repo/176")
        self.assertEqual(rows[228]["workspacePath"], "/source/repo/228")
        self.assertEqual(len(calls), 3)
        self.assertEqual([c["request"]["offset"] for c in calls], [0, 100, 200])
        self.assertEqual(calls[0], {"op": "rpc", "peer": "node", "request":
                         {"op": "sessions", "profile": "default", "limit": 100, "offset": 0}})

    def test_paired_metadata_never_matches_a_different_session_or_profile(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_remote_sessions_for_peer"]
        def gateway(url, path, key, timeout):
            return {"data": [{"id": "right", "title": "/injected/from/title",
                              "message_count": 1, "last_active": time.time()}]}
        requests = []
        def control(request):
            requests.append(request)
            return {"ok": True, "result": {"data": [
                {"id": "wrong", "profile": request["request"]["profile"],
                 "cwd": "/source/other"}], "has_more": False}}
        with patch.dict(fn.__globals__, {
            "_peer_key": lambda *a: "key", "probe_peer": lambda *a, **kw: {"authenticated": True},
            "_peer_profile_rows": lambda *a: [{"profile": "default"}, {"profile": "work"}],
            "_peer_ui_metadata": lambda: {}, "_peer_json": gateway,
            "_peer_control_json": control,
        }):
            rows = fn("exact-alias", {"url": "http://example.test"}, {})
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["workspacePath"] for r in rows], ["", ""])
        self.assertEqual([c["peer"] for c in requests], ["exact-alias", "exact-alias"])
        self.assertEqual([c["request"]["profile"] for c in requests], ["default", "work"])

    def test_untrusted_or_malformed_peer_page_keeps_legacy_rows_unassigned(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_remote_sessions_for_peer"]
        def gateway(url, path, key, timeout):
            return {"data": [{"id": "one", "message_count": 1,
                              "last_active": time.time()}]}
        responses = [
            {"ok": False, "error": "denied"},
            {"ok": True, "result": {"data": [{"id": "one", "cwd": "relative/path"}],
                                     "has_more": False}},
            {"ok": True, "result": {"data": [{"id": "one", "cwd": "/source/path"}],
                                     "has_more": False}},
            {"ok": True, "result": {"data": [{"id": "one", "profile": "other",
                                                "cwd": "/source/path"}], "has_more": False}},
            {"ok": True, "result": {"data": [{"id": "one", "cwd": ""}],
                                     "has_more": False}},
            {"ok": True, "result": {"data": [{"id": "one", "cwd": "/source/path"}],
                                     "has_more": "false"}},
        ]
        for response in responses:
            with self.subTest(response=response), patch.dict(fn.__globals__, {
                "_peer_key": lambda *a: "key",
                "probe_peer": lambda *a, **kw: {"authenticated": True},
                "_peer_profile_rows": lambda *a: [{"profile": "default"}],
                "_peer_ui_metadata": lambda: {}, "_peer_json": gateway,
                "_peer_control_json": lambda request: response,
            }):
                rows = fn("node", {"url": "http://example.test"}, {})
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["workspacePath"], "")

    def test_incomplete_paired_pagination_discards_earlier_paths(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        source = runtime["_paired_session_paths"]
        def control(request):
            if request["request"]["offset"] == 0:
                return {"ok": True, "result": {"data": [{"id": "one", "cwd": "/source/one"}],
                                                 "has_more": True}}
            return {"ok": False, "error": "denied"}
        with patch.dict(source.__globals__, {"_peer_control_json": control}):
            self.assertEqual(source("node", "default"), {})

    def test_paired_repo_root_precedes_gateway_cwd(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_remote_sessions_for_peer"]
        with patch.dict(fn.__globals__, {
            "_peer_key": lambda *a: "key", "probe_peer": lambda *a, **kw: {"authenticated": True},
            "_peer_profile_rows": lambda *a: [{"profile": "default"}],
            "_peer_ui_metadata": lambda: {},
            "_peer_json": lambda *a: {"data": [{"id": "one", "message_count": 1,
                "last_active": time.time(), "cwd": "/gateway/subdir"}]},
            "_peer_control_json": lambda *a: {"ok": True, "result": {"data": [
                {"id": "one", "profile": "default", "cwd": "/source/subdir",
                 "git_repo_root": "/source/repo"}], "has_more": False}},
        }):
            rows = fn("node", {"url": "http://example.test"}, {})
        self.assertEqual(rows[0]["workspacePath"], "/source/repo")

    def test_control_socket_uses_bounded_line_protocol(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        call = runtime["_peer_control_json"]
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "control.sock")
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(path)
                Path(path).chmod(0o600)
                server.listen(1)
                observed = []
                def respond():
                    connection, _ = server.accept()
                    with connection:
                        observed.append(connection.recv(4096))
                        connection.sendall(b'{"ok":false,"error":"denied"}\n')
                worker = threading.Thread(target=respond)
                worker.start()
                try:
                    with patch.dict(call.__globals__, {"_PEER_CONTROL_SOCKET": path}):
                        result = call({"op": "rpc", "peer": "node", "request":
                                       {"op": "sessions", "profile": "default", "limit": 100, "offset": 0}})
                finally:
                    worker.join(timeout=3)
            self.assertEqual(result, {"ok": False, "error": "denied"})
            self.assertIn(b'"peer":"node"', observed[0])
            self.assertTrue(observed[0].endswith(b"\n"))

    def test_gateway_id_is_not_trimmed_into_a_peer_match(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_remote_sessions_for_peer"]
        with patch.dict(fn.__globals__, {
            "_peer_key": lambda *a: "key", "probe_peer": lambda *a, **kw: {"authenticated": True},
            "_peer_profile_rows": lambda *a: [{"profile": "default"}],
            "_peer_ui_metadata": lambda: {},
            "_peer_json": lambda *a: {"data": [{"id": " one ", "message_count": 1,
                "last_active": time.time()}]},
            "_peer_control_json": lambda *a: {"ok": True, "result": {"data": [
                {"id": "one", "profile": "default", "cwd": "/source/repo"}],
                "has_more": False}},
        }):
            rows = fn("node", {"url": "http://example.test"}, {})
        self.assertEqual(rows[0]["workspacePath"], "")

    def test_gateway_paths_without_paired_source_remain_unassigned(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["_remote_sessions_for_peer"]
        globals_ = fn.__globals__
        now = time.time()
        paths = []

        def peer_json(url, path, key, timeout):
            paths.append(path)
            return {"data": [
                {"id": "alpha", "title": "Alpha", "message_count": 2,
                 "last_active": now, "cwd": "/home/agent/work/alpha/subdir",
                 "git_repo_root": "/home/agent/work/alpha"},
                {"id": "beta", "title": "Beta", "message_count": 1,
                 "last_active": now, "cwd": "/home/agent/work/beta"},
                {"id": "old-peer", "title": "Legacy", "message_count": 1,
                 "last_active": now},
            ]}

        with patch.dict(globals_, {
            "_peer_key": lambda *args: "test-key",
            "probe_peer": lambda *args, **kwargs: {"authenticated": True},
            "_peer_profile_rows": lambda *args: [{"profile": "default", "title": "Hermes"}],
            "_peer_ui_metadata": lambda: {},
            "_peer_json": peer_json,
        }):
            rows = fn("host", {"url": "http://example.test:8642"}, {})

        self.assertEqual([row["workspacePath"] for row in rows], ["", "", ""])
        self.assertTrue(any("include_workspace=1" in path for path in paths))
        self.assertEqual(len({row["id"] for row in rows}), 3)

    def test_paired_only_catalog_pages_229_with_53_verified_paths_and_no_preview(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        requests = []
        def control(req):
            requests.append(req)
            if req["op"] == "status":
                return {"ok": True, "result": {"trusted": ["node"]}}
            self.assertEqual((req["op"], req["peer"], req["request"]["profile"]),
                             ("rpc", "node", "default"))
            offset = req["request"]["offset"]
            return {"ok": True, "result": {"data": [
                {"id": f"s{i}", "profile": "default", "title": f"Session {i}",
                 "message_count": 2, "last_activity_at": 1000 + i,
                 "cwd": f"/source/work/{i}" if i >= 176 else "",
                 "git_repo_root": f"/source/repo/{i}" if i >= 176 else "",
                 "preview": "private text"}
                for i in range(offset, min(offset + 100, 229))],
                "has_more": offset + 100 < 229}}
        fn = runtime["list_remote_sessions"]
        with patch.dict(fn.__globals__, {
            "_configured_peers": lambda: {}, "_read_env_values": lambda: {},
            "_peer_ui_metadata": lambda: {}, "_peer_control_json": control,
        }):
            rows = fn()
        self.assertEqual(len(rows), 229)
        self.assertEqual(len({r["id"] for r in rows}), 229)
        self.assertEqual(sum(bool(r["workspacePath"]) for r in rows), 53)
        self.assertEqual(next(r for r in rows if r["remoteSessionId"] == "s176")["workspacePath"],
                         "/source/repo/176")
        self.assertTrue(all(r["lastMsg"] == "" and not r["working"] for r in rows))
        self.assertEqual([r["request"]["offset"] for r in requests[1:]], [0, 100, 200])

    def test_paired_and_gateway_rows_merge_by_exact_alias_profile_session(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        def control(req):
            if req["op"] == "status":
                return {"ok": True, "result": {"trusted": ["node"]}}
            return {"ok": True, "result": {"data": [
                {"id": "same", "profile": "default", "title": "Paired", "message_count": 2,
                 "last_activity_at": 500, "cwd": "/verified"},
                {"id": "only-paired", "profile": "default", "title": "Solo", "message_count": 1,
                 "last_activity_at": 400, "cwd": ""}], "has_more": False}}
        def gateway(url, path, key, timeout):
            return {"data": [
                {"id": "same", "message_count": 3, "title": "Gateway", "last_active": 600},
                {"id": "only-gateway", "message_count": 1, "title": "Other", "last_active": 300}],
                "has_more": False}
        fn = runtime["list_remote_sessions"]
        with patch.dict(os.environ, {"ARES_LEGACY_GATEWAY_IMPORT": "1"}), patch.dict(fn.__globals__, {
            "_configured_peers": lambda: {"node": {"url": "http://node.test"}},
            "_read_env_values": lambda: {}, "_peer_key": lambda *args: "key",
            "probe_peer": lambda *args, **kwargs: {"authenticated": True},
            "_peer_profile_rows": lambda *args: [{"profile": "default"}],
            "_peer_ui_metadata": lambda: {}, "_peer_json": gateway,
            "_peer_control_json": control,
        }):
            rows = fn()
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(set(by_id), {"remote::node::default::same",
                                      "remote::node::default::only-paired",
                                      "remote::node::default::only-gateway"})
        self.assertEqual(len(rows), 3)
        self.assertEqual(by_id["remote::node::default::same"]["workspacePath"], "/verified")
        self.assertEqual(by_id["remote::node::default::same"]["lastMsg"], "")

    def test_invalid_roster_or_catalog_fails_closed_without_losing_gateway_rows(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["list_remote_sessions"]
        for roster in (["node", "node"], ["node", "bad::alias"],
                       ["node", 3], {"node": True}):
            with self.subTest(roster=roster), patch.dict(fn.__globals__, {
                "_configured_peers": lambda: {}, "_read_env_values": lambda: {},
                "_peer_control_json": lambda req: {"ok": True, "result": {"trusted": roster}},
            }):
                self.assertEqual(fn(), [])
        calls = []
        def broken(req):
            calls.append(req)
            if req["op"] == "status":
                return {"ok": True, "result": {"trusted": ["node"]}}
            if req["request"]["offset"] == 0:
                return {"ok": True, "result": {"data": [
                    {"id": "one", "profile": "default", "title": "One", "message_count": 1,
                     "cwd": "/source"}], "has_more": True}}
            return {"ok": True, "result": {"data": "invalid", "has_more": False}}
        with patch.dict(fn.__globals__, {
            "_configured_peers": lambda: {}, "_read_env_values": lambda: {},
            "_peer_control_json": broken,
        }):
            self.assertEqual(fn(), [])
        self.assertEqual(len(calls), 3)

    def test_join_paths_advance_by_returned_page_size(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        source = runtime["_paired_session_paths"]
        offsets = []
        def control(req):
            offset = req["request"]["offset"]
            offsets.append(offset)
            return {"ok": True, "result": {"data": [
                {"id": f"sid{i}", "profile": "default", "cwd": f"/source/{i}"}
                for i in range(offset, min(offset + 37, 101))],
                "has_more": offset + 37 < 101}}
        with patch.dict(source.__globals__, {"_peer_control_json": control}):
            paths = source("node", "default")
        self.assertEqual(len(paths), 101)
        self.assertEqual(offsets, [0, 37, 74])
        self.assertEqual(paths["sid100"], "/source/100")

    def test_paired_catalog_advances_by_actual_page_length(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["list_remote_sessions"]
        offsets = []
        def control(req):
            if req["op"] == "status":
                return {"ok": True, "result": {"trusted": ["node"]}}
            offset = req["request"]["offset"]
            offsets.append(offset)
            size = 37
            return {"ok": True, "result": {"data": [
                {"id": f"s{i}", "profile": "default", "message_count": 1,
                 "cwd": f"/work/{i}"} for i in range(offset, min(offset + size, 229))],
                 "has_more": offset + size < 229}}
        with patch.dict(fn.__globals__, {
            "_configured_peers": lambda: {}, "_read_env_values": lambda: {},
            "_peer_control_json": control,
        }):
            rows = fn()
        self.assertEqual(len(rows), 229)
        self.assertEqual(offsets, [0, 37, 74, 111, 148, 185, 222])

    def test_paired_catalog_rejects_zero_progress(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["list_remote_sessions"]
        calls = []
        def control(req):
            calls.append(req)
            if req["op"] == "status":
                return {"ok": True, "result": {"trusted": ["node"]}}
            return {"ok": True, "result": {"data": [], "has_more": True}}
        with patch.dict(fn.__globals__, {
            "_configured_peers": lambda: {}, "_read_env_values": lambda: {},
            "_peer_control_json": control,
        }):
            self.assertEqual(fn(), [])
        self.assertEqual(len(calls), 2)

    def test_malformed_paired_page_never_leaks_partial_paths_or_cross_profile_rows(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["list_remote_sessions"]
        for bad in ({"id": "wrong", "profile": "other", "message_count": 1},
                    {"id": "wrong", "profile": "default", "message_count": 1,
                     "cwd": "relative/project"},
                    {"id": "wrong", "profile": "default", "message_count": 1,
                     "cwd": "/project/../other"}):
            def control(req):
                if req["op"] == "status":
                    return {"ok": True, "result": {"trusted": ["node"]}}
                return {"ok": True, "result": {"data": [
                    {"id": "valid", "profile": "default", "message_count": 1,
                     "cwd": "/verified"}, bad], "has_more": False}}
            with self.subTest(bad=bad), patch.dict(os.environ, {"ARES_LEGACY_GATEWAY_IMPORT": "1"}), patch.dict(fn.__globals__, {
                "_configured_peers": lambda: {"node": {"url": "http://node.test"}},
                "_read_env_values": lambda: {}, "_peer_key": lambda *args: "key",
                "probe_peer": lambda *args, **kwargs: {"authenticated": True},
                "_peer_profile_rows": lambda *args: [{"profile": "default"}],
                "_peer_ui_metadata": lambda: {},
                "_peer_json": lambda *args: {"data": [{"id": "valid", "message_count": 1,
                    "last_active": 100, "title": "Legacy"}], "has_more": False},
                "_peer_control_json": control,
            }):
                rows = fn()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["workspacePath"], "")

    def test_offline_and_unpaired_discovery_cannot_assert_new_sessions(self):
        with patch.object(sys, "path", [str(RUNTIME.parent), *sys.path]):
            runtime = runpy.run_path(str(RUNTIME))
        fn = runtime["list_remote_sessions"]
        for response in ({"ok": False, "error": "unavailable"},
                         {"ok": True, "result": {"peers": [{"alias": "node", "online": True}]}}):
            with self.subTest(response=response), patch.dict(fn.__globals__, {
                "_configured_peers": lambda: {}, "_read_env_values": lambda: {},
                "_peer_control_json": lambda req: response,
            }):
                self.assertEqual(fn(), [])


if __name__ == "__main__":
    unittest.main()
