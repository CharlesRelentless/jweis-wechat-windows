import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from probe.platform_io import WINDOWS, is_private

ROOT=Path(__file__).resolve().parents[1]

def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/name)
    result=importlib.util.module_from_spec(spec); spec.loader.exec_module(result); return result


class SetupTests(unittest.TestCase):
    @unittest.skipUnless(WINDOWS, 'Windows Unicode and command path rendering')
    def test_windows_unicode_space_and_shell_characters_are_literal_paths(self):
        root=self.root/"测试 空格 & $ '目录"
        runtime=module('render-config.py').render(root,'tunnel_'+'1'*32,[],discover_callback=True)
        lines=(runtime/'profile.yaml').read_text(encoding='utf-8').splitlines()
        command=json.loads(next(line.split('command: ',1)[1] for line in lines if 'command: ' in line))
        self.assertIn('"'+(root/'.venv/Scripts/python.exe').as_posix()+'"',command)
        self.assertIn('"'+(runtime/'state').as_posix()+'"',command)
        self.assertNotIn('--allow-callback-host',command)
        self.assertTrue(is_private(runtime/'profile.yaml'))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); shutil.copytree(ROOT/'config',self.root/'config')
        self.config=module('render-config.py')

    def test_profile_only_references_private_key_service_resource_limits(self):
        runtime=self.config.render(self.root,'tunnel_'+'0'*32,['callbacks.example.com'])
        profile=(runtime/'profile.yaml').read_text(encoding='utf-8'); service=(runtime/'dots-wechat-bridge.service').read_text(encoding='utf-8') if not WINDOWS else ''
        self.assertIn('file:',profile); self.assertNotIn('sk-',profile)
        self.assertIn('--allow-callback-host callbacks.example.com',profile)
        self.assertIn('127.0.0.1:0',profile)
        if not WINDOWS:
            for value in ('CPUQuota=100%','MemoryMax=384M','User=dotsbridge','TasksMax=64'):
                self.assertIn(value,service)
        else: self.assertFalse((runtime/'dots-wechat-bridge.service').exists())
        self.assertTrue(is_private(runtime/'profile.yaml'))
        self.assertTrue(is_private(runtime/'state'))

    def test_replace_config_preserves_secret_and_requires_explicit_replace(self):
        runtime=self.config.render(self.root,'tunnel_'+'0'*32,['callbacks.example.com'])
        key=runtime/'secrets/runtime.key'; key.write_text('synthetic-key-fixture')
        with self.assertRaises(FileExistsError): self.config.render(self.root,'tunnel_'+'0'*32,['callbacks.example.com'])
        self.config.render(self.root,'tunnel_'+'0'*32,['second.example.com'],True)
        self.assertEqual(key.read_text(),'synthetic-key-fixture')

    def test_invalid_ids_hosts_and_path_metacharacters_rejected(self):
        for tid,hosts in (('invalid',['callbacks.example.com']),('tunnel_'+'0'*32,['*']),('tunnel_'+'0'*32,[])):
            with self.assertRaises(ValueError): self.config.render(self.root,tid,hosts)
        if not WINDOWS:
            with self.assertRaises(ValueError): self.config.render(str(self.root)+'/bad space','tunnel_'+'0'*32,['callbacks.example.com'])

    def test_discovery_cli_denies_callbacks_then_exact_host_replace_preserves_key(self):
        command=[sys.executable,str(ROOT/'scripts/render-config.py'),'--root',str(self.root),
                 '--tunnel-id','tunnel_'+'0'*32]
        missing=subprocess.run(command,capture_output=True,text=True)
        self.assertNotEqual(missing.returncode,0)
        discover=subprocess.run(command+['--discover-callback'],capture_output=True,text=True)
        self.assertEqual(discover.returncode,0,discover.stderr)
        runtime=self.root/'runtime'
        self.assertNotIn('--allow-callback-host',(runtime/'profile.yaml').read_text())
        key=runtime/'secrets/runtime.key'; key.write_text('synthetic-key-fixture')
        replace=subprocess.run(command+['--callback-host','callbacks.example.com','--replace'],capture_output=True,text=True)
        self.assertEqual(replace.returncode,0,replace.stderr)
        self.assertIn('--allow-callback-host callbacks.example.com',(runtime/'profile.yaml').read_text())
        self.assertEqual(key.read_text(),'synthetic-key-fixture')
        self.assertTrue(is_private(runtime/'profile.yaml'))
        with self.assertRaises(ValueError):
            self.config.render(self.root,'tunnel_'+'0'*32,['callbacks.example.com'],True,True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux systemd installer only; Windows has separate launcher tests")
    def test_installer_rejects_foreground_worker_before_mutation_then_allows_handoff(self):
        import fcntl
        self.config.render(self.root,'tunnel_'+'0'*32,[],discover_callback=True)
        (self.root/'runtime/secrets/runtime.key').write_text('synthetic-key-fixture')
        scripts=self.root/'scripts'; scripts.mkdir()
        shutil.copy2(ROOT/'scripts/install-service.sh',scripts/'install-service.sh')
        (self.root/'.venv/bin').mkdir(parents=True)
        (self.root/'.venv/bin/python').symlink_to(sys.executable)
        (self.root/'bin').mkdir()
        client=self.root/'bin/tunnel-client'; client.write_text('#!/bin/sh\nexit 0\n'); client.chmod(0o755)
        commands=self.root/'mock-bin'; commands.mkdir()
        # No root access or system changes: privileged commands are inert fixtures.
        for name,body in {
            'id':'if [ "$1" = -u ]; then echo 0; fi\nexit 0',
            'systemctl':'if [ "$1" = cat ]; then exit 1; fi\nprintf "systemctl %s\\n" "$*" >> "$SETUP_TEST_LOG"',
            **{name:'printf "'+name+' %s\\n" "$*" >> "$SETUP_TEST_LOG"' for name in ('chown','chmod','install','useradd')}
        }.items():
            target=commands/name; target.write_text('#!/bin/sh\n'+body+'\n'); target.chmod(0o755)
        log=self.root/'commands.log'
        env=dict(os.environ,PATH=str(commands)+os.pathsep+os.environ.get('PATH',''),SETUP_TEST_LOG=str(log))
        lock=os.open(self.root/'runtime/state/worker.lock',os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            rejected=subprocess.run(['sh',str(scripts/'install-service.sh')],env=env,capture_output=True,text=True,timeout=15)
            self.assertNotEqual(rejected.returncode,0)
            self.assertIn('Stop the foreground tunnel',rejected.stderr)
            self.assertFalse(log.exists(),'Installer mutated system before rejecting worker')
        finally: os.close(lock)
        accepted=subprocess.run(['sh',str(scripts/'install-service.sh')],env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(accepted.returncode,0,accepted.stderr)
        self.assertIn('systemctl enable --now dots-wechat-bridge.service',log.read_text())

    def test_private_synthetic_qr_render_no_network_or_payload_stdout(self):
        state=self.root/'state'; state.mkdir()
        (state/'weixin-qr-content.txt').write_text('https://example.com/synthetic-qr-test')
        target=module('render-qr.py').render(state)
        self.assertTrue(is_private(target))
        self.assertEqual(target.read_bytes()[:8],b'\x89PNG\r\n\x1a\n')

if __name__=='__main__': unittest.main()
