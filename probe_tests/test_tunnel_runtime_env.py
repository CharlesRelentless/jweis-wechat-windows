import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from probe.platform_io import open_private, private_dir
from probe.tunnel_runtime_env import official_environment, activation_policy


class ConfirmedRuntimeEnvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = private_dir(Path(self.temp.name) / 'installation')
        self.runtime = private_dir(self.root / 'runtime')
        private_dir(self.runtime / 'secrets')
        self.profile = self.runtime / 'profile.yaml'
        self.tunnel = 'tunnel_' + '1' * 32
        self.write(self.profile, 'control_plane:\n  tunnel_id: ' + self.tunnel
                   + '\n  api_key: "env:JWEIS_TUNNEL_API_KEY"\n')

    def write(self, target, value):
        with os.fdopen(open_private(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
                       'w', encoding='utf-8') as output:
            output.write(value)

    def policy(self, free=True, authorized=True, tunnel=None):
        self.write(self.runtime / 'official-tunnel-activation.json', json.dumps({
            'tunnel_id': tunnel or self.tunnel,
            'cost_status': 'confirmed_free' if free else 'unverified',
            'online_start_authorized': authorized}))

    def test_unconfirmed_cost_blocks_before_reading_any_credential(self):
        self.policy(free=False)
        self.assertFalse((self.runtime / 'secrets/openai-tunnel.env').exists())
        with self.assertRaisesRegex(ValueError, 'free transport'):
            official_environment(self.root, self.profile)

    def test_policy_must_authorize_the_exact_tunnel(self):
        self.policy(tunnel='tunnel_' + '2' * 32)
        with self.assertRaisesRegex(ValueError, 'free transport'):
            official_environment(self.root, self.profile)

    def test_only_confirmed_key_is_loaded_for_the_child_without_rewriting_it(self):
        self.policy()
        synthetic = 'sk-offline-synthetic-not-a-real-credential'
        target = self.runtime / 'secrets/openai-tunnel.env'
        self.write(target, 'OPENAI_API_KEY=' + synthetic + '\n')
        original = target.read_bytes()
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'unrelated-parent-value'}):
            env = official_environment(self.root, self.profile)
            self.assertEqual(env['JWEIS_TUNNEL_API_KEY'], synthetic)
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertEqual(os.environ['OPENAI_API_KEY'], 'unrelated-parent-value')
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse((target.parent / 'runtime.key').exists())

    def test_extra_env_assignments_are_rejected_without_echoing_content(self):
        self.policy()
        self.write(self.runtime / 'secrets/openai-tunnel.env',
                   'OPENAI_API_KEY=sk-offline-synthetic-credential\nEXTRA=unwanted\n')
        with self.assertRaisesRegex(ValueError, '^The runtime env file has an unsupported format.$'):
            official_environment(self.root, self.profile)

    def test_legacy_raw_key_profiles_keep_the_existing_branch(self):
        self.write(self.profile, 'control_plane:\n  api_key: file:/owner/runtime.key\n')
        self.assertIsNone(official_environment(self.root, self.profile))

    def test_owner_continuation_is_distinct_from_a_free_claim_and_uses_child_proxy(self):
        policy={'tunnel_id':self.tunnel,'cost_status':'no_charge_observed',
                'continue_until_charge':True,'online_start_authorized':True,
                'proxy_url':'http://127.0.0.1:7890'}
        self.write(self.runtime/'official-tunnel-activation.json',json.dumps(policy))
        self.write(self.runtime/'secrets/openai-tunnel.env','OPENAI_API_KEY=sk-offline-synthetic-credential\n')
        self.assertEqual(official_environment(self.root,self.profile)['HTTPS_PROXY'],policy['proxy_url'])
        self.assertEqual(activation_policy(self.root,self.profile)['cost_status'],'no_charge_observed')
        policy['charge_detected']=True
        self.write(self.runtime/'official-tunnel-activation.json',json.dumps(policy))
        with self.assertRaises(ValueError): official_environment(self.root,self.profile)

    def test_observed_zero_cost_without_explicit_continuation_stays_blocked(self):
        self.write(self.runtime/'official-tunnel-activation.json',json.dumps({
            'tunnel_id':self.tunnel,'cost_status':'no_charge_observed','online_start_authorized':True}))
        with self.assertRaises(ValueError): activation_policy(self.root,self.profile)
