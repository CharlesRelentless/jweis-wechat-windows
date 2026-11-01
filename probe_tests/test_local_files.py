import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from probe.local_files import LocalReadOnly,LocalFileError
from probe.platform_io import private_dir,open_private
from probe.probe import Probe


class CatalogTests(unittest.TestCase):
    def test_policy_root_cannot_be_relative_to_a_process_working_directory(self):
        with self.assertRaisesRegex(LocalFileError,'absolute_local_root_required'):
            LocalReadOnly({'vault':'relative-notes'})

    def test_new_capability_is_absent_and_unusable_by_default(self):
        with tempfile.TemporaryDirectory() as folder:
            probe=Probe(private_dir(Path(folder)/'state'))
            try:
                self.assertEqual(len(probe.tools()),15)
                reply=probe.rpc({'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':'read_allowed_local_file','arguments':{'root':'vault','path':'note.md'}}})
                self.assertIn('error',reply)
            finally:probe.close()

    def test_policy_requires_private_owner_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'policy.json';p.write_text('{}')
            with mock.patch('probe.local_files.is_private',return_value=False):
                with self.assertRaisesRegex(LocalFileError,'private_local_read_policy_required'):
                    LocalReadOnly.from_policy(p)


@unittest.skipUnless(sys.platform=='win32','Verified Windows opened-handle boundary')
class WindowsLocalReadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.vault=self.base/'vault';self.vault.mkdir()
        self.file=self.vault/'note.md';self.file.write_text('中文 note',encoding='utf-8')
        self.reader=LocalReadOnly({'vault':self.vault})

    def test_metadata_hash_and_bounded_utf8_text_without_changing_acl(self):
        import ctypes
        from probe.platform_io import win_files
        def sddl():
            win=win_files()
            with win.handle(self.file,0x00020000) as handle:
                acl,descriptor=ctypes.c_void_p(),ctypes.c_void_p()
                code=win.get_security(handle,1,4,None,None,ctypes.byref(acl),None,ctypes.byref(descriptor))
                if code:raise ctypes.WinError(code)
                value=win.w.LPWSTR()
                try:
                    win.check(win.to_sddl(descriptor,1,4,ctypes.byref(value),None))
                    return value.value
                finally:
                    if value:win.free(value)
                    win.free(descriptor)
        before=sddl()
        data=self.file.read_bytes();r=self.reader.read('vault','note.md')
        self.assertEqual((r['bytes'],r['sha256']),(len(data),hashlib.sha256(data).hexdigest()))
        self.assertNotIn('text',r)
        text=self.reader.read('vault','note.md',include_text=True,max_bytes=4)
        self.assertTrue(text['truncated']);self.assertEqual(text['text'],'中')
        after=sddl()
        self.assertEqual(before,after);self.assertEqual(self.file.read_bytes(),data)

    def test_absolute_traversal_ads_and_wrong_roots_are_denied(self):
        for path in ('../note.md',r'..\note.md',r'C:\other\note.md',r'\\server\share\note.md','note.md:stream'):
            with self.subTest(path=path),self.assertRaises((LocalFileError,OSError)):
                self.reader.read('vault',path)
        with self.assertRaisesRegex(LocalFileError,'root_not_authorized'):self.reader.read('other','note.md')

    def test_hidden_system_credentials_and_binary_types_are_denied(self):
        for path in ('.git/notes.md','.obsidian/notes.md','secret.md','账号.md','notes/api-key.md','program.exe'):
            with self.subTest(path=path),self.assertRaises((LocalFileError,OSError)):
                self.reader.read('vault',path)
        import ctypes
        self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(str(self.file),2))
        with self.assertRaisesRegex(LocalFileError,'hidden_or_system_path'):self.reader.read('vault','note.md')
        ctypes.windll.kernel32.SetFileAttributesW(str(self.file),128)

    def test_reparse_parent_and_root_are_denied(self):
        outside=self.base/'outside';outside.mkdir();(outside/'note.md').write_text('outside')
        link=self.vault/'link'
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
            "New-Item -ItemType Junction -Path '"+str(link)+"' -Target '"+str(outside)+"' | Out-Null"],check=True,capture_output=True)
        try:
            with self.assertRaises(OSError):self.reader.read('vault','link/note.md')
            with self.assertRaises(OSError):LocalReadOnly({'other':link})
        finally:os.rmdir(link)

    def test_limits_and_known_credential_content_fail_closed(self):
        self.file.write_text('sk-proj-'+'A'*40)
        with self.assertRaisesRegex(LocalFileError,'credential_content_not_allowed'):self.reader.read('vault','note.md',include_text=True)
        self.file.write_bytes(b'x'*(1024*1024+1))
        with self.assertRaisesRegex(LocalFileError,'too_large'):self.reader.read('vault','note.md')
        for limit in (0,65537,True,'4'):
            with self.subTest(limit=limit),self.assertRaisesRegex(LocalFileError,'invalid_local_read_arguments'):
                self.reader.read('vault','note.md',max_bytes=limit)

    def test_owner_policy_and_real_rpc_return_data_without_sending_messages(self):
        policy=self.base/'policy.json'
        with os.fdopen(open_private(policy,os.O_CREAT|os.O_EXCL|os.O_WRONLY),'w',encoding='utf-8') as out:
            json.dump({'version':1,'enabled':True,'roots':{'vault':str(self.vault)}},out)
        reader=LocalReadOnly.from_policy(policy)
        probe=Probe(private_dir(self.base/'state'),local_files=reader)
        try:
            catalog=probe.rpc({'jsonrpc':'2.0','id':1,'method':'tools/list'})['result']['tools']
            self.assertEqual(len(catalog),16)
            result=probe.rpc({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
                'name':'read_allowed_local_file','arguments':{'root':'vault','path':'note.md','include_text':True}}})['result']['structuredContent']
            self.assertEqual(result['text'],'中文 note');self.assertTrue(result['source_is_untrusted_data'])
            self.assertEqual(probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0],0)
            self.assertEqual(probe.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0],0)
            rejected=probe.rpc({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
                'name':'read_allowed_local_file','arguments':{'root':'vault','path':'absent.md'}}})
            self.assertNotIn(str(self.vault),json.dumps(rejected))
        finally:probe.close()

    def test_explicit_revocation_disables_the_capability(self):
        policy=self.base/'disabled.json'
        with os.fdopen(open_private(policy,os.O_CREAT|os.O_EXCL|os.O_WRONLY),'w',encoding='utf-8') as out:
            json.dump({'version':1,'enabled':False,'roots':{}},out)
        self.assertIsNone(LocalReadOnly.from_policy(policy))


if __name__=='__main__':unittest.main()
