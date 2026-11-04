"""FIX-021 settings integrity; disposable /data only."""
import json
import unittest
from unittest.mock import patch
import test_download_lifecycle as harness
main = harness.main

class SettingsTest(unittest.TestCase):
    setUpClass = classmethod(harness.DownloadLifecycleTest.setUpClass.__func__)
    tearDownClass = classmethod(harness.DownloadLifecycleTest.tearDownClass.__func__)
    def setUp(self):
        self.tearDown()
    def tearDown(self):
        for path in main.CONFIG_DIR.glob('settings*'):
            path.unlink(missing_ok=True)
    def get(self):
        status, raw = harness.request('/settings')
        return status, json.loads(raw)
    def put(self, data, revision=None):
        import urllib.request, urllib.error
        req=urllib.request.Request(harness.BASE+'/settings',method='PUT',data=json.dumps(data).encode(),headers={'Content-Type':'application/json',**({'If-Match':revision} if revision else {})})
        try:
            with urllib.request.urlopen(req) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            raw=error.read()
            try: payload=json.loads(raw)
            except ValueError: payload={'detail':raw.decode()}
            return error.code,payload
    def test_stale_tab_cannot_delete_new_preset(self):
        _, initial=self.get()
        preset=json.dumps({'Photo':{'setting-quality':'80'}})
        status,saved=self.put({'user_presets':preset},initial['revision'])
        self.assertEqual(status,200)
        self.assertEqual(self.put({'setting-quality':'90','user_presets':'{}'},initial['revision'])[0],409)
        self.assertEqual(self.get()[1]['settings']['user_presets'],preset)
        self.assertEqual(self.put({'user_presets':preset,'setting-quality':'90'},saved['revision'])[0],200)
    def test_legacy_migration_backup_and_restart_read(self):
        old=b'{"setting-quality":"70"}'
        main.SETTINGS_FILE.write_bytes(old)
        _,data=self.get();self.assertEqual(data['settings']['setting-quality'],'70')
        self.assertEqual(self.put({'setting-quality':'80'},data['revision'])[0],200)
        self.assertEqual(main.SETTINGS_FILE.with_name('settings.backup.json').read_bytes(),old)
        self.assertEqual(main._read_settings_document()[0]['setting-quality'],'80')
        self.assertEqual(json.loads(main.SETTINGS_FILE.read_text())['schema_version'],1)
    def test_corrupt_and_empty_files_are_preserved_and_block_writes(self):
        for raw in (b'{broken',b'',b'{"schema_version":2,"settings":{}}'):
            main.SETTINGS_FILE.write_bytes(raw)
            self.assertEqual(self.get()[0],503)
            self.assertEqual(self.put({'setting-quality':'90'},'old')[0],503)
            self.assertEqual(main.SETTINGS_FILE.read_bytes(),raw)
    def test_missing_revision_invalid_presets_and_size_limits(self):
        _,data=self.get();revision=data['revision']
        self.assertEqual(self.put({'setting-quality':'80'})[0],428)
        for presets in ({'bad':{'unknown':1}},{'bad':{'setting-flip':'false'}},[],{'bad':[]}):
            self.assertEqual(self.put({'user_presets':json.dumps(presets)},revision)[0],400)
        self.assertEqual(self.put({'large':'x'*9000},revision)[0],400)
        self.assertEqual(self.put({str(i):'x'*8000 for i in range(10)},revision)[0],400)
        self.assertFalse(main.SETTINGS_FILE.exists())
    def test_backup_preserved_after_later_corruption(self):
        _,data=self.get();self.put({'setting-quality':'70'},data['revision'])
        _,data=self.get();self.put({'setting-quality':'80'},data['revision'])
        backup=main.SETTINGS_FILE.with_name('settings.backup.json').read_bytes()
        main.SETTINGS_FILE.write_text('broken')
        self.assertEqual(self.get()[0],503)
        self.assertEqual(main.SETTINGS_FILE.with_name('settings.backup.json').read_bytes(),backup)

    def test_failed_atomic_write_does_not_claim_success_or_replace_current_settings(self):
        _, data = self.get()
        self.assertEqual(self.put({'setting-quality':'70'},data['revision'])[0],200)
        _, data = self.get()
        original = main.SETTINGS_FILE.read_bytes()
        with patch.object(main.Path, 'replace', side_effect=OSError('simulated disk failure')):
            self.assertEqual(self.put({'setting-quality':'80'},data['revision'])[0],500)
        self.assertEqual(main.SETTINGS_FILE.read_bytes(),original)
