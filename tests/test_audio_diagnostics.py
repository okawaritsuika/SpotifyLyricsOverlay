import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio_diagnostics import record_failure
from audio_progress import AudioProgress


class AudioDiagnosticsTests(unittest.TestCase):
    def test_failure_history_survives_restart_is_bounded_and_omits_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);progress=AudioProgress('Example')
            progress.update({'kind':'progress','state':'recording','completed':100,'total':100})
            event={'kind':'error','text':'private exception contents','audio':'private audio',
                   'diagnostic':{'stage':'analyze','error_type':'RuntimeError','secret':'private path',
                                 'frames':[{'module':'audio_alignment','function':'align_lyrics','line':85,
                                            'locals':'private lyrics'}]}}
            for i in range(22):record_failure(path,str(i),progress,event)
            saved=json.loads((path/'audio_failures.json').read_text(encoding='utf-8'))
            self.assertEqual(len(saved),20)
            self.assertEqual(saved[0]['title'],'21')
            self.assertEqual(saved[0]['stage'],'analyze')
            self.assertEqual(saved[0]['percent'],100)
            self.assertEqual(saved[0]['error_type'],'RuntimeError')
            self.assertNotIn('private',json.dumps(saved))
            self.assertEqual(saved[0]['frames'],[{'module':'audio_alignment','function':'align_lyrics','line':85}])

    def test_reporting_survives_storage_failure(self):
        with patch('audio_diagnostics.atomic_json',side_effect=OSError('disk full')):
            record_failure(Path('unused'),'',AudioProgress(''),{'kind':'error'})


if __name__=='__main__':unittest.main()
