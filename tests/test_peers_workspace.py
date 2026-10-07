"""Peer workspace safety and navigation contracts; no live service mutations."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PeerWorkspaceTests(unittest.TestCase):
    def test_consent_gates_and_changed_identity(self):
        script = """
        const m = require('./PeerWorkspaceModel.js');
        const peer = {id:'device-1', alias:'desk', fingerprint:'a'.repeat(64), paired:false, online:true};
        const checks = [
          m.canPair(peer, peer.fingerprint, false, '12345678', false),
          m.canPair(peer, peer.fingerprint, true, '', false),
          m.canPair(peer, 'b'.repeat(64), true, '12345678', false),
          m.canPair({...peer, online:false}, peer.fingerprint, true, '12345678', false),
          m.canPair({...peer, online:undefined}, peer.fingerprint, true, '12345678', false),
          m.canPair(peer, peer.fingerprint, true, '12345678', true),
          m.canPair(peer, peer.fingerprint, true, '12345678', false),
          m.canPair(peer, peer.fingerprint, true, 'secret', false),
          m.canPair({...peer, fingerprint:'sha256:abc'}, 'sha256:abc', true, '12345678', false),
          m.canRevoke(peer, true, false),
          m.canRevoke({...peer, paired:true}, false, false),
          m.canRevoke({...peer, paired:true}, true, false),
          m.selectedPeer([peer], '') === null
        ];
        console.log(JSON.stringify(checks));
        """
        result = subprocess.run(['node', '-e', script], cwd=ROOT, check=True, text=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout), [False, False, False, False, False, False, True,
                                                    False, False, False, False, True, True])

    def test_catalog_label_uses_source_fields_without_claiming_live_status(self):
        script = """
        const m = require('./PeerWorkspaceModel.js');
        const rows = [
          {id:'session-1', title:'Build\\nproject', profile:'default', cwd:'/work/project/subdir', git_repo_root:'/work/project'},
          {id:'session-2', title:'Scheduled', profile:'default', cwd:'', git_repo_root:''},
        ];
        console.log(JSON.stringify(rows.map(m.sessionSummary)));
        """
        result = subprocess.run(['node', '-e', script], cwd=ROOT, check=True, text=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout), ['Build project · default · project',
                                                     'Scheduled · default · Unassigned'])
        qml = (ROOT / 'PeersWorkspace.qml').read_text()
        self.assertIn('PeerModel.sessionSummary(modelData)', qml)
        self.assertIn('Session catalog (read-only)', qml)
        self.assertNotIn('status unknown', qml)

    def test_sensitive_code_not_presented_or_logged_and_no_auto_pair(self):
        qml = (ROOT / 'PeersWorkspace.qml').read_text()
        model = (ROOT / 'PeerWorkspaceModel.js').read_text()
        self.assertIn('echoMode: TextInput.Password', qml)
        self.assertIn('onActivated: workspace.pair()', qml)
        self.assertIn('enabled: workspace.pairReady', qml)
        self.assertIn('confirmed === true', model)
        self.assertIn('pin === String(fingerprint)', model)
        self.assertIn('/^[0-9a-fA-F]{64}$/.test(pin)', model)
        self.assertIn('/^[0-9]{8}$/.test(secret)', model)
        self.assertIn('/^[A-Za-z0-9_-]{1,64}$/.test(workspace.pairAlias.trim())', qml)
        self.assertIn('clearConsent()\n    bridge.request("POST", "/v1/peers/pair"', qml)
        self.assertNotIn('console.log', qml + model)
        self.assertNotIn('value.error', qml + model)
        self.assertNotIn('onVisibleChanged: if (visible) pair()', qml)
        self.assertIn('encodeURIComponent(peer.alias) + "/revoke", { confirmed: true }', qml)
        self.assertIn('enabled: workspace.revokeReady', qml)

    def test_navigation_is_separate_from_project_map_and_control_room(self):
        panel = (ROOT / 'Panel.qml').read_text()
        home = (ROOT / 'AresWorkspace.qml').read_text()
        self.assertIn('function openPeers()', panel)
        self.assertIn('visible: !root.extendedMode && root.section === "peers"', panel)
        self.assertIn('PeersWorkspace {', panel)
        self.assertIn('onActivated: controller.openPeers()', home)
        self.assertIn('onActivated: controller.openControlRoom()', home)
        self.assertIn('ControlRoom {', panel)
        self.assertIn('AresWorkspace {', panel)


if __name__ == '__main__':
    unittest.main()
