#!/usr/bin/env python3
"""Local-only EML mail desk. Python 3.9+, standard library only."""
import argparse
import base64
import copy
import csv
import fcntl
import hashlib
import html
import io
import json
import math
import os
import re
import secrets
import signal
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote

BASE = Path(__file__).resolve().parent
APP_VERSION = '1.1.0'
MAX_FILE = 32 * 1024 * 1024
SMTP_HOST, SMTP_PORT = 'mail.sjtu.edu.cn', 465
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")
ACCOUNT_RE = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')
BLOCKED = {'sent', 'attempting', 'uncertain'}


def default_data_root(demo=False):
    """Never write runtime data into a signed or read-only application bundle."""
    if getattr(sys, 'frozen', False):
        return Path.home() / 'Library' / 'Application Support' / 'SJTUMailDesk' / ('demo_data' if demo else 'data')
    return BASE / ('demo_data' if demo else 'data')


def create_tls_context():
    # Frozen Python cannot rely on certificate paths from the build machine.
    if getattr(sys, 'frozen', False):
        return ssl.create_default_context(cafile=str(BASE / 'certs' / 'cacert.pem'))
    return ssl.create_default_context()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def valid_address(value):
    return isinstance(value, str) and len(value) <= 254 and bool(EMAIL_RE.fullmatch(value))


def clean_text(value):
    return ''.join(c for c in str(value) if c in '\n\t' or ord(c) >= 32)


class PreviewHTML(HTMLParser):
    """Retain simple email layout; remove active content and external resources."""
    tags = {'p', 'div', 'span', 'br', 'hr', 'b', 'strong', 'i', 'em', 'u', 's',
            'h1', 'h2', 'h3', 'h4', 'ul', 'ol', 'li', 'table', 'thead', 'tbody',
            'tfoot', 'tr', 'td', 'th', 'blockquote', 'pre', 'code', 'img', 'a'}
    skip_tags = {'script', 'style', 'iframe', 'object', 'svg', 'math', 'template'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self.skipping += 1
        if self.skipping or tag not in self.tags:
            return
        kept = []
        for name, value in attrs:
            value = value or ''
            if name in {'title', 'alt', 'colspan', 'rowspan', 'width', 'height', 'align'}:
                kept.append((name, value))
            elif name == 'style' and not re.search(r'url\s*\(|expression|@import|behavior|position\s*:', value, re.I):
                kept.append((name, value))
            elif tag == 'img' and name == 'src' and re.fullmatch(r'data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+', value):
                kept.append((name, value))
        self.out.append('<' + tag + ''.join(' %s="%s"' % (k, html.escape(v, quote=True)) for k, v in kept) + '>')

    def handle_endtag(self, tag):
        if tag in self.skip_tags:
            self.skipping = max(0, self.skipping - 1)
            return
        if not self.skipping and tag in self.tags:
            self.out.append('</' + tag + '>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in self.skip_tags:
            self.skipping = max(0, self.skipping - 1)

    def handle_data(self, data):
        if not self.skipping:
            self.out.append(html.escape(data))


def parse_eml(raw):
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
    problem = []
    groups, recipients = {}, []
    for key in ('To', 'Cc', 'Bcc'):
        values = msg.get_all(key, [])
        if any(getattr(v, 'defects', ()) for v in values):
            problem.append(key + ' 地址格式有误')
        addresses = []
        parsed = [(address.display_name, address.addr_spec) for value in values for address in value.addresses]
        for name, addr in parsed:
            if not valid_address(addr):
                problem.append(key + ' 中存在无效地址：' + clean_text(addr or '(空地址)'))
                continue
            addr = addr.lower()
            addresses.append({'name': clean_text(name), 'address': addr})
            if addr not in recipients:
                recipients.append(addr)
        groups[key] = addresses
    if not recipients:
        problem.append('没有有效收件人')
    if len(msg.get_all('Subject', [])) > 1:
        problem.append('存在多个主题字段')
    if any(part.defects for part in msg.walk()):
        problem.append('邮件结构不完整，请重新导出 .eml')
    attachments = []
    for index, part in enumerate(msg.walk()):
        if part.get_filename() or part.get_content_disposition() == 'attachment':
            payload = part.get_payload(decode=True)
            attachments.append({'index': index, 'name': clean_text(part.get_filename() or '未命名附件'),
                                'size': len(payload or b''), 'type': part.get_content_type()})
    plain, rich = '', ''
    try:
        part = msg.get_body(preferencelist=('plain',))
        if part:
            plain = part.get_content()
        part = msg.get_body(preferencelist=('html',))
        if part:
            sanitizer = PreviewHTML()
            sanitizer.feed(part.get_content())
            rich = ''.join(sanitizer.out)
    except (LookupError, UnicodeError, ValueError, KeyError):
        problem.append('正文编码无法解析')
    if any(part.defects for part in msg.walk()):
        problem.append('邮件结构不完整，请重新导出 .eml')
    return msg, {'subject': clean_text(str(msg.get('Subject', '(无主题)'))),
                 'groups': groups, 'recipients': recipients, 'attachments': attachments,
                 'plain': clean_text(plain), 'html': rich, 'problem': '；'.join(dict.fromkeys(problem))}


def prepare_message(raw, sender, recipients, test_to=None, message_id=None):
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
    remove = {'from', 'sender', 'bcc', 'return-path', 'date', 'message-id', 'x-unsent',
              'received', 'dkim-signature', 'authentication-results', 'delivered-to',
              'disposition-notification-to', 'return-receipt-to'}
    for key in list(msg.keys()):
        if key.lower() in remove or key.lower().startswith(('resent-', 'arc-')):
            del msg[key]
    msg['From'] = sender
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = message_id or make_msgid(domain='sjtu.edu.cn')
    if test_to:
        for key in ('To', 'Cc'):
            if key in msg:
                del msg[key]
        msg['To'] = test_to
    return msg.as_bytes(policy=policy.SMTP)


class DemoSMTP:
    """No network: mimic the public SMTP_SSL operations used by the worker."""
    def __init__(self, *args, **kwargs):
        pass

    def login(self, account, password):
        pass

    def sendmail(self, sender, recipients, data):
        time.sleep(0.15)
        return {}

    def quit(self):
        pass

    def close(self):
        pass


class MailDesk:
    def __init__(self, root, demo=False, smtp_factory=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.files = self.root / 'drafts'
        self.files.mkdir(exist_ok=True, mode=0o700)
        self.demo = demo
        self.smtp_factory = smtp_factory or (DemoSMTP if demo else smtplib.SMTP_SSL)
        self.lock = threading.RLock()
        self.plans = {}
        self.job = None
        self.worker = None
        self.stop_event = threading.Event()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, meta TEXT NOT NULL, imported_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    hash TEXT NOT NULL, scope TEXT NOT NULL, recipient TEXT NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(hash,scope,recipient));
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, hash TEXT, filename TEXT, subject TEXT, scope TEXT,
                    sender TEXT, recipients TEXT, accepted TEXT, rejected TEXT,
                    status TEXT, detail TEXT, message_id TEXT, started_at TEXT, finished_at TEXT);
                CREATE TABLE IF NOT EXISTS audit (
                    time TEXT, hash TEXT, scope TEXT, detail TEXT);
            ''')
            db.execute("UPDATE deliveries SET status='uncertain', detail='程序在上次提交期间退出，需人工核对' WHERE status='attempting'")
            db.execute("UPDATE attempts SET status='uncertain', detail='程序在上次提交期间退出，需人工核对', finished_at=? WHERE status='attempting'", (now(),))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'send_results.sqlite3', timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def busy(self):
        return self.job is not None and self.job['status'] in ('running', 'stopping')

    def import_file(self, filename, raw):
        if not isinstance(filename, str) or not filename.lower().endswith('.eml'):
            raise ValueError('请选择 .eml 文件')
        if not raw or len(raw) > MAX_FILE:
            raise ValueError('文件为空或超过本工具的 32 MB 单文件上限')
        filename = clean_text(filename.replace('\\', '/').split('/')[-1])[:240]
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock, self.db() as db:
            if self.busy():
                raise ValueError('发送期间不能导入，请等待本批次结束')
            if db.execute('SELECT 1 FROM drafts WHERE id=?', (digest,)).fetchone():
                return {'id': digest, 'duplicate': True}
            _, meta = parse_eml(raw)
            target = self.files / (digest + '.eml')
            temp = target.with_suffix('.tmp')
            temp.write_bytes(raw)
            os.chmod(temp, 0o600)
            os.replace(temp, target)
            db.execute('INSERT INTO drafts VALUES (?,?,?,?)',
                       (digest, filename, json.dumps(meta, ensure_ascii=False), now()))
            return {'id': digest, 'duplicate': False, 'problem': meta['problem']}

    def _draft(self, db, digest):
        row = db.execute('SELECT * FROM drafts WHERE id=?', (digest,)).fetchone()
        if not row:
            raise ValueError('草稿不存在，请刷新列表')
        return dict(row), json.loads(row['meta'])

    def _delivery(self, db, digest, scope):
        return {r['recipient']: dict(r) for r in db.execute(
            'SELECT * FROM deliveries WHERE hash=? AND scope=?', (digest, scope))}

    @staticmethod
    def aggregate(meta, delivery):
        if meta['problem']:
            return 'invalid'
        states = [delivery.get(r, {}).get('status', 'pending') for r in meta['recipients']]
        if any(s in ('uncertain', 'attempting') for s in states):
            return 'uncertain'
        if states and all(s == 'sent' for s in states):
            return 'sent'
        if 'sent' in states:
            return 'partial'
        if 'failed' in states:
            return 'failed'
        return 'pending'

    def state(self):
        with self.lock, self.db() as db:
            items = []
            for row in db.execute('SELECT * FROM drafts ORDER BY imported_at,id'):
                meta = json.loads(row['meta'])
                delivery = self._delivery(db, row['id'], 'real')
                item = {'id': row['id'], 'filename': row['filename'],
                        **{k: v for k, v in meta.items() if k not in ('plain', 'html')}}
                item['status'] = self.aggregate(meta, delivery)
                item['eligible'] = not meta['problem'] and any(delivery.get(r, {}).get('status') not in BLOCKED for r in meta['recipients'])
                item['test_count'] = db.execute("SELECT COUNT(*) FROM attempts WHERE hash=? AND scope LIKE 'test:%' AND status IN ('sent','partial')", (row['id'],)).fetchone()[0]
                items.append(item)
            history = [dict(r) for r in db.execute('SELECT * FROM attempts ORDER BY started_at DESC,rowid DESC LIMIT 200')]
            return {'demo': self.demo, 'items': items, 'job': copy.deepcopy(self.job), 'history': history,
                    'smtp': {'host': SMTP_HOST, 'port': SMTP_PORT}, 'max_file': MAX_FILE, 'desktop': bool(getattr(sys, 'frozen', False)), 'version': APP_VERSION}

    def detail(self, digest):
        with self.lock, self.db() as db:
            row, meta = self._draft(db, digest)
            delivery = self._delivery(db, digest, 'real')
            return {'id': digest, 'filename': row['filename'], **meta, 'deliveries': delivery}

    def remove(self, ids):
        with self.lock, self.db() as db:
            if self.busy():
                raise ValueError('发送期间不能移除草稿')
            for digest in ids:
                self._draft(db, digest)
            for digest in ids:
                db.execute('DELETE FROM drafts WHERE id=?', (digest,))
                (self.files / (digest + '.eml')).unlink(missing_ok=True)
            self.plans.clear()

    def attachment(self, digest, index):
        with self.lock, self.db() as db:
            self._draft(db, digest)
            msg = BytesParser(policy=policy.SMTP).parsebytes((self.files / (digest + '.eml')).read_bytes())
            for i, part in enumerate(msg.walk()):
                if i == index and (part.get_filename() or part.get_content_disposition() == 'attachment'):
                    return clean_text(part.get_filename() or '附件'), part.get_payload(decode=True) or b''
        raise ValueError('附件不存在')

    def make_plan(self, ids, account, test_to=None, delay=2):
        if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
            raise ValueError('请填写 jAccount 用户名，不含 @sjtu.edu.cn')
        if test_to is not None and not valid_address(test_to):
            raise ValueError('请填写有效测试邮箱')
        if not isinstance(delay, (int, float)) or not math.isfinite(delay) or not 0 <= delay <= 60:
            raise ValueError('发送间隔须为 0–60 秒')
        if not isinstance(ids, list) or not ids or len(ids) > 1000 or any(not isinstance(i, str) for i in ids):
            raise ValueError('请选择 1–1000 封草稿')
        if test_to and len(set(ids)) != 1:
            raise ValueError('测试时请只选择一封草稿')
        test_to = test_to.lower() if test_to else None
        scope = 'test:' + test_to if test_to else 'real'
        with self.lock, self.db() as db:
            if self.busy():
                raise ValueError('已有发送任务正在进行')
            items, skipped = [], []
            for digest in dict.fromkeys(ids):
                row, meta = self._draft(db, digest)
                if meta['problem']:
                    raise ValueError(row['filename'] + '：' + meta['problem'])
                original = [test_to] if test_to else meta['recipients']
                delivery = self._delivery(db, digest, scope)
                eligible = [r for r in original if delivery.get(r, {}).get('status') not in BLOCKED]
                omitted = [r for r in original if r not in eligible]
                if omitted:
                    skipped.append({'filename': row['filename'], 'recipients': omitted})
                if eligible:
                    raw = (self.files / (digest + '.eml')).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != digest:
                        raise ValueError('草稿文件已被外部修改，请移除后重新导入')
                    items.append({'id': digest, 'filename': row['filename'], 'subject': meta['subject'],
                                  'recipients': eligible, 'previous': {r: delivery.get(r, {}).get('status', 'pending') for r in eligible}})
            if not items:
                raise ValueError('没有可发送地址：已接受或待核对的地址不会重复发送')
            self.plans = {k: p for k, p in self.plans.items() if p['expires'] > time.monotonic()}
            nonce = secrets.token_urlsafe(24)
            plan = {'id': nonce, 'account': account, 'sender': account + '@sjtu.edu.cn',
                    'scope': scope, 'test_to': test_to, 'items': items, 'skipped': skipped,
                    'delay': delay, 'expires': time.monotonic() + 300}
            self.plans[nonce] = plan
            return {**plan, 'confirmation': '确认测试' if test_to else '确认发送 ' + str(len(items))}

    def start(self, plan_id, password, confirmation):
        with self.lock, self.db() as db:
            if self.busy():
                raise ValueError('已有发送任务正在进行，请勿重复提交')
            plan = self.plans.get(plan_id)
            if not plan or plan['expires'] < time.monotonic():
                raise ValueError('确认已过期，请重新预览发送清单')
            expected = '确认测试' if plan['test_to'] else '确认发送 ' + str(len(plan['items']))
            if confirmation != expected:
                raise ValueError('确认文字不匹配')
            if not self.demo and (not isinstance(password, str) or not password):
                raise ValueError('请输入密码；密码只保留在本次任务的内存中')
            for item in plan['items']:
                self._draft(db, item['id'])
                delivery = self._delivery(db, item['id'], plan['scope'])
                if any(delivery.get(r, {}).get('status', 'pending') != item['previous'][r] for r in item['recipients']):
                    raise ValueError('邮件状态已变化，请重新确认发送清单')
                if hashlib.sha256((self.files / (item['id'] + '.eml')).read_bytes()).hexdigest() != item['id']:
                    raise ValueError('草稿文件已变化，请重新导入')
            self.plans.clear()
            self.stop_event.clear()
            self.job = {'id': secrets.token_hex(8), 'status': 'running', 'total': len(plan['items']),
                        'completed': 0, 'sent': 0, 'partial': 0, 'failed': 0, 'uncertain': 0,
                        'current': '', 'scope': plan['scope'], 'sender': plan['sender'], 'events': [], 'started_at': now()}
            self.worker = threading.Thread(target=self._run, args=(copy.deepcopy(plan), password), daemon=False)
            self.worker.start()
            return copy.deepcopy(self.job)

    def _record_begin(self, item, plan, attempt_id, message_id):
        with self.lock, self.db() as db:
            for recipient in item['recipients']:
                db.execute('INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,?)',
                           (item['id'], plan['scope'], recipient, 'attempting', '开始 SMTP 提交', now()))
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (attempt_id, item['id'], item['filename'], item['subject'], plan['scope'], plan['sender'],
                        json.dumps(item['recipients']), '[]', '{}', 'attempting', '', message_id, now(), None))

    def _record_end(self, item, plan, attempt_id, status, accepted, rejected, detail):
        with self.lock, self.db() as db:
            for recipient in item['recipients']:
                result = 'sent' if recipient in accepted else ('uncertain' if status == 'uncertain' else 'failed')
                reason = 'SMTP 服务器已接受；不代表最终送达' if result == 'sent' else detail
                if recipient in rejected:
                    reason = '收件地址被拒绝，SMTP 状态码 ' + str(rejected[recipient])
                db.execute('UPDATE deliveries SET status=?,detail=?,updated_at=? WHERE hash=? AND scope=? AND recipient=?',
                           (result, reason, now(), item['id'], plan['scope'], recipient))
            db.execute('UPDATE attempts SET accepted=?,rejected=?,status=?,detail=?,finished_at=? WHERE id=?',
                       (json.dumps(accepted), json.dumps(rejected), status, detail, now(), attempt_id))

    def _submit(self, plan, item, data):
        smtp, phase = None, 'connect'
        try:
            smtp = self.smtp_factory(SMTP_HOST, SMTP_PORT, timeout=30, context=create_tls_context())
            phase = 'login'
            smtp.login(plan['account'], plan['_password'])
            phase = 'send'
            refused = smtp.sendmail(plan['sender'], item['recipients'], data)
            rejected = {r: int(value[0]) for r, value in refused.items()}
            accepted = [r for r in item['recipients'] if r not in rejected]
            return ('partial' if rejected else 'sent'), accepted, rejected, 'SMTP 服务器已接受' if not rejected else '部分收件地址被拒绝', False
        except smtplib.SMTPAuthenticationError as exc:
            return 'failed', [], {}, '认证失败（%s），请检查账号和密码' % exc.smtp_code, True
        except smtplib.SMTPRecipientsRefused as exc:
            return 'failed', [], {r: int(v[0]) for r, v in exc.recipients.items()}, '全部收件地址被拒绝', False
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            return 'failed', [], {}, '服务器明确拒绝提交（%s）' % exc.smtp_code, False
        except Exception:
            if phase == 'send':
                return 'uncertain', [], {}, '提交时连接异常，是否已接受未知；请人工核对', True
            return 'failed', [], {}, '连接或登录阶段失败，尚未提交邮件；已停止后续发送', True
        finally:
            if smtp is not None:
                # A lost QUIT reply cannot undo the successful DATA acknowledgement.
                try:
                    smtp.quit()
                except Exception:
                    try:
                        smtp.close()
                    except Exception:
                        pass

    def _run(self, plan, password):
        plan['_password'] = password
        final_status = 'error'
        try:
            for index, item in enumerate(plan['items']):
                if self.stop_event.is_set():
                    break
                with self.lock:
                    self.job['current'] = item['filename']
                attempt_id = secrets.token_hex(16)
                message_id = make_msgid(domain='sjtu.edu.cn')
                raw = (self.files / (item['id'] + '.eml')).read_bytes()
                if hashlib.sha256(raw).hexdigest() != item['id']:
                    raise ValueError('草稿内容变化')
                data = prepare_message(raw, plan['sender'], item['recipients'], plan['test_to'], message_id)
                self._record_begin(item, plan, attempt_id, message_id)
                status, accepted, rejected, detail, halt = self._submit(plan, item, data)
                self._record_end(item, plan, attempt_id, status, accepted, rejected, detail)
                with self.lock:
                    self.job['completed'] += 1
                    self.job[status] += 1
                    self.job['events'].append({'filename': item['filename'], 'status': status, 'detail': detail})
                if halt:
                    self.stop_event.set()
                if index + 1 < len(plan['items']) and self.stop_event.wait(plan['delay']):
                    break
            final_status = 'stopped' if self.stop_event.is_set() else 'finished'
        except Exception:
            # If journaling or serialization fails, halt rather than attempt another message.
            with self.lock:
                self.job['events'].append({'filename': self.job['current'], 'status': 'uncertain',
                                           'detail': '本地处理或结果保存失败，已停止。请保留数据并人工核对。'})
        finally:
            plan.pop('_password', None)
            password = None
            try:
                self.write_csv()
            except Exception:
                with self.lock:
                    self.job['events'].append({'filename': '', 'status': 'failed', 'detail': 'CSV 自动保存失败；请在页面导出，SQLite 记录仍保留。'})
            finally:
                with self.lock:
                    self.job['finished_at'] = now()
                    self.job['current'] = ''
                    self.job['status'] = final_status

    def stop(self):
        with self.lock:
            if self.busy():
                self.stop_event.set()
                self.job['status'] = 'stopping'

    def unlock(self, digest, scope, confirmation):
        if confirmation != '已核对，允许重试':
            raise ValueError('请完整输入：已核对，允许重试')
        with self.lock, self.db() as db:
            if self.busy():
                raise ValueError('请等待当前任务结束后再处理')
            self._draft(db, digest)
            db.execute("UPDATE deliveries SET status='failed',detail='用户人工核对后允许重试',updated_at=? WHERE hash=? AND scope=? AND status IN ('uncertain','attempting')", (now(), digest, scope))
            db.execute('INSERT INTO audit VALUES (?,?,?,?)', (now(), digest, scope, '用户确认已核对，允许重试'))
            self.plans.clear()

    def csv_bytes(self):
        out = io.StringIO(newline='')
        writer = csv.writer(out)
        writer.writerow(['文件名', '主题', '模式', '发件人', '本次收件地址', '已接受地址', '被拒绝地址及状态码', '状态', '说明', 'Message-ID', '开始时间（UTC）', '结束时间（UTC）'])
        def safe(value):
            value = str(value or '')
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
        with self.lock, self.db() as db:
            for row in db.execute('SELECT * FROM attempts ORDER BY started_at,rowid'):
                values = [row[k] for k in ('filename', 'subject', 'scope', 'sender', 'recipients', 'accepted', 'rejected', 'status', 'detail', 'message_id', 'started_at', 'finished_at')]
                if self.demo:
                    values[2] = '模拟（未发送）:' + values[2]
                writer.writerow([safe(value) for value in values])
        return ('\ufeff' + out.getvalue()).encode('utf-8')

    def write_csv(self):
        data = self.csv_bytes()
        target = self.root / 'send_results.csv'
        temp = target.with_suffix('.tmp')
        temp.write_bytes(data)
        os.chmod(temp, 0o600)
        os.replace(temp, target)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, app, port=0):
        self.app, self.token = app, secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)


class Handler(BaseHTTPRequestHandler):
    server_version = 'MailDesk/' + APP_VERSION

    def log_message(self, *args):
        pass  # Never log authorization headers, passwords, or imported mail.

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def reply(self, status, data, content_type='application/json; charset=utf-8', filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self' about:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename, safe=''))
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, api=False):
        if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
            self.reply(403, {'error': '拒绝非本机来源'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            self.reply(403, {'error': '请求来源不匹配'})
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.reply(403, {'error': '拒绝跨站请求'})
            return False
        if api and not secrets.compare_digest(self.headers.get('X-MailDesk-Token', ''), self.server.token):
            self.reply(403, {'error': '连接已失效，请使用启动时打开的页面'})
            return False
        return True

    def do_GET(self):
        route = urlsplit(self.path)
        if not self.allowed(route.path.startswith('/api/')):
            return
        try:
            if route.path == '/api/state':
                self.reply(200, self.server.app.state())
            elif route.path == '/api/detail':
                self.reply(200, self.server.app.detail(parse_qs(route.query).get('id', [''])[0]))
            elif route.path == '/api/attachment':
                args = parse_qs(route.query)
                filename, payload = self.server.app.attachment(args.get('id', [''])[0], int(args.get('index', ['-1'])[0]))
                self.reply(200, payload, 'application/octet-stream', filename)
            elif route.path == '/api/export':
                self.reply(200, self.server.app.csv_bytes(), 'text/csv; charset=utf-8', '演示发送结果.csv' if self.server.app.demo else '发送结果.csv')
            else:
                files = {'/': ('index.html', 'text/html; charset=utf-8'),
                         '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                         '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if route.path not in files:
                    self.reply(404, {'error': '页面不存在'})
                    return
                name, kind = files[route.path]
                self.reply(200, (BASE / 'static' / name).read_bytes(), kind)
        except (ValueError, KeyError, TypeError):
            self.reply(400, {'error': '请求无效或内容不存在'})
        except Exception:
            self.reply(500, {'error': '本地读取失败，请检查数据目录'})

    def do_POST(self):
        if not self.allowed(True):
            return
        route = urlsplit(self.path)
        try:
            size = int(self.headers.get('Content-Length', '-1'))
            cap = MAX_FILE if route.path == '/api/import' else 1024 * 1024
            if not 0 <= size <= cap:
                self.reply(413, {'error': '请求超过大小限制（.eml 单文件 32 MB）'})
                return
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('上传不完整，请重新导入')
            app = self.server.app
            if route.path == '/api/import':
                result = app.import_file(parse_qs(route.query).get('name', [''])[0], raw)
            else:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('请求格式有误')
                if route.path == '/api/plan':
                    result = app.make_plan(data.get('ids'), data.get('account'), data.get('test_to'), data.get('delay', 2))
                elif route.path == '/api/start':
                    result = app.start(data.get('plan_id'), data.get('password'), data.get('confirmation'))
                elif route.path == '/api/stop':
                    app.stop()
                    result = {'ok': True}
                elif route.path == '/api/remove':
                    if not isinstance(data.get('ids'), list) or any(not isinstance(x, str) for x in data['ids']):
                        raise ValueError('请选择草稿')
                    app.remove(data['ids'])
                    result = {'ok': True}
                elif route.path == '/api/unlock':
                    app.unlock(data.get('id'), data.get('scope', 'real'), data.get('confirmation'))
                    result = {'ok': True}
                else:
                    self.reply(404, {'error': '接口不存在'})
                    return
            self.reply(200, result)
        except (ValueError, TypeError, KeyError) as exc:
            self.reply(400, {'error': str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else '请求格式有误'})
        except Exception:
            self.reply(500, {'error': '本地处理失败，已停止本次操作；请检查数据目录'})


def main():
    parser = argparse.ArgumentParser(description='交邮 · 本机批量邮件工作台')
    parser.add_argument('--self-test', action='store_true', help='离线自检，不连接 SMTP、不打开浏览器')
    parser.add_argument('--demo', action='store_true', help='演示模式，绝不连接 SMTP')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.self_test:
        from macos.selftest import run
        run()
        return
    root = args.data_dir or default_data_root(args.demo)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lockfile = open(root / '.instance.lock', 'a+')
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('此工作目录已有一个窗口在运行。请使用已打开的浏览器页面，或先关闭原启动终端。')
        return
    app = MailDesk(root, demo=args.demo)
    if args.demo and not (root / '.seeded').exists():
        from samples import sample_messages
        for filename, raw in sample_messages():
            app.import_file(filename, raw)
        (root / '.seeded').touch()
    server = Server(app, args.port)
    url = server.origin + '/#token=' + server.token
    print('\n交邮 · ' + ('演示模式（不会发送任何邮件）' if args.demo else '本机邮件工作台'), flush=True)
    print('请保持此窗口打开。关闭前先在网页中停止后续发送。', flush=True)
    print('页面地址：' + url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    def shutdown(signum, frame):
        app.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        app.stop()
        if app.worker and app.worker.is_alive():
            print('正在等待当前提交结束；不会启动下一封。', flush=True)
            app.worker.join()
        server.server_close()
        lockfile.close()


if __name__ == '__main__':
    main()
