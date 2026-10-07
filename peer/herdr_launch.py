"""Foreground Hermes child in a newly provisioned Herdr pane only.

This code never allocates a PTY, reports an agent claim, or sends input.
"""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys


def main(argv):
    if len(argv) != 6:
        return 2
    _, hermes, profile, source, sid, destination = argv
    target = Path(destination)
    if target.exists() or os.environ.get('HERDR_PANE_ID') is None:
        return 2
    reader, writer = os.pipe()
    child = None
    try:
        child = subprocess.Popen([hermes, '--profile', profile, 'chat', '--cli',
                                  '--resume', sid, '--source', source, '--exact-resume',
                                  '--exact-resume-ready-fd', str(writer)],
                                 pass_fds=(writer,))  # stdio remains Herdr's PTY
        os.close(writer)
        writer = -1
        if not select.select([reader], [], [], 60)[0]:
            return 3
        raw = os.read(reader, 1025)
        if not raw or len(raw) > 1024 or not raw.endswith(b'\n'):
            return 3
        record = json.loads(raw)
        if (not isinstance(record, dict) or record.get('protocol') != 'hermes-exact-resume-ready-v1'
                or record.get('sessionId') != sid or record.get('source') != source
                or record.get('pid') != child.pid or child.poll() is not None):
            return 3
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        return child.wait()
    finally:
        os.close(reader)
        if writer >= 0:
            os.close(writer)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
