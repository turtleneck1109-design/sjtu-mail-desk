#!/usr/bin/env python3
"""Headless native-bundle checks. Uses temporary demo data; never sends mail."""
import argparse
import http.client
import json
import os
import queue
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


def verify(app):
    app = app.resolve()
    launcher = app/'Contents/MacOS/交邮'
    engine = app/'Contents/Resources/engine/sjtu-mail-engine'
    clean_env = {'PATH': '/nonexistent', 'TMPDIR': tempfile.gettempdir()}
    results = []
    for arch in ('arm64', 'x86_64'):
        output = subprocess.check_output(['/usr/bin/arch', '-'+arch, str(launcher), '--self-test'],
                                         env=clean_env, text=True, timeout=60)
        result = json.loads(output)
        assert result['ok'] and result['frozen'] and result['architecture'] == arch, result
        results.append(result)
    magics = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
              b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
    binary_count = 0
    for path in app.rglob('*'):
        if not path.is_file() or path.is_symlink(): continue
        with path.open('rb') as file: magic = file.read(4)
        if magic not in magics: continue
        binary_count += 1
        arch = subprocess.check_output(['lipo', '-archs', str(path)], text=True)
        assert 'arm64' in arch and 'x86_64' in arch, str(path)
        dependencies = subprocess.check_output(['otool', '-L', str(path)], text=True)
        for line in dependencies.splitlines():
            if line.startswith('\t'):
                library = line.strip().split(' (', 1)[0]
                assert not library.startswith(('/opt/', '/usr/local/', '/Library/Frameworks/', '/Users/')), (str(path), library)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    with tempfile.TemporaryDirectory(prefix='maildesk-stop-') as root:
        process = subprocess.Popen([str(engine), '--demo', '--no-browser', '--data-dir', root],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=clean_env)
        output = queue.Queue()
        def read():
            for line in process.stdout: output.put(line)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            url = None
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                line = output.get(timeout=15)
                if line.startswith('页面地址：'):
                    url = urlsplit(line.removeprefix('页面地址：').strip())
                    break
            assert url and url.hostname == '127.0.0.1'
            token = url.fragment.removeprefix('token=')
            def request(method, path, data=None):
                connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
                connection.request(method, path, json.dumps(data) if data is not None else None,
                                   {'X-MailDesk-Token': token})
                response = connection.getresponse()
                body = response.read()
                connection.close()
                assert response.status == 200, (path, response.status)
                return json.loads(body)
            state = request('GET', '/api/state')
            ids = [item['id'] for item in state['items'] if item['eligible']][:2]
            assert len(ids) == 2
            plan = request('POST', '/api/plan', {'ids': ids, 'account': 'demo', 'delay': 60})
            request('POST', '/api/start', {'plan_id': plan['id'], 'confirmation': plan['confirmation'], 'password': ''})
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                state = request('GET', '/api/state')
                if state['job']['completed'] == 1: break
                time.sleep(0.05)
            assert state['job']['completed'] == 1
            process.terminate()
            assert process.wait(timeout=15) == 0
            with sqlite3.connect(Path(root)/'send_results.sqlite3') as db:
                rows = db.execute('SELECT status FROM attempts').fetchall()
                assert rows == [('sent',)], rows
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=2)
            process.stdout.close()
    return {'ok': True, 'native_self_tests': results, 'universal_binaries': binary_count,
            'external_python_required': False, 'graceful_stop_preserves_result': True,
            'real_smtp_used': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.app), ensure_ascii=False, indent=2))
