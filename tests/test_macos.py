import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from app import BASE, default_data_root, create_tls_context


class MacPackagingTests(unittest.TestCase):
    def test_source_keeps_existing_data_directory(self):
        with patch.object(sys, 'frozen', False, create=True):
            self.assertEqual(default_data_root(), BASE/'data')
            self.assertEqual(default_data_root(True), BASE/'demo_data')

    def test_bundle_data_is_outside_application(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(Path, 'home', return_value=Path('/Users/test-person')):
            self.assertEqual(default_data_root(), Path('/Users/test-person/Library/Application Support/SJTUMailDesk/data'))
            self.assertEqual(default_data_root(True).name, 'demo_data')

    def test_bundle_uses_explicit_certificate_bundle(self):
        with patch.object(sys, 'frozen', True, create=True), patch('app.ssl.create_default_context') as factory:
            create_tls_context()
            factory.assert_called_once_with(cafile=str(BASE/'certs'/'cacert.pem'))

    def test_source_keeps_system_tls_verification(self):
        with patch.object(sys, 'frozen', False, create=True), patch('app.ssl.create_default_context') as factory:
            create_tls_context()
            factory.assert_called_once_with()
