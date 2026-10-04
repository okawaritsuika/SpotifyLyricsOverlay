import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

import app_updates as updates


class Response(io.BytesIO):
    def __init__(self, body, url=updates.LATEST_URL):
        super().__init__(body);self.url=url
    def geturl(self):return self.url


def release_for(body=b'MZnew executable', name=updates.EXE_ASSET_NAME, tag='v0.2.0'):
    checksum=(hashlib.sha256(body).hexdigest()+'  '+name+'\n').encode()
    assets={item:{'name':item,'size':len(value),'browser_download_url':updates.REPOSITORY_URL+'/releases/download/'+tag+'/'+item}
            for item,value in ((name,body),(updates.CHECKSUM_ASSET_NAME,checksum))}
    return updates.Release(tag.lstrip('v'),tag,updates.REPOSITORY_URL+'/releases/tag/'+tag,'Notes',assets),checksum


class UpdateDownloadTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='lyrics update 한글 ')
        self.base=Path(self.folder.name)
        updates._verified_downloads.clear()
    def tearDown(self):self.folder.cleanup()

    def test_numeric_version_order_and_invalid_versions(self):
        self.assertTrue(updates.is_newer('0.10.0','0.9.9'))
        self.assertFalse(updates.is_newer('v0.1.0','0.1.0'))
        self.assertFalse(updates.is_newer('0.1.0','0.2.0'))
        for value in ('../0.2.0','0.1.0;command','0.1.0-beta','01.2.3'):
            with self.assertRaises(updates.UpdateError):updates.is_newer(value)

    def test_same_version_release_remains_available_for_data_updates(self):
        release,_=release_for(tag='v0.1.0')
        body={'tag_name':release.tag,'html_url':release.html_url,'body':'자료 갱신','assets':list(release.assets.values())}
        with patch.object(updates,'_open',return_value=Response(json.dumps(body).encode())):
            found=updates.check_release()
        self.assertEqual(found.version,'0.1.0')
        self.assertEqual(found.body,'자료 갱신')

    def test_missing_release_is_not_network_failure(self):
        with patch.object(updates,'_open',side_effect=HTTPError(updates.LATEST_URL,404,'missing',None,None)):
            self.assertIsNone(updates.check_release())
        with patch.object(updates,'_open',side_effect=HTTPError(updates.LATEST_URL,403,'rate limit',None,None)):
            with self.assertRaises(updates.UpdateError):updates.check_release()

    def test_other_repository_and_https_downgrade_rejected_before_network(self):
        for url in ('http://github.com/okawaritsuika/SpotifyLyricsOverlay/releases/download/v0.2.0/SpotifyLyricsOverlay.exe',
                    'https://github.com/other/repo/releases/download/v0.2.0/SpotifyLyricsOverlay.exe'):
            release,_=release_for();release.assets[updates.EXE_ASSET_NAME]['browser_download_url']=url
            with patch.object(updates,'_open') as opened:
                with self.assertRaises(updates.UpdateError):updates.download_asset(release,updates.EXE_ASSET_NAME,self.base/'app.exe')
                opened.assert_not_called()

    def test_redirects_reject_foreign_host_and_allow_github_asset_cdn(self):
        redirect=updates._GitHubRedirects();request=Request(updates.REPOSITORY_URL+'/releases/download/v0.2.0/app.exe')
        for url in ('http://release-assets.githubusercontent.com/a','https://evil.example/a','https://github.com/other/repo/releases/download/v1/app.exe'):
            with self.assertRaises(updates.UpdateError):redirect.redirect_request(request,None,302,'found',{},url)
        self.assertIsNotNone(redirect.redirect_request(request,None,302,'found',{},'https://release-assets.githubusercontent.com/a?token=signed'))

    def test_verified_download_replaces_only_after_digest_matches_and_reports_progress(self):
        body=b'MZdownloaded';release,checksums=release_for(body);destination=self.base/'app with spaces.exe'
        destination.write_bytes(b'previous');progress=Mock()
        with patch.object(updates,'_open',side_effect=[Response(checksums),Response(body)]):
            self.assertEqual(updates.download_asset(release,updates.EXE_ASSET_NAME,destination,progress),destination)
        self.assertEqual(destination.read_bytes(),body)
        self.assertEqual(progress.call_args.args,(len(body),len(body)))
        self.assertFalse(list(self.base.glob('*.part')))

    def test_tampered_short_and_oversized_downloads_preserve_existing_file(self):
        body=b'MZexpected';release,checksums=release_for(body);destination=self.base/'app.exe'
        for received in (b'MZtampered',body[:-1],body+b'extra'):
            destination.write_bytes(b'previous')
            with patch.object(updates,'_open',side_effect=[Response(checksums),Response(received)]):
                with self.assertRaises(updates.UpdateError):updates.download_asset(release,updates.EXE_ASSET_NAME,destination)
            self.assertEqual(destination.read_bytes(),b'previous')
            self.assertFalse(list(self.base.glob('*.part')))

    def test_ambiguous_checksum_is_rejected(self):
        release,checksums=release_for();checksums*=2
        release.assets[updates.CHECKSUM_ASSET_NAME]['size']=len(checksums)
        with patch.object(updates,'_open',return_value=Response(checksums)):
            with self.assertRaises(updates.UpdateError):updates.download_asset(release,updates.EXE_ASSET_NAME,self.base/'app.exe')

    def test_prepare_rechecks_bytes_and_never_runs_unverified_files(self):
        body=b'MZexpected';release,checksums=release_for(body);destination=self.base/'download.exe'
        with patch.object(updates,'_open',side_effect=[Response(checksums),Response(body)]):
            updates.download_asset(release,updates.EXE_ASSET_NAME,destination)
        destination.write_bytes(b'MZmodified')
        with patch.object(updates,'frozen',return_value=True),patch.object(updates,'_start_job') as launch:
            with self.assertRaises(updates.UpdateError):updates.prepare_executable_update(destination,release,self.base/'data')
            launch.assert_not_called()


class UpdateHandoffTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='lyrics handoff 한글 ')
        self.base=Path(self.folder.name);self.job=self.base/'private job';self.job.mkdir()
        self.target=self.base/'Overlay with spaces.exe';self.target.write_bytes(b'MZoriginal')
        self.candidate=self.job/'candidate.exe';self.candidate.write_bytes(b'MZupdated')
        self.data=self.base/'data';self.data.mkdir();(self.data/'settings.json').write_text('{"preserved":true}')
        self.request={'action':'replace','target':str(self.target),'restart_command':[str(self.target),'--data-dir',str(self.data)],
                      'size':self.candidate.stat().st_size,'sha256':updates._sha256(self.candidate),'old_sha256':updates._sha256(self.target)}

    def tearDown(self):self.folder.cleanup()

    def test_atomic_replacement_keeps_backup_and_data(self):
        before=(self.data/'settings.json').read_bytes()
        with patch.object(updates,'_launch_restarted',return_value=Mock(pid=123)) as launch:
            result=updates._apply_job(self.job,self.request)
        self.assertEqual(result['status'],'updated')
        self.assertEqual(self.target.read_bytes(),b'MZupdated')
        self.assertEqual(Path(result['backup']).read_bytes(),b'MZoriginal')
        self.assertEqual((self.data/'settings.json').read_bytes(),before)
        launch.assert_called_once_with(self.request['restart_command'])

    def test_failed_restart_rolls_back_and_restarts_previous_executable(self):
        with patch.object(updates,'_launch_restarted',side_effect=[OSError('start failed'),Mock(pid=123)]) as launch:
            with self.assertRaises(OSError):updates._apply_job(self.job,self.request)
        self.assertEqual(self.target.read_bytes(),b'MZoriginal')
        self.assertEqual(launch.call_count,2)

    def test_failed_second_rename_rolls_back_original(self):
        original=updates.os.replace
        def replace(source,destination):
            if str(source).endswith('.new'):raise OSError('replace failed')
            return original(source,destination)
        with patch.object(updates.os,'replace',side_effect=replace),patch.object(updates,'_launch_restarted') as launch:
            with self.assertRaises(OSError):updates._apply_job(self.job,self.request)
        self.assertEqual(self.target.read_bytes(),b'MZoriginal');launch.assert_called_once()

    def test_late_tampering_restarts_original_without_replacing(self):
        self.candidate.write_bytes(b'MZtampered')
        with patch.object(updates,'_launch_restarted') as launch:
            with self.assertRaises(updates.UpdateError):updates._apply_job(self.job,self.request)
        self.assertEqual(self.target.read_bytes(),b'MZoriginal');launch.assert_called_once()

    def test_bootloader_file_lock_is_retried(self):
        original=updates.os.replace;attempts=[]
        def replace(source,destination):
            attempts.append(1)
            if len(attempts)<3:raise PermissionError('outer bootloader still alive')
            return original(source,destination)
        with patch.object(updates.os,'replace',side_effect=replace),patch.object(updates.time,'sleep'):
            updates._replace_retry(self.candidate,self.base/'received.exe',lambda:False)
        self.assertEqual(len(attempts),3)

    def test_preparation_crash_does_not_report_ready_and_marks_cancelled(self):
        updates._pending_job=None
        with patch.object(updates,'user_dir',return_value=self.base),patch.object(updates,'frozen',return_value=False), \
             patch.object(updates.subprocess,'Popen',return_value=Mock(poll=Mock(return_value=1))):
            with self.assertRaises(updates.UpdateError):updates.restart_app(self.data)
        self.assertIsNone(updates._pending_job)
        self.assertEqual(len(list((self.base/'updates/jobs').glob('*/cancelled'))),1)

    def test_source_restart_uses_argument_list_with_same_data_directory(self):
        with patch.object(updates,'frozen',return_value=False):command=updates._restart_command(self.data)
        self.assertTrue(command[1].endswith('app.py'))
        self.assertEqual(command[-2:],['--data-dir',str(self.data)])

    def test_source_helper_preserves_original_venv_pythonpath(self):
        with patch.object(updates,'frozen',return_value=False),patch.dict(os.environ,{'PYTHONPATH':'original-venv'}), \
             patch.object(updates.subprocess,'Popen') as launch:
            updates._launch_restarted([sys.executable,'-c','pass'])
        self.assertEqual(launch.call_args.kwargs['env']['PYTHONPATH'],'original-venv')

    @unittest.skipUnless(sys.platform=='win32','Windows process handle protocol')
    def test_real_source_helper_acknowledges_and_cancels_without_restarting(self):
        updates._pending_job=None
        try:
            with patch.object(updates,'user_dir',return_value=self.base),patch.object(updates,'frozen',return_value=False):
                job=updates._start_job({'action':'restart','restart_command':[sys.executable,'-c','pass']})
            self.assertTrue((job/'ready.json').is_file())
            (job/'cancelled').touch()
            deadline=time.monotonic()+8
            while not (job/'error.json').is_file() and time.monotonic()<deadline:time.sleep(.1)
            self.assertTrue((job/'error.json').is_file())
            self.assertFalse((job/'result.json').exists())
            updates._helper_process.wait(timeout=8)
        finally:updates._pending_job=None;updates._helper_process=None


if __name__=='__main__':unittest.main()
