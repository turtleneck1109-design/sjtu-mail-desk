"""Start the actual macOS shell entries with browser opening explicitly disabled."""
import os
import queue
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(Path('/bin/zsh').exists(), 'macOS launcher checks require zsh')
class LauncherTests(unittest.TestCase):
    def launch(self, name):
        temp = tempfile.TemporaryDirectory(prefix='交邮 启动测试 ')
        self.addCleanup(temp.cleanup)
        proc = subprocess.Popen(['/bin/zsh', str(ROOT/name), '--no-browser', '--data-dir', temp.name],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        def cleanup():
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.communicate('\n', timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
        self.addCleanup(cleanup)
        lines = queue.Queue()
        def reader():
            for line in proc.stdout:
                lines.put(line)
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        captured = []
        for _ in range(20):
            try:
                line = lines.get(timeout=8)
            except queue.Empty:
                self.fail('Launcher did not start: '+''.join(captured))
            captured.append(line)
            if line.startswith('页面地址：http://127.0.0.1:'):
                return temp.name, proc, ''.join(captured)
        self.fail('No localhost service URL')

    def test_demo_launcher_starts_with_spaces_and_chinese_path(self):
        root, proc, output = self.launch('演示启动.command')
        self.assertIn('演示模式（不会发送任何邮件）', output)
        self.assertTrue((Path(root)/'.seeded').exists())
        self.assertEqual(len(list((Path(root)/'drafts').glob('*.eml'))), 4)
        self.assertTrue(os.access(ROOT/'演示启动.command',os.X_OK))

    def test_normal_launcher_starts_empty_without_sending(self):
        root, proc, output = self.launch('启动交邮.command')
        self.assertIn('本机邮件工作台',output)
        self.assertFalse((Path(root)/'.seeded').exists())
        self.assertEqual(list((Path(root)/'drafts').glob('*.eml')), [])
        self.assertTrue(os.access(ROOT/'启动交邮.command',os.X_OK))

    def test_duplicate_instance_is_rejected(self):
        root, proc, output = self.launch('演示启动.command')
        second = subprocess.run(['/bin/zsh',str(ROOT/'演示启动.command'),'--no-browser','--data-dir',root],
                                input='\n',capture_output=True,text=True,timeout=8)
        self.assertIn('已有一个窗口在运行',second.stdout)
        self.assertIsNone(proc.poll())


if __name__ == '__main__':
    unittest.main(verbosity=2)
