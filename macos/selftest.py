"""Exercise the actual frozen runtime without SMTP or a browser."""
import hashlib
import http.client
import json
import platform
import smtplib
import sys
import tempfile
import threading
from email import policy
from email.parser import BytesParser
from pathlib import Path


def run():
    from app import APP_VERSION, BASE, MailDesk, Server, create_tls_context, prepare_message
    from samples import sample_messages
    checks = []
    original_smtp = smtplib.SMTP_SSL
    def forbidden(*args, **kwargs):
        raise AssertionError('Real SMTP must never run during self-test')
    smtplib.SMTP_SSL = forbidden
    server = None
    thread = None
    try:
        context = create_tls_context()
        ca_count = context.cert_store_stats()['x509_ca']
        if getattr(sys, 'frozen', False):
            assert ca_count > 50, 'Missing bundled TLS trust roots'
            checks.append('bundled-tls-certificates')
        assert context.check_hostname
        checks.append('tls-hostname-verification')
        for name in ('index.html', 'app.js', 'style.css'):
            assert (BASE / 'static' / name).is_file(), name
        checks.append('bundled-interface-assets')
        with tempfile.TemporaryDirectory(prefix='maildesk-offline-') as temp:
            app = MailDesk(temp, demo=True)
            filename, raw = next(sample_messages())
            digest = app.import_file(filename, raw)['id']
            assert app.import_file('duplicate.eml', raw)['duplicate']
            checks.append('import-and-deduplication')
            detail = app.detail(digest)
            name, payload = app.attachment(digest, detail['attachments'][0]['index'])
            assert name.endswith('.xlsx') and payload.startswith(b'PK')
            outbound = BytesParser(policy=policy.SMTP).parsebytes(prepare_message(raw, 'demo@sjtu.edu.cn', ['self@example.com'], 'self@example.com'))
            assert str(outbound['To']) == 'self@example.com' and outbound['Cc'] is None and outbound['Bcc'] is None
            assert list(outbound.iter_attachments())[0].get_payload(decode=True) == payload
            checks.append('attachment-and-test-recipient-integrity')
            server = Server(app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, path, data=None):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                headers = {'X-MailDesk-Token': server.token}
                body = json.dumps(data) if data is not None else None
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                result = response.read()
                conn.close()
                assert response.status == 200, (path, response.status)
                return result
            assert '交邮'.encode() in request('GET', '/')
            request('GET', '/app.js')
            request('GET', '/style.css')
            checks.append('local-http-service')
            plan = json.loads(request('POST', '/api/plan', {'ids': [digest], 'account': 'demo', 'test_to': 'self@example.com', 'delay': 0}))
            request('POST', '/api/start', {'plan_id': plan['id'], 'password': '', 'confirmation': plan['confirmation']})
            app.worker.join(10)
            assert not app.worker.is_alive()
            state = json.loads(request('GET', '/api/state'))
            assert state['job']['sent'] == 1 and state['items'][0]['status'] == 'pending'
            checks.append('simulated-submission-no-smtp')
            assert '模拟（未发送）'.encode() in request('GET', '/api/export')
            restored = MailDesk(temp, demo=True)
            assert restored.state()['items'][0]['test_count'] == 1
            checks.append('sqlite-persistence-and-csv')
            assert hashlib.sha256((Path(temp) / 'drafts' / (digest + '.eml')).read_bytes()).hexdigest() == digest
            checks.append('original-draft-preserved')
    finally:
        smtplib.SMTP_SSL = original_smtp
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(5)
    print(json.dumps({'ok': True, 'version': APP_VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
                      'architecture': platform.machine(), 'python': platform.python_version(),
                      'ca_count': ca_count, 'checks': checks}, ensure_ascii=False), flush=True)
