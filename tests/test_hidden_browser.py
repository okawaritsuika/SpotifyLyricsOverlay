"""Keep automatic WebView initialization hidden without changing visible readers."""
import io
import json
import queue
import subprocess
import sys
import tempfile
import threading
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from browser_worker import main,prepare_background_window
from browser_visibility import show_original_window
from internal_reader import BrowserJob
from spotify_worker import SpotifyBrowser,main as spotify_main


class Event:
    def __init__(self,*handlers):self.handlers=list(handlers);self.set=Mock()
    def __iadd__(self,handler):self.handlers.append(handler);return self
    def __isub__(self,handler):self.handlers.remove(handler);return self
    def fire(self,*args):
        for handler in self.handlers:handler(*args)


def window(hidden=True):
    form=SimpleNamespace(ShowInTaskbar=True,Opacity=1,Handle=Mock(),on_shown=Mock())
    form.Handle.ToInt64.return_value=1234
    form.Shown=Event(form.on_shown)
    return SimpleNamespace(hidden=hidden,native=form,
                           events=SimpleNamespace(before_show=Event(),initialized=Event(),
                                                  shown=Event(),closing=Event(),closed=Event()))


class HiddenBrowserTests(unittest.TestCase):
    def backend(self):
        calls=[]
        class EdgeChrome:
            def __init__(self,form,owner,cache):
                calls.append((form.ShowInTaskbar,form.Opacity,owner,cache))
        module=ModuleType('webview.platforms.edgechromium');module.EdgeChrome=EdgeChrome
        return module,calls

    def test_hidden_properties_are_set_before_webview_controller_initialization(self):
        target=window();module,calls=self.backend()
        with patch.dict(sys.modules,{'webview.platforms.edgechromium':module}):
            prepare_background_window(target,'edgechromium')
            self.assertTrue(target.native.ShowInTaskbar)
            module.EdgeChrome(target.native,target,'temporary-profile')
        self.assertEqual(calls,[(False,0,target,'temporary-profile')])

    def test_process_hook_only_affects_the_owned_automatic_window(self):
        target=window();other=window(False);module,calls=self.backend()
        with patch.dict(sys.modules,{'webview.platforms.edgechromium':module}):
            prepare_background_window(target,'edgechromium')
            module.EdgeChrome(other.native,other,'other-profile')
        self.assertEqual(calls,[(True,1,other,'other-profile')])

    def test_hidden_shown_signal_remains_ready_without_calling_native_focus_handler(self):
        target=window();module,_=self.backend();user=Mock()
        user.GetWindowLongPtrW.return_value=0x00040000|0x100
        with patch.dict(sys.modules,{'webview.platforms.edgechromium':module}),patch('browser_visibility.ctypes.WinDLL',return_value=user):
            prepare_background_window(target,'edgechromium')
            module.EdgeChrome(target.native,target,'temporary-profile')
            target.events.before_show.fire()
            target.native.Shown.fire(None,None)
        target.native.on_shown.assert_not_called()
        target.events.shown.set.assert_called_once_with()
        user.SetWindowLongPtrW.assert_called_once_with(1234,-20,0x08000000|0x80|0x100)
        self.assertFalse(target.native.ShowInTaskbar)
        self.assertEqual(target.native.Opacity,0)

    def test_visible_reader_does_not_modify_backend_or_shown_behavior(self):
        target=window(False);module,_=self.backend();original=module.EdgeChrome.__init__
        with patch.dict(sys.modules,{'webview.platforms.edgechromium':module}),patch('browser_visibility.ctypes.WinDLL') as native:
            prepare_background_window(target,'edgechromium')
        self.assertIs(module.EdgeChrome.__init__,original)
        self.assertEqual(target.events.before_show.handlers,[])
        target.native.Shown.fire(None,None)
        target.native.on_shown.assert_called_once_with(None,None)
        native.assert_not_called()

    def test_other_backend_is_not_patched_with_winforms_policy(self):
        target=window()
        with patch('browser_visibility.ctypes.WinDLL') as native:prepare_background_window(target,'qt')
        self.assertEqual(target.events.before_show.handlers,[])
        native.assert_not_called()

    def test_main_registers_early_hook_only_for_automatic_window(self):
        for start in (main,spotify_main):
            for visible in (False,True):
                with self.subTest(worker=start.__module__,visible=visible),tempfile.TemporaryDirectory() as directory:
                    target=window(not visible);webview=ModuleType('webview')
                    webview.settings={};webview.create_window=Mock(return_value=target);webview.start=Mock()
                    request={'state':{},'visible':visible,'profile':directory}
                    with patch.dict(sys.modules,{'webview':webview}),patch('sys.stdin',io.StringIO(json.dumps(request)+'\n')):
                        start()
                    options=webview.create_window.call_args.kwargs
                    self.assertEqual((options['hidden'],options['focus']),(not visible,visible))
                    self.assertEqual(target.events.initialized.handlers,[] if visible else [prepare_background_window])

    def test_closing_visible_spotify_window_before_login_reports_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            target=window(False);webview=ModuleType('webview')
            webview.settings={};webview.create_window=Mock(return_value=target)
            webview.start=Mock(side_effect=lambda *args,**kwargs:target.events.closing.fire())
            request={'visible':True,'profile':directory}
            with patch.dict(sys.modules,{'webview':webview}),patch('sys.stdin',io.StringIO(json.dumps(request)+'\n')),\
                    patch('spotify_worker.emit') as emit:
                spotify_main()
            self.assertEqual(emit.call_args.args,('status',))
            self.assertEqual(emit.call_args.kwargs['state'],'login_required')
            self.assertTrue(emit.call_args.kwargs['login_cancelled'])
            self.assertFalse(emit.call_args.kwargs['authenticated'])

    def test_explicit_show_restores_taskbar_focus_and_original_handler_once(self):
        target=window();module,_=self.backend();user=Mock()
        target.show=Mock();target.native.Invoke=Mock(side_effect=lambda action:action())
        user.GetWindowLongPtrW.return_value=0x08000000|0x80|0x100
        system=ModuleType('System');system.Action=lambda callback:callback
        with patch.dict(sys.modules,{'webview.platforms.edgechromium':module,'System':system}),patch('browser_visibility.ctypes.WinDLL',return_value=user):
            prepare_background_window(target,'edgechromium')
            module.EdgeChrome(target.native,target,'temporary-profile')
            target.events.before_show.fire()
            show_original_window(target);show_original_window(target)
        self.assertEqual((target.hidden,target.focus),(False,True))
        self.assertEqual((target.native.ShowInTaskbar,target.native.Opacity),(True,1))
        self.assertEqual(target.native.Shown.handlers,[target.native.on_shown])
        self.assertIsNone(target._background_restore)
        target.native.Invoke.assert_called_once()
        self.assertEqual(target.show.call_count,2)
        user.SetWindowLongPtrW.assert_called_with(1234,-20,0x00040000|0x100)

    def test_already_visible_window_keeps_normal_show_without_native_restore(self):
        target=window(False);target.show=Mock();target.native.Invoke=Mock()
        show_original_window(target)
        target.show.assert_called_once_with()
        target.native.Invoke.assert_not_called()

    def test_explicit_spotify_login_command_restores_window_before_login_navigation(self):
        target=Mock();browser=SpotifyBrowser(target,queue.Queue(),threading.Event())
        order=[];target.load_url.side_effect=lambda url:order.append('login_navigation')
        with patch('spotify_worker.show_original_window',side_effect=lambda value:order.append('restore_and_show')) as show:
            self.assertTrue(browser.command({'action':'show'}))
        show.assert_called_once_with(target)
        self.assertEqual(order,['restore_and_show','login_navigation'])

    def test_shared_visibility_policy_is_included_in_source_distribution(self):
        from package_release import MODULES
        self.assertIn('browser_visibility',MODULES)

    def test_worker_console_stays_hidden_even_for_an_explicit_original_window(self):
        for visible in (False,True):
            with self.subTest(visible=visible):
                process=SimpleNamespace(stdin=Mock())
                with patch('internal_reader.subprocess.Popen',return_value=process) as launch,patch('internal_reader.threading.Thread') as threads:
                    job=BrowserJob('.',{'token':'synthetic','track':'synthetic'},queue.Queue(),visible=visible)
                job._stop_input.set()
                writer=threads.call_args_list[0].kwargs
                writer['target'](*writer['args'])
                self.assertTrue(launch.call_args.kwargs['creationflags']&subprocess.CREATE_NO_WINDOW)
                self.assertEqual(json.loads(process.stdin.write.call_args.args[0])['visible'],visible)


if __name__=='__main__':unittest.main()
