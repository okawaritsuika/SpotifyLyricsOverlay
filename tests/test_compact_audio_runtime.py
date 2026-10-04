from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from compact_audio_runtime import compact, BUILD_ARCHIVES


class CompactRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.packages=self.base/'.venv/Lib/site-packages';self.lib=self.packages/'torch/lib'
        self.lib.mkdir(parents=True)
        for name in BUILD_ARCHIVES:(self.lib/name).write_bytes(b'!<arch>\nsynthetic archive')
        for name in ('torch_cpu.dll','torch_cpu.lib','c10.lib','model.bin'):(self.lib/name).write_bytes(b'keep')
        self.location=patch('compact_audio_runtime.sysconfig.get_path',return_value=str(self.packages));self.location.start()
        self.check=patch('compact_audio_runtime.check_runtime');self.probe=self.check.start()
    def tearDown(self):self.check.stop();self.location.stop();self.temp.cleanup()

    def test_only_five_build_archives_removed_and_second_run_is_safe(self):
        size=sum((self.lib/name).stat().st_size for name in BUILD_ARCHIVES)
        self.assertEqual(compact(self.base),{'files':5,'removed_bytes':size})
        self.assertEqual({p.name for p in self.lib.iterdir()},{'torch_cpu.dll','torch_cpu.lib','c10.lib','model.bin'})
        self.assertEqual(compact(self.base),{'files':0,'removed_bytes':0})
        self.probe.assert_called_with(self.packages.resolve())

    def test_external_python_is_rejected_before_probe_or_deletion(self):
        with patch('compact_audio_runtime.sysconfig.get_path',return_value=str(self.base.parent)):
            with self.assertRaises(ValueError):compact(self.base)
        self.probe.assert_not_called()
        self.assertTrue(all((self.lib/name).exists() for name in BUILD_ARCHIVES))

    def test_unknown_format_or_failed_runtime_keeps_all_files(self):
        path=self.lib/BUILD_ARCHIVES[-1];path.write_bytes(b'unexpected')
        with self.assertRaises(ValueError):compact(self.base)
        self.assertTrue(all((self.lib/name).exists() for name in BUILD_ARCHIVES));self.probe.assert_not_called()
        path.write_bytes(b'!<arch>\nvalid');self.probe.side_effect=ValueError('unsupported runtime')
        with self.assertRaises(ValueError):compact(self.base)
        self.assertTrue(all((self.lib/name).exists() for name in BUILD_ARCHIVES))

    def test_installer_calls_compaction_and_disables_pip_cache(self):
        root=Path(__file__).resolve().parents[1]
        script=(root/'setup-audio.ps1').read_text(encoding='utf-8-sig')
        self.assertEqual(script.count('--no-cache-dir --no-compile'),2)
        self.assertLess(script.index('--install-model'),script.index('compact_audio_runtime.py'))
        self.assertLess(script.index('compact_audio_runtime.py'),script.index("Write-Output '음원 분석 준비 완료"))
        from package_release import MODULES
        self.assertIn('compact_audio_runtime',MODULES)


if __name__=='__main__':unittest.main()
