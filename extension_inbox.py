"""Receive user-exported lyric files; never navigate or fetch the source website."""
import ctypes
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import threading
import uuid

from storage import atomic_json,read_json
from web_lyrics import parse_table_text,validate_url,source_id


def downloads_folder():
    try:
        guid=ctypes.create_string_buffer(uuid.UUID('374DE290-123F-4565-9164-39C4925E467B').bytes_le)
        pointer=ctypes.c_void_p()
        shell=ctypes.windll.shell32
        shell.SHGetKnownFolderPath.argtypes=(ctypes.c_void_p,ctypes.c_ulong,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p))
        if shell.SHGetKnownFolderPath(guid,0,None,ctypes.byref(pointer))==0:
            try:return Path(ctypes.wstring_at(pointer))
            finally:
                ctypes.windll.ole32.CoTaskMemFree.argtypes=(ctypes.c_void_p,)
                ctypes.windll.ole32.CoTaskMemFree(pointer)
    except (AttributeError,OSError):pass
    return Path.home()/'Downloads'


def parse_export(data):
    value=json.loads(data)
    if not isinstance(value,dict) or value.get('format')!='spotify-lyrics-extension' or value.get('version')!=1:
        raise ValueError('지원하지 않는 확장 가사 파일입니다.')
    title=value.get('title');text=value.get('table_text');url=value.get('url')
    if not isinstance(title,str) or not title.strip() or len(title)>200 or not isinstance(text,str) or not isinstance(url,str):
        raise ValueError('확장 가사 파일의 제목과 본문을 확인하세요.')
    source={'url':validate_url(url),'title':title.strip(),'fetched_at':datetime.now(timezone.utc).isoformat(),
            'translation':'사용자가 확장에서 보낸 한국어 번역','reading':'로컬 자동 히라가나 독음'}
    return title.strip(),parse_table_text(text,source=source_id(source['url'])),source


class ExtensionInbox:
    def __init__(self,store,messages,directory=None):
        self.store,self.messages=store,messages
        self.directory=Path(directory) if directory else downloads_folder()/'SpotifyLyricsOverlay'
        self.receipt_path=store.directory/'extension_receipts.json'
        self.receipts=read_json(self.receipt_path,{})
        self.seen=set(self.receipts)
        self.file_stamps={}
        self.stop_event=threading.Event()
        self.thread=threading.Thread(target=self._run,daemon=True,name='extension-lyrics-inbox')

    def start(self):self.thread.start()

    def scan(self):
        for path in self.directory.glob('*.spotifylyrics.json'):
            try:
                info=path.stat()
                stamp=(info.st_mtime_ns,info.st_size)
                if self.file_stamps.get(path)==stamp:continue
                with path.open('rb') as handle:data=handle.read(1_000_001)
                self.file_stamps[path]=stamp
                key=hashlib.sha256(data).hexdigest()
                if key in self.seen:continue
                self.seen.add(key)
                if len(data)>1_000_000:raise ValueError('확장 가사 파일이 너무 큽니다.')
                result=parse_export(data.decode('utf-8-sig'))
                error=''
            except (OSError,UnicodeError,ValueError) as exception:
                if isinstance(exception,OSError):continue
                result=None
                error=str(exception) if isinstance(exception,ValueError) and not isinstance(exception,json.JSONDecodeError) else '확장 가사 파일을 읽지 못했습니다.'
            self.messages.put(('extension_lyrics',(key,result,error)))

    def _run(self):
        while not self.stop_event.is_set():
            try:self.scan()
            except OSError:pass
            self.stop_event.wait(2)

    def acknowledge(self,key):
        self.receipts[key]=True
        atomic_json(self.receipt_path,self.receipts)

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():self.thread.join(timeout=2)
