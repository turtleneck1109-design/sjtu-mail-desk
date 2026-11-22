import concurrent.futures
import csv
import http.client
import io
import json
import smtplib
import socket
import tempfile
import threading
import time
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import MailDesk, Server, parse_eml, prepare_message, MAX_FILE
from samples import sample_messages

SAMPLES = list(sample_messages())


class FakeSMTP:
    calls = []
    behavior = 'success'
    entered = threading.Event()
    release = threading.Event()

    def __init__(self, *args, **kwargs):
        if self.behavior == 'connect_fail':
            raise OSError('connection refused')

    def login(self, account, password):
        if self.behavior == 'auth_fail':
            raise smtplib.SMTPAuthenticationError(535, b'bad credentials')

    def sendmail(self, sender, recipients, data):
        self.calls.append((sender, list(recipients), data))
        if self.behavior == 'block':
            self.entered.set()
            self.release.wait(5)
        if self.behavior == 'partial':
            return {recipients[-1]: (550, b'rejected')}
        if self.behavior == 'disconnect':
            raise smtplib.SMTPServerDisconnected('lost after DATA')
        if self.behavior == 'data_reject':
            raise smtplib.SMTPDataError(554, b'refused')
        if self.behavior == 'all_refused':
            raise smtplib.SMTPRecipientsRefused({r: (550, b'refused') for r in recipients})
        if self.behavior == 'sender_reject':
            raise smtplib.SMTPSenderRefused(553, b'bad sender', sender)
        return {}

    def quit(self):
        if self.behavior == 'quit_fail':
            raise smtplib.SMTPServerDisconnected('QUIT failed')

    def close(self):
        pass


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        FakeSMTP.calls = []
        FakeSMTP.behavior = 'success'
        FakeSMTP.entered.clear()
        FakeSMTP.release.clear()
        self.app = MailDesk(self.tmp.name, smtp_factory=FakeSMTP)
        # Any accidental use of a real SMTP connector fails the test immediately.
        self.guard = patch('smtplib.SMTP_SSL', side_effect=AssertionError('Real SMTP is forbidden in tests'))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.addCleanup(self.tmp.cleanup)

    def imported(self, index=0, filename=None, raw=None):
        name, content = SAMPLES[index]
        return self.app.import_file(filename or name, raw or content)['id']

    def send(self, ids, test_to=None):
        plan = self.app.make_plan(ids, 'demo', test_to, 0)
        self.app.start(plan['id'], 'TEMP_SECRET_DO_NOT_LOG', plan['confirmation'])
        self.app.worker.join(5)
        self.assertFalse(self.app.worker.is_alive())
        return self.app.state()

    def test_parse_chinese_recipients_and_excel(self):
        msg, meta = parse_eml(SAMPLES[0][1])
        self.assertEqual(len(meta['recipients']), 3)
        self.assertEqual(meta['groups']['Bcc'][0]['address'], 'archive@example.com')
        self.assertIn('志愿者', meta['subject'])
        self.assertTrue(meta['attachments'][0]['name'].endswith('.xlsx'))
        self.assertGreater(meta['attachments'][0]['size'], 1000)
        self.assertFalse(meta['problem'])

    def test_preview_never_connects(self):
        digest = self.imported()
        self.app.state()
        self.app.detail(digest)
        self.app.make_plan([digest], 'demo')
        self.assertFalse(FakeSMTP.calls)

    def test_invalid_mail_cannot_send(self):
        digest = self.imported(3)
        self.assertEqual(self.app.state()['items'][0]['status'], 'invalid')
        with self.assertRaises(ValueError):
            self.app.make_plan([digest], 'demo')

    def test_duplicate_content_same_batch(self):
        digest = self.imported()
        result = self.app.import_file('其他名字.EML', SAMPLES[0][1])
        self.assertTrue(result['duplicate'])
        self.assertEqual(result['id'], digest)
        self.assertEqual(len(self.app.state()['items']), 1)

    def test_test_delivery_only_to_test_address(self):
        digest = self.imported()
        state = self.send([digest], 'self@example.com')
        sender, recipients, raw = FakeSMTP.calls[0]
        msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
        self.assertEqual(recipients, ['self@example.com'])
        self.assertEqual(str(msg['To']), 'self@example.com')
        self.assertIsNone(msg['Cc'])
        self.assertIsNone(msg['Bcc'])
        self.assertIsNone(msg['X-Unsent'])
        self.assertEqual(state['items'][0]['status'], 'pending')
        self.assertEqual(state['items'][0]['test_count'], 1)
        self.assertEqual(state['history'][0]['scope'], 'test:self@example.com')

    def test_real_delivery_bcc_envelope_and_payload_preserved(self):
        digest = self.imported()
        self.send([digest])
        sender, recipients, raw = FakeSMTP.calls[0]
        original = BytesParser(policy=policy.SMTP).parsebytes(SAMPLES[0][1])
        sent = BytesParser(policy=policy.SMTP).parsebytes(raw)
        self.assertIn('archive@example.com', recipients)
        self.assertIsNone(sent['Bcc'])
        self.assertEqual(str(sent['To']), str(original['To']))
        self.assertEqual(str(sent['Cc']), str(original['Cc']))
        self.assertEqual(sent.get_body(('plain',)).get_content(), original.get_body(('plain',)).get_content())
        self.assertEqual(sent.get_body(('html',)).get_content(), original.get_body(('html',)).get_content())
        self.assertEqual(list(sent.iter_attachments())[0].get_payload(decode=True), list(original.iter_attachments())[0].get_payload(decode=True))
        self.assertEqual((Path(self.tmp.name)/'drafts'/(digest+'.eml')).read_bytes(), SAMPLES[0][1])

    def test_prepare_does_not_mutate_original(self):
        before = SAMPLES[0][1]
        prepare_message(before, 'demo@sjtu.edu.cn', ['self@example.com'], 'self@example.com')
        real = BytesParser(policy=policy.SMTP).parsebytes(prepare_message(before, 'demo@sjtu.edu.cn', ['group10@example.com']))
        self.assertIn('group10@example.com', str(real['To']))

    def test_sent_cannot_repeat_across_restarts_or_reimport(self):
        digest = self.imported()
        self.send([digest])
        self.app.remove([digest])
        self.imported()
        restarted = MailDesk(self.tmp.name, smtp_factory=FakeSMTP)
        with self.assertRaises(ValueError):
            restarted.make_plan([digest], 'otheraccount')
        self.assertEqual(restarted.state()['items'][0]['status'], 'sent')

    def test_partial_retries_only_failed_recipients(self):
        digest = self.imported()
        FakeSMTP.behavior = 'partial'
        state = self.send([digest])
        self.assertEqual(state['items'][0]['status'], 'partial')
        self.assertEqual(state['job']['partial'], 1)
        retry = self.app.make_plan([digest], 'demo')
        self.assertEqual(retry['items'][0]['recipients'], ['archive@example.com'])
        FakeSMTP.behavior = 'success'
        self.send([digest])
        self.assertEqual(FakeSMTP.calls[-1][1], ['archive@example.com'])
        self.assertEqual(self.app.state()['items'][0]['status'], 'sent')

    def test_disconnect_uncertain_blocks_automatic_retry(self):
        digest = self.imported()
        FakeSMTP.behavior = 'disconnect'
        state = self.send([digest])
        self.assertEqual(state['items'][0]['status'], 'uncertain')
        with self.assertRaises(ValueError):
            self.app.make_plan([digest], 'demo')
        with self.assertRaises(ValueError):
            self.app.unlock(digest, 'real', '随便')
        self.app.unlock(digest, 'real', '已核对，允许重试')
        self.assertTrue(self.app.make_plan([digest], 'demo')['items'])
        with self.app.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit').fetchone()[0], 1)

    def test_auth_failure_stops_remaining_batch(self):
        ids = [self.imported(i) for i in (0,1)]
        FakeSMTP.behavior = 'auth_fail'
        state = self.send(ids)
        self.assertFalse(FakeSMTP.calls)
        self.assertEqual(state['job']['completed'], 1)
        self.assertEqual(state['job']['status'], 'stopped')
        self.assertEqual({i['status'] for i in state['items']}, {'failed','pending'})

    def test_failure_before_submission_is_known_failed(self):
        digest = self.imported()
        FakeSMTP.behavior = 'connect_fail'
        state = self.send([digest])
        self.assertEqual(state['items'][0]['status'], 'failed')
        self.assertTrue(state['items'][0]['eligible'])

    def test_explicit_rejections_are_known_failed(self):
        for behavior in ('all_refused','data_reject','sender_reject'):
            with self.subTest(behavior=behavior):
                digest = self.imported()
                FakeSMTP.behavior = behavior
                state = self.send([digest])
                self.assertEqual(state['items'][0]['status'], 'failed')

    def test_quit_failure_does_not_erase_acceptance(self):
        digest = self.imported()
        FakeSMTP.behavior = 'quit_fail'
        state = self.send([digest])
        self.assertEqual(state['items'][0]['status'], 'sent')

    def test_stale_attempt_becomes_uncertain_after_restart(self):
        digest = self.imported()
        plan = self.app.make_plan([digest], 'demo')
        self.app._record_begin(plan['items'][0], plan, 'interrupted', '<test@example.com>')
        restarted = MailDesk(self.tmp.name, smtp_factory=FakeSMTP)
        self.assertEqual(restarted.state()['items'][0]['status'], 'uncertain')
        self.assertEqual(restarted.state()['history'][0]['status'], 'uncertain')
        with self.assertRaises(ValueError):
            restarted.make_plan([digest], 'demo')

    def test_double_click_or_second_tab_cannot_send_twice(self):
        digest = self.imported()
        first = self.app.make_plan([digest], 'demo', delay=0)
        second = self.app.make_plan([digest], 'demo', delay=0)
        FakeSMTP.behavior = 'block'
        self.app.start(first['id'], 'secret', first['confirmation'])
        try:
            self.assertTrue(FakeSMTP.entered.wait(3))
            with self.assertRaises(ValueError):
                self.app.start(second['id'], 'secret', second['confirmation'])
            with self.assertRaises(ValueError):
                self.app.remove([digest])
            with self.assertRaises(ValueError):
                self.imported(1)
        finally:
            FakeSMTP.release.set()
            self.app.worker.join(5)
        self.assertEqual(len(FakeSMTP.calls),1)

    def test_stop_only_allows_current_submission_to_finish(self):
        ids = [self.imported(i) for i in (0,1,2)]
        plan = self.app.make_plan(ids, 'demo', delay=0)
        FakeSMTP.behavior = 'block'
        self.app.start(plan['id'], 'secret', plan['confirmation'])
        try:
            self.assertTrue(FakeSMTP.entered.wait(3))
            self.app.stop()
        finally:
            FakeSMTP.release.set()
            self.app.worker.join(5)
        self.assertEqual(len(FakeSMTP.calls),1)
        state = self.app.state()
        self.assertEqual(state['job']['status'],'stopped')
        self.assertEqual(sum(i['status']=='pending' for i in state['items']),2)

    def test_confirmation_and_plan_expiry(self):
        digest = self.imported()
        plan = self.app.make_plan([digest], 'demo')
        with self.assertRaises(ValueError):
            self.app.start(plan['id'], 'secret', 'SEND')
        self.app.plans[plan['id']]['expires'] = time.monotonic()-1
        with self.assertRaises(ValueError):
            self.app.start(plan['id'], 'secret', plan['confirmation'])
        self.assertFalse(FakeSMTP.calls)

    def test_csv_safe_and_no_password_on_disk(self):
        digest = self.imported(filename='=formula.eml')
        self.send([digest])
        data = self.app.csv_bytes()
        rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))
        self.assertEqual(rows[1][0], "'=formula.eml")
        for path in Path(self.tmp.name).rglob('*'):
            if path.is_file():
                self.assertNotIn(b'TEMP_SECRET_DO_NOT_LOG', path.read_bytes())

    def test_preview_sanitizes_active_content_and_external_images(self):
        raw = b'To: a@example.com\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<script>alert(1)</script><img src="https://example.com/track"><iframe src="https://example.com"></iframe><a href="https://example.com">Link</a><meta http-equiv="refresh" content="0; url=https://example.com"><div onclick="alert(1)" style="background:url(https://example.com)">OK</div>'
        _, meta = parse_eml(raw)
        for forbidden in ('https:', 'script', 'iframe', 'onclick', 'http-equiv', 'background:'):
            self.assertNotIn(forbidden, meta['html'])
        self.assertIn('OK', meta['html'])
        self.assertIn('Link', meta['html'])

    def test_attachment_download_matches_source(self):
        digest = self.imported()
        item = self.app.detail(digest)
        index = item['attachments'][0]['index']
        name, payload = self.app.attachment(digest, index)
        self.assertTrue(name.endswith('.xlsx'))
        self.assertTrue(payload.startswith(b'PK'))
        with self.assertRaises(ValueError):
            self.app.attachment(digest, 0)

    def test_changed_draft_cannot_send(self):
        digest = self.imported()
        plan = self.app.make_plan([digest], 'demo')
        (Path(self.tmp.name)/'drafts'/(digest+'.eml')).write_bytes(SAMPLES[1][1])
        with self.assertRaises(ValueError):
            self.app.start(plan['id'], 'secret', plan['confirmation'])
        self.assertFalse(FakeSMTP.calls)

    def test_demo_cannot_connect_to_smtp(self):
        with tempfile.TemporaryDirectory() as root:
            demo = MailDesk(root, demo=True)
            digest = demo.import_file(*SAMPLES[0])['id']
            plan = demo.make_plan([digest], 'demo', delay=0)
            with patch.object(socket, 'create_connection', side_effect=AssertionError('NO NETWORK')):
                demo.start(plan['id'], '', plan['confirmation'])
                demo.worker.join(5)
            self.assertEqual(demo.state()['job']['sent'], 1)

    def test_empty_cc_and_empty_display_group_with_bcc_are_valid(self):
        raw = b'To: undisclosed-recipients:;\r\nCc: \r\nBcc: hidden@example.com\r\nSubject: test\r\n\r\nBody'
        _, meta = parse_eml(raw)
        self.assertFalse(meta['problem'])
        self.assertEqual(meta['recipients'], ['hidden@example.com'])

    def test_malformed_attachment_encoding_is_blocked(self):
        raw = b'To: a@example.com\r\nMIME-Version: 1.0\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename="bad.xlsx"\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!!invalid!!!'
        _, meta = parse_eml(raw)
        self.assertTrue(meta['problem'])

    def test_record_failure_after_acceptance_still_blocks_retry(self):
        digest = self.imported()
        with patch.object(self.app, '_record_end', side_effect=OSError('disk unavailable')):
            state = self.send([digest])
        self.assertEqual(state['job']['status'], 'error')
        self.assertEqual(state['items'][0]['status'], 'uncertain')
        with self.assertRaises(ValueError):
            self.app.make_plan([digest], 'demo')

    def test_demo_csv_is_clearly_marked_as_simulation(self):
        with tempfile.TemporaryDirectory() as root:
            demo = MailDesk(root, demo=True)
            digest = demo.import_file(*SAMPLES[0])['id']
            plan = demo.make_plan([digest], 'demo', delay=0)
            demo.start(plan['id'], '', plan['confirmation'])
            demo.worker.join(5)
            self.assertIn('模拟（未发送）', demo.csv_bytes().decode('utf-8-sig'))

    def test_invalid_parameters(self):
        digest = self.imported()
        for account in ('bad@sjtu.edu.cn', '', 'a\r\nBcc:x@y.com'):
            with self.assertRaises(ValueError):
                self.app.make_plan([digest], account)
        for delay in (-1,61,float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                self.app.make_plan([digest], 'demo', delay=delay)
        with self.assertRaises(ValueError):
            self.app.import_file('not-eml.txt', b'abc')
        with self.assertRaises(ValueError):
            self.app.import_file('empty.eml', b'')
        with self.assertRaises(ValueError):
            self.app.import_file('large.eml', b'a'*(MAX_FILE+1))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = MailDesk(self.tmp.name, demo=True)
        self.server = Server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.app.worker:
            self.app.worker.join(5)
        self.tmp.cleanup()

    def request(self, method, path, data=None, headers=None, auth=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        hdr = {'X-MailDesk-Token': self.server.token} if auth else {}
        hdr.update(headers or {})
        if isinstance(data, dict):
            data=json.dumps(data)
            hdr['Content-Type']='application/json'
        conn.request(method,path,body=data,headers=hdr)
        response=conn.getresponse()
        result=(response.status,dict(response.getheaders()),response.read())
        conn.close()
        return result

    def test_api_requires_token(self):
        self.assertEqual(self.request('GET','/api/state',auth=False)[0],403)
        self.assertEqual(self.request('GET','/api/state')[0],200)

    def test_reject_cross_origin_and_host_rebinding(self):
        self.assertEqual(self.request('POST','/api/stop',{}, {'Origin':'https://other.example'})[0],403)
        self.assertEqual(self.request('GET','/api/state',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('GET','/api/state',headers={'Sec-Fetch-Site':'cross-site'})[0],403)

    def test_static_security_and_no_file_traversal(self):
        code,headers,body=self.request('GET','/',auth=False)
        self.assertEqual(code,200)
        self.assertIn('frame-ancestors',headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn('交邮'.encode(),body)
        self.assertEqual(self.request('GET','/../app.py')[0],404)
        self.assertEqual(self.request('GET','/data/send_results.sqlite3')[0],404)

    def test_end_to_end_import_plan_confirm_refresh_export(self):
        code,_,body=self.request('POST','/api/import?name=sample.eml',SAMPLES[0][1])
        self.assertEqual(code,200)
        digest=json.loads(body)['id']
        code,_,body=self.request('POST','/api/plan',{'ids':[digest],'account':'demo','test_to':'self@example.com','delay':0})
        self.assertEqual(code,200)
        plan=json.loads(body)
        code,_,_=self.request('POST','/api/start',{'plan_id':plan['id'],'password':'','confirmation':plan['confirmation']})
        self.assertEqual(code,200)
        self.app.worker.join(5)
        code,_,body=self.request('GET','/api/state')
        self.assertEqual(json.loads(body)['job']['sent'],1)
        self.assertEqual(self.request('GET','/api/export')[0],200)
        self.assertEqual(self.request('POST','/api/remove',{'ids':[digest]})[0],200)
        self.assertEqual(len(self.app.state()['history']),1)

    def test_bad_requests_have_no_side_effects(self):
        self.assertEqual(self.request('POST','/api/start',{})[0],400)
        self.assertEqual(self.request('POST','/api/remove',{'ids':'bad'})[0],400)
        self.assertEqual(self.request('POST','/api/stop',b'not-json')[0],400)
        self.assertIsNone(self.app.state()['job'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
