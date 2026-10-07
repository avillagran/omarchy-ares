"""Output-only peer observation through the real executable."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / 'bin/ares-peer-observe-client'
class PeerObserveClientTests(unittest.TestCase):
    def test_poll_is_bounded_and_stdin_never_sent(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                data = json.dumps({'ok':True,'transport':'peer','encoding':'base64','frame':base64.b64encode(b'owner output\r\n').decode(),'terminalId':'term-1'}).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_POST(self): requests.append(('POST',self.path)); self.send_error(405)
            def log_message(self,*args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                state=Path(directory)/'omarchy/ares'; state.mkdir(parents=True)
                token=state/'access.token'; token.write_text('private-secret'); token.chmod(0o600)
                env=dict(os.environ,XDG_STATE_HOME=directory,ARES_API_URL='http://127.0.0.1:'+str(server.server_port))
                proc=subprocess.Popen([str(CLIENT),'room-1'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                proc.stdin.write(b'NEVER-FORWARD-THIS\n'); proc.stdin.flush(); time.sleep(.8); proc.terminate()
                out,err=proc.communicate(timeout=3)
                self.assertIn(b'owner output',out)
                self.assertNotIn(b'NEVER-FORWARD',out)
                self.assertNotIn(b'private-secret',err)
                self.assertTrue(requests)
                self.assertTrue(all(path.startswith('/v1/tty/rooms/room-1/observe?cols=') for path,_ in requests))
                self.assertTrue(all(auth=='Bearer private-secret' for _,auth in requests))
                count = len(requests)
                time.sleep(.7)
                self.assertEqual(len(requests), count, 'polling must stop when the PTY client exits')
        finally: server.shutdown(); server.server_close()
    def test_http_error_and_oversize_frame_never_emit_untrusted_bytes(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({'ok':True,'transport':'peer','encoding':'base64',
                    'frame':base64.b64encode(b'x'*8193).decode(),'terminalId':'term-1'}).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                state=Path(directory)/'omarchy/ares';state.mkdir(parents=True)
                token=state/'access.token';token.write_text('secret');token.chmod(0o600)
                env=dict(os.environ,XDG_STATE_HOME=directory,ARES_API_URL='http://127.0.0.1:'+str(server.server_port))
                run=subprocess.run([str(CLIENT),'room-1'],env=env,capture_output=True,timeout=3)
                self.assertNotEqual(run.returncode,0)
                self.assertEqual(run.stdout,b'')
                self.assertNotIn(b'secret',run.stderr)
        finally: server.shutdown(); server.server_close()
    def test_missing_private_token_and_malformed_room_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            env=dict(os.environ,XDG_STATE_HOME=directory)
            for room in ('../other','room/id',''):
                run=subprocess.run([str(CLIENT),room],env=env,capture_output=True,timeout=3)
                self.assertNotEqual(run.returncode,0); self.assertEqual(run.stdout,b'')
            run=subprocess.run([str(CLIENT),'room-1'],env=env,capture_output=True,timeout=3)
            self.assertNotEqual(run.returncode,0); self.assertEqual(run.stdout,b'')

    def test_redirect_never_forwards_bearer_even_to_another_loopback_port(self):
        seen=[]
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                self.send_response(200); self.end_headers()
            def log_message(self,*args): pass
        destination=ThreadingHTTPServer(('127.0.0.1',0),Destination)
        threading.Thread(target=destination.serve_forever,daemon=True).start()
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location',f'http://127.0.0.1:{destination.server_port}/stolen')
                self.end_headers()
            def log_message(self,*args): pass
        origin=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        threading.Thread(target=origin.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                state=Path(directory)/'omarchy/ares';state.mkdir(parents=True)
                token=state/'access.token';token.write_text('secret');token.chmod(0o600)
                env=dict(os.environ,XDG_STATE_HOME=directory,ARES_API_URL=f'http://127.0.0.1:{origin.server_port}')
                run=subprocess.run([str(CLIENT),'room-1'],env=env,capture_output=True,timeout=3)
                self.assertNotEqual(run.returncode,0)
                self.assertEqual(run.stdout,b'')
                self.assertEqual(seen,[])
        finally:
            origin.shutdown();origin.server_close();destination.shutdown();destination.server_close()

    def test_unterminated_clipboard_escape_is_never_emitted(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data=json.dumps({'ok':True,'transport':'peer','encoding':'base64',
                    'frame':base64.b64encode(b'prefix\x1b]52;c;secret').decode(),
                    'terminalId':'term-1'}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(data)))
                self.end_headers();self.wfile.write(data)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                state=Path(directory)/'omarchy/ares';state.mkdir(parents=True)
                token=state/'access.token';token.write_text('secret');token.chmod(0o600)
                env=dict(os.environ,XDG_STATE_HOME=directory,ARES_API_URL=f'http://127.0.0.1:{server.server_port}')
                proc=subprocess.Popen([str(CLIENT),'room-1'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                time.sleep(.3);proc.terminate();out,_=proc.communicate(timeout=3)
                self.assertIn(b'prefix',out)
                self.assertNotIn(b'\x1b]52',out)
        finally:server.shutdown();server.server_close()
