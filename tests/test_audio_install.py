import io
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch, MagicMock
from audio_install import AudioInstaller, worker
from app import LyricsApp
from storage import Store


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        (self.base/'setup-audio.ps1').write_text('test')
        self.installer=AudioInstaller(self.base)
    def tearDown(self):self.temp.cleanup()

    def test_no_install_on_construction_and_completed_install_rechecks_readiness(self):
        with patch('audio_install.subprocess.Popen') as factory,patch('audio_install.ready') as ready:
            self.assertEqual(self.installer.snapshot(),(False,'설치 전',()))
            factory.assert_not_called()
            process=factory.return_value;process.stdout=io.StringIO('1/3 downloading\n3/3 model\n');process.wait.return_value=0
            self.installer.start();self.installer.thread.join(3)
            self.assertFalse(self.installer.running)
            self.assertIn('설치 완료',self.installer.status);ready.assert_called_once_with(self.base)
            command=factory.call_args.args[0]
            self.assertIn('--worker',command);self.assertIn(str(self.base),command)
            self.assertEqual(self.installer.snapshot()[2],('1/3 downloading','3/3 model'))

    def test_failure_can_retry_and_logs_are_bounded(self):
        with patch('audio_install.subprocess.Popen') as factory,patch('audio_install.ready') as ready:
            process=factory.return_value;process.stdout=io.StringIO('failure\n'*300);process.wait.return_value=1
            self.installer.start();self.installer.thread.join(3)
            self.assertIn('실패',self.installer.status);self.assertEqual(len(self.installer.lines),160);ready.assert_not_called()
            process.stdout=io.StringIO('done\n');process.wait.return_value=0
            self.installer.start();self.installer.thread.join(3)
            self.assertIn('완료',self.installer.status)

    def test_running_install_is_not_duplicated_and_cancel_targets_only_owned_tree(self):
        self.installer.running=True;self.installer.process=MagicMock(pid=43210)
        self.installer.process.poll.return_value=None
        with patch('audio_install.subprocess.Popen') as launch,patch('audio_install.subprocess.run') as stop:
            self.installer.start();launch.assert_not_called()
            self.installer.cancel()
            self.assertEqual(stop.call_args.args[0],['taskkill','/PID','43210','/T','/F'])
            self.assertTrue(self.installer.cancelled.is_set())

    def test_worker_limits_apply_before_optional_script_with_interpreter_argument(self):
        with patch('analysis_limits.apply_analysis_limits') as limits,patch('audio_install.subprocess.call',return_value=0) as call:
            self.assertEqual(worker(self.base,'C:/test path/python.exe'),0)
            limits.assert_called_once_with(cpu_percent=2,memory_mb=1024)
            self.assertEqual(call.call_args.args[0][-4:],['-File',str(self.base/'setup-audio.ps1'),'-Python','C:/test path/python.exe'])

    def test_medium_download_requests_the_validated_revision(self):
        from audio_worker import install_model
        with patch('huggingface_hub.snapshot_download') as download:
            install_model(self.base/'whisper-medium','medium')
        self.assertEqual(download.call_args.args,('Systran/faster-whisper-medium',))
        self.assertEqual(download.call_args.kwargs['revision'],'7832330bcea9a8d5fd6d6637c49fe5d256e98277')
        self.assertEqual(download.call_args.kwargs['local_dir'],str(self.base/'whisper-medium'))

    @unittest.skipUnless(os.name=='nt','Windows installer protocol')
    def test_actual_powershell_worker_passes_unicode_and_spaced_arguments_without_install(self):
        script="param([string]$Python)\n[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding\nWrite-Output '1/3 설치 통신 확인'\nWrite-Output $Python\n"
        (self.base/'setup-audio.ps1').write_text(script,encoding='utf-8-sig')
        import audio_install
        result=subprocess.run([sys.executable,'-X','utf8',audio_install.__file__,'--worker',str(self.base),'C:/test path/python.exe'],
                              capture_output=True,encoding='utf-8',timeout=40,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('1/3 설치 통신 확인',result.stdout)
        self.assertIn('C:/test path/python.exe',result.stdout)


class InstallerUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.store.save_settings({'online':False,'auto_audio':False,'transparent':False,'borderless':False})
        self.root=tk.Tk();self.root.withdraw();self.app=LyricsApp(self.root,self.store,start_services=False)
        self.ready=patch('audio_install.ready',side_effect=ValueError('optional'));self.ready.start()
    def tearDown(self):
        try:self.app.close()
        finally:self.ready.stop();self.temp.cleanup()

    def test_open_close_reopen_does_not_download_or_enable_analysis(self):
        with patch.object(self.app.audio_installer,'start') as start:
            self.app.open_audio_install();dialog=self.app.audio_install_dialog
            self.assertEqual(str(dialog.download['state']),'normal')
            self.app.open_audio_install();self.assertIs(dialog,self.app.audio_install_dialog)
            dialog.close();self.app.open_audio_install()
            start.assert_not_called();self.assertFalse(self.app.auto_audio.get())
            self.app.audio_install_dialog.download.invoke();start.assert_called_once()

    def test_installed_state_blocks_duplicate_download_and_minimum_window_fits(self):
        self.app.open_audio_install();dialog=self.app.audio_install_dialog
        with patch('audio_install.ready'),patch('audio_install.gpu_upgrade_needed',return_value=False):
            dialog.refresh();self.assertEqual(str(dialog.download['state']),'disabled')
        dialog.window.geometry('520x360+20+20');self.root.update();self.root.update()
        for widget in (dialog.download,dialog.cancel,dialog.done,dialog.log):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),dialog.window.winfo_rootx()+dialog.window.winfo_width())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),dialog.window.winfo_rooty()+dialog.window.winfo_height())
        self.assertGreater(dialog.log.winfo_height(),40)

    def test_existing_cpu_install_can_add_recommended_gpu_model(self):
        self.app.open_audio_install();dialog=self.app.audio_install_dialog
        with patch('audio_install.ready'),patch('audio_install.gpu_upgrade_needed',return_value=True):
            dialog.refresh()
        self.assertEqual(str(dialog.download['state']),'normal')
        self.assertIn('GPU 실시간 분석용',dialog.status.cget('text'))

    def test_compaction_failure_can_retry_even_when_model_is_already_present(self):
        self.app.open_audio_install();dialog=self.app.audio_install_dialog
        self.app.audio_installer.status='설치 실패 · 파일 정리 실패'
        with patch('audio_install.ready'):
            dialog.refresh()
        self.assertEqual(str(dialog.download['state']),'normal')

    def test_missing_ai_opens_choice_without_installing_or_retaining_auto_toggle(self):
        self.app.auto_audio.set(True)
        with patch('auto_audio.ready',side_effect=ValueError('optional')),patch.object(self.app.audio_installer,'start') as start:
            with self.assertRaises(ValueError):self.app.toggle_auto_audio()
            self.assertIsNotNone(self.app.audio_install_dialog);start.assert_not_called()
            self.assertFalse(self.app.auto_audio.get());self.assertFalse(self.store.settings()['auto_audio'])


if __name__=='__main__':unittest.main()
