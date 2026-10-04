import asyncio
import queue
import unittest
from unittest.mock import patch,Mock
from media import Playback
from reader_bridge import ReaderBridge
from lyrics import Line
from screen_lyrics import lyric_rows,matching_lines,accessible_rows,static_rows,ScreenLyricsMonitor,highlighted_index,scan_highlight,BackgroundHighlight,accessible_spotify_lyrics

class ScreenLyricTests(unittest.TestCase):
    def test_static_main_range_accepts_complete_unsynced_rows_without_footer(self):
        text='가사가 아직 동기화되지 않았습니다.\r\n青い空を見る\r\nMy example\r\n青い空を見る\r\n最後の言葉'
        self.assertEqual(static_rows(text),('青い空を見る','My example','青い空を見る','最後の言葉'))
        self.assertEqual(static_rows('Playlist\n'+text),())
        self.assertEqual(static_rows(text+'\n지금 재생 중 화면 숨기기'),())
        self.assertEqual(accessible_rows(text.splitlines()),())

    def test_static_monitor_never_invents_or_polls_a_highlight(self):
        monitor=ScreenLyricsMonitor(queue.Queue());monitor.observe(Playback(title='Example'))
        monitor.read_track=monitor.static_track=monitor.playback.key
        async def stop_on_sleep(_):monitor.stop()
        with patch('screen_lyrics.scan_highlight') as scan,patch('screen_lyrics.accessible_spotify_lyrics') as read,patch('screen_lyrics.asyncio.sleep',side_effect=stop_on_sleep):
            asyncio.run(monitor.poll(Mock()))
        scan.assert_not_called();read.assert_not_called()
        self.assertTrue(monitor.messages.empty())

    def test_static_lyrics_use_main_range_instead_of_only_visible_children(self):
        auto=Mock();window=Mock(Name='Example',ProcessId=10,NativeWindowHandle=1)
        main=Mock();main.GetPropertyValue.return_value='main'
        banner=Mock(Name='가사가 아직 동기화되지 않았습니다.',ControlType=auto.ControlType.TextControl)
        visible=Mock(Name='青い空を見る',ControlType=auto.ControlType.TextControl)
        auto.GetRootControl.return_value.GetChildren.return_value=[window]
        auto.WalkControl.side_effect=lambda control,**kw:[(main,1)] if control is window else [(banner,1),(visible,1)]
        auto.TextRange.return_value.GetText.return_value='가사가 아직 동기화되지 않았습니다.\n青い空を見る\n新しい言葉\n最後の言葉'
        with patch('screen_lyrics.process_is_spotify',return_value=True):
            originals,handles=accessible_spotify_lyrics(Playback(title='Example'),auto)
        self.assertEqual(originals,('青い空を見る','新しい言葉','最後の言葉'));self.assertIsNone(handles)
        main.GetPattern.return_value.pattern.RangeFromChild.assert_called_once_with(main.Element)
        auto.TextRange.return_value.GetText.assert_called_once_with(200001)

    def test_accessibility_checks_later_main_regions_when_first_has_no_lyrics(self):
        auto=Mock();window=Mock(Name='Example',ProcessId=10,NativeWindowHandle=1)
        empty=Mock();empty.GetPropertyValue.return_value='main'
        main=Mock();main.GetPropertyValue.return_value='main'
        originals=('青い空を見る','最後の言葉')
        rows=[Mock(Name=text,ControlType=auto.ControlType.TextControl) for text in (*originals,'가사 제공: Example')]
        auto.GetRootControl.return_value.GetChildren.return_value=[window]
        def walk(control,**kwargs):
            if control is window:return [(empty,1),(main,1)]
            if control is main:return [(row,1) for row in rows]
            return []
        auto.WalkControl.side_effect=walk
        with patch('screen_lyrics.process_is_spotify',return_value=True):
            found,handles=accessible_spotify_lyrics(Playback(title='Example'),auto)
        self.assertEqual(found,originals);self.assertEqual(len(handles),2)

    def fullscreen_tree(self,originals):
        auto=Mock();window=Mock(Name='Example',ProcessId=10,NativeWindowHandle=1)
        document=Mock(ControlType=auto.ControlType.DocumentControl);document.GetPropertyValue.return_value='document'
        outer=Mock(ControlType=auto.ControlType.GroupControl);outer.GetPropertyValue.return_value='group';outer.GetParentControl.return_value=document
        lyrics=Mock(ControlType=auto.ControlType.GroupControl);lyrics.GetPropertyValue.return_value='group';lyrics.GetParentControl.return_value=outer
        credit=Mock(ControlType=auto.ControlType.GroupControl);credit.GetPropertyValue.return_value='group';credit.GetParentControl.return_value=lyrics
        footer=Mock(Name='가사 제공: Example',ControlType=auto.ControlType.TextControl);footer.GetPropertyValue.return_value='description';footer.GetParentControl.return_value=credit
        rows=[Mock(Name=text,ControlType=auto.ControlType.TextControl) for text in originals]
        metadata=[Mock(Name='Example',ControlType=auto.ControlType.TextControl),Mock(Name='Example Artist',ControlType=auto.ControlType.TextControl),Mock(Name='Play',ControlType=auto.ControlType.ButtonControl)]
        for row in rows+metadata:row.GetPropertyValue.return_value='description'
        auto.GetRootControl.return_value.GetChildren.return_value=[window]
        def walk(control,**kwargs):
            if control is window:return [(outer,1),*[(node,2) for node in metadata],(lyrics,2),*[(row,3) for row in rows],(credit,3),(footer,4)]
            if control is credit:return [(footer,1)]
            if control is lyrics:return [*[(row,1) for row in rows],(credit,1),(footer,2)]
            if control is outer:return [*[(node,1) for node in metadata],(lyrics,1),*[(row,2) for row in rows],(credit,2),(footer,3)]
            raise AssertionError('Must not scan the broader document for lyric text')
        auto.WalkControl.side_effect=walk
        return auto,window,lyrics,outer

    def test_fullscreen_lyrics_without_main_use_nearest_footer_group(self):
        originals=('青い空を見る','最後の言葉','青い空を見る')
        auto,window,lyrics,outer=self.fullscreen_tree(originals)
        auto.TextRange.side_effect=[Mock(GetAttributeValue=Mock(return_value=color)) for color in (0xffd39f,0xffffff,0xffd39f)]
        with patch('screen_lyrics.process_is_spotify',return_value=True):
            found,handles=accessible_spotify_lyrics(Playback(title='Example'),auto)
        self.assertEqual(found,originals);self.assertEqual(scan_highlight(handles,0),(1,True))
        self.assertFalse(any(call.args[0] is outer for call in auto.WalkControl.call_args_list))

    def test_fullscreen_footer_without_lyrics_does_not_capture_player_metadata(self):
        auto,window,lyrics,outer=self.fullscreen_tree(())
        with patch('screen_lyrics.process_is_spotify',return_value=True):
            self.assertEqual(accessible_spotify_lyrics(Playback(title='Example'),auto),((),[]))
        auto.TextRange.assert_not_called()

    def test_monitor_reconnects_when_old_ranges_have_colors_but_no_highlight(self):
        monitor=ScreenLyricsMonitor(queue.Queue());monitor.observe(Playback(title='Example',duration=100))
        monitor.read_track=monitor.playback.key
        clock=[0.];originals=('青い空を見る','最後の言葉')
        async def advance(seconds):
            clock[0]+=seconds
            if clock[0]>6 or any(kind=='spotify_active' for kind,_ in monitor.messages.queue):monitor.stop()
        with patch('screen_lyrics.time.monotonic',side_effect=lambda:clock[0]),patch('screen_lyrics.asyncio.sleep',side_effect=advance),patch('screen_lyrics.accessible_spotify_lyrics',return_value=(originals,[Mock(),Mock()])) as read,patch('screen_lyrics.scan_highlight',side_effect=lambda *args:(1,True) if read.called else (None,True)):
            asyncio.run(monitor.poll(Mock()))
        read.assert_called_once()
        active=[value for kind,value in monitor.messages.queue if kind=='spotify_active']
        self.assertEqual(active[0][:3],(monitor.playback.key,originals,1))

    def test_monitor_rechecks_static_lyrics_when_same_track_becomes_synced(self):
        monitor=ScreenLyricsMonitor(queue.Queue());monitor.observe(Playback(title='Example',duration=100))
        monitor.read_track=monitor.static_track=monitor.playback.key
        clock=[0.];originals=('青い空を見る','最後の言葉')
        async def advance(seconds):
            clock[0]+=seconds
            if clock[0]>8 or any(kind=='spotify_active' for kind,_ in monitor.messages.queue):monitor.stop()
        with patch('screen_lyrics.time.monotonic',side_effect=lambda:clock[0]),patch('screen_lyrics.asyncio.sleep',side_effect=advance),patch('screen_lyrics.accessible_spotify_lyrics',return_value=(originals,[Mock(),Mock()])) as read,patch('screen_lyrics.scan_highlight',return_value=(1,True)):
            asyncio.run(monitor.poll(Mock()))
        read.assert_called_once()
        self.assertEqual(monitor.static_track,'')
        active=[value for kind,value in monitor.messages.queue if kind=='spotify_active']
        self.assertEqual(active[0][:3],(monitor.playback.key,originals,1))

    def test_static_retry_does_not_repeat_lyrics_or_invent_highlight(self):
        monitor=ScreenLyricsMonitor(queue.Queue());monitor.observe(Playback(title='Example',duration=100))
        clock=[0.];originals=('青い空を見る','最後の言葉')
        async def advance(seconds):
            clock[0]+=seconds
            if clock[0]>8:monitor.stop()
        with patch('screen_lyrics.time.monotonic',side_effect=lambda:clock[0]),patch('screen_lyrics.asyncio.sleep',side_effect=advance),patch('screen_lyrics.accessible_spotify_lyrics',return_value=(originals,None)) as read,patch('screen_lyrics.scan_highlight') as scan:
            asyncio.run(monitor.poll(Mock()))
        self.assertEqual(read.call_count,2);scan.assert_not_called()
        self.assertEqual(sum(kind=='spotify_lyrics' for kind,_ in monitor.messages.queue),1)
        self.assertFalse(any(kind=='spotify_active' for kind,_ in monitor.messages.queue))

    def test_filter_rejects_metadata_tiny_text_and_short_common_words(self):
        rows=[('現在の曲のタイトル',5,5,170,25),('静かな空を見上げる',5,40,170,25),('新しい道を歩いている',5,80,170,25),('ありがとう',5,120,80,25),('小さなメニューの表示',5,150,170,9)]
        result=lyric_rows(rows,300,200,['現在の曲のタイトル'])
        self.assertEqual(result,('静かな空を見上げる','新しい道を歩いている'))
        self.assertTrue(matching_lines(result,result));self.assertFalse(matching_lines([result[0],result[0]],result))
    def test_references_are_track_scoped_and_allow_verified_qualified_title(self):
        messages=queue.Queue();bridge=ReaderBridge(messages=messages)
        playback=Playback(title='Example',artist='Artist',playing=True)
        bridge.observe(playback);refs=('静かな空を見上げる','新しい道を歩いている')
        bridge.set_reference(playback.key,refs)
        state=bridge.snapshot();payload={'track':state['track'],'token':state['token'],'url':'https://namu.wiki/w/Example(Series)','text':'synthetic'}
        with patch('reader_bridge.parse_table_text',return_value=tuple(Line(x,'','') for x in refs)):
            self.assertEqual(bridge.receive(payload),'queued')
        with patch('reader_bridge.parse_table_text',return_value=(Line('違う人の別の言葉','',''),)):
            with self.assertRaises(ValueError):bridge.receive(payload)
        bridge.observe(Playback(title='Next',playing=True));bridge.set_reference(playback.key,refs)
        self.assertEqual(bridge.snapshot()['reference_lines'],())
    def test_explicit_user_document_accepts_qualified_title_without_ocr(self):
        bridge=ReaderBridge(messages=queue.Queue());playback=Playback(title='Example',playing=True)
        bridge.observe(playback);state=bridge.snapshot();url='https://namu.wiki/w/Example(Series)'
        bridge.document_urls[state['track']]=url
        with patch('reader_bridge.parse_table_text',return_value=(Line('静かな空を見上げる','',''),)):
            self.assertEqual(bridge.receive({'track':state['track'],'token':state['token'],'url':url,'text':'synthetic'}),'queued')

    def test_accessibility_requires_lyrics_attribution_and_keeps_repeated_lines(self):
        texts=['静かな空を見上げる','新しい道を歩いている','♪','静かな空を見上げる','가사 제공: Example','Other metadata']
        self.assertEqual(accessible_rows(texts),tuple(texts[i] for i in [0,1,3]))
        self.assertEqual(accessible_rows(['Playlist name','Other menu text']),())

    def test_returning_to_previous_track_reads_lyrics_again(self):
        monitor=ScreenLyricsMonitor(queue.Queue());first=Playback(title='First')
        monitor.observe(first);monitor.read_track=first.key
        monitor.observe(first);self.assertEqual(monitor.read_track,first.key)
        monitor.observe(Playback(title='Second'));monitor.observe(first)
        self.assertEqual(monitor.read_track,'')

    def test_highlight_requires_one_white_line_not_a_guess(self):
        self.assertEqual(highlighted_index([0x221100,0xffffff,0xbb8877]),1)
        self.assertIsNone(highlighted_index([0xffffff,0xffffff]))
        self.assertIsNone(highlighted_index([None,0x111111]))

    def test_metadata_mode_accepts_qualified_title_only_without_spotify_reference(self):
        bridge=ReaderBridge(messages=queue.Queue());playback=Playback(title='Example-2022ver',playing=True)
        bridge.observe(playback);state=bridge.snapshot();payload={'track':state['track'],'token':state['token'],'url':'https://namu.wiki/w/Example(Series)','text':'synthetic','match_mode':'metadata'}
        with patch('reader_bridge.parse_table_text',return_value=(Line('静かな空を見上げる','',''),)):
            self.assertEqual(bridge.receive(payload),'queued')
            bridge.set_reference(playback.key,['違う場所に歩いている','別の道へ進んでいく'])
            with self.assertRaises(ValueError):bridge.receive(payload)

    def test_minimized_monitor_still_reads_native_highlight(self):
        monitor=ScreenLyricsMonitor(queue.Queue());monitor.observe(Playback(title='Example'))
        monitor.window_handle=1;monitor.read_track=monitor.playback.key
        async def stop_on_sleep(_):monitor.stop()
        with patch('screen_lyrics.window_minimized',return_value=True),patch('screen_lyrics.scan_highlight',return_value=(1,True)) as scan,patch('screen_lyrics.asyncio.sleep',side_effect=stop_on_sleep):
            asyncio.run(monitor.poll(Mock()))
        scan.assert_called_once()
        kind,payload=monitor.messages.get_nowait()
        self.assertEqual((kind,payload[:3]),('spotify_active',(monitor.playback.key,(),1)))
        self.assertIsInstance(payload[3],float)
        self.assertEqual(monitor.read_track,monitor.playback.key)

    def test_saved_timeline_can_suspend_screen_scans_and_ocr(self):
        monitor=ScreenLyricsMonitor(queue.Queue());monitor.observe(Playback(title='Example'),needed=False)
        async def stop_on_sleep(_):monitor.stop()
        with patch('screen_lyrics.accessible_spotify_lyrics') as read,patch('screen_lyrics.capture_spotify') as capture,patch('screen_lyrics.asyncio.sleep',side_effect=stop_on_sleep):
            asyncio.run(monitor.poll(Mock()))
        read.assert_not_called();capture.assert_not_called()
        monitor.observe(Playback(title='Another'))
        self.assertTrue(monitor.needed)

    def test_background_highlight_falls_back_on_stalled_sample_and_recovers_on_change(self):
        guard=BackgroundHighlight()
        self.assertTrue(guard.accepts(1,20,False))
        self.assertTrue(guard.accepts(1,21,True))
        self.assertTrue(guard.accepts(1,21,True))  # Paused playback has no timeout.
        self.assertFalse(guard.accepts(1,30,True))
        self.assertFalse(guard.accepts(None,30,True))
        self.assertTrue(guard.accepts(2,30,True))
        self.assertTrue(guard.accepts(3,36,True))
        self.assertFalse(guard.accepts(3,45,True))
        self.assertTrue(guard.accepts(3,45,False))

    def test_minimized_window_is_not_rejected_before_accessibility_walk(self):
        auto=Mock();window=Mock(Name='Example',ProcessId=10,NativeWindowHandle=1)
        auto.GetRootControl.return_value.GetChildren.return_value=[window]
        auto.WalkControl.return_value=[]
        with patch('screen_lyrics.process_is_spotify',return_value=True),patch('screen_lyrics.window_minimized',return_value=True):
            accessible_spotify_lyrics(Playback(title='Example'),auto)
        auto.WalkControl.assert_called_once_with(window,maxDepth=30)

    def test_fast_scan_reads_neighbours_and_falls_back_to_full_scan_after_a_seek(self):
        colors=[0x111111]*40;colors[11]=0xffffff;calls=[]
        class Handle:
            def __init__(self,index):self.index=index
            def GetAttributeValue(self,_):calls.append(self.index);return colors[self.index]
        handles=[Handle(i) for i in range(40)]
        self.assertEqual(scan_highlight(handles,0,10),(11,True));self.assertEqual(len(calls),4)
        calls.clear();colors[11]=0x111111;colors[30]=0xffffff
        self.assertEqual(scan_highlight(handles,0,11),(30,True));self.assertEqual(len(calls),40)
