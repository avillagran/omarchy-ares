"""Optional Ares Herdr socket must not hide the default peer inventory."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peer.herdr_source import LocalSource


class OptionalAresSocketTests(unittest.TestCase):
    def test_source_lists_dedicated_session_only_when_its_socket_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            default = Path(directory)/'default.sock'
            dedicated = Path(directory)/'ares.sock'
            source = LocalSource(databases={}, sockets=[str(default)],
                                 optional_sockets=[str(dedicated)])
            snapshot = {'tabs':[], 'panes':[], 'agents':[], 'workspaces':[]}
            with patch.object(source, '_snapshot', return_value=snapshot) as observe:
                self.assertEqual(source.inventory(), {'terminals':[], 'complete':True})
                self.assertEqual([call.args[0] for call in observe.call_args_list], [str(default)])
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(dedicated))
                    observe.reset_mock()
                    self.assertEqual(source.inventory(), {'terminals':[], 'complete':True})
                    self.assertEqual([call.args[0] for call in observe.call_args_list],
                                     [str(default),str(dedicated)])


if __name__ == '__main__':
    unittest.main()
