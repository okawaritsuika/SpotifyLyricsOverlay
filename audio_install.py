"""Explicit optional audio installation; no downloads while importing or opening UI."""
from collections import deque
import importlib
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import threading
import tkinter as tk
from tkinter import ttk
from audio_job import ready
from app_paths import frozen, worker_command, worker_environment


class AudioInstaller:
    def __init__(self,base,kind='audio'):
        self.base=Path(base);self.process=None;self.thread=None
        self.kind=kind
        self.lock=threading.RLock();self.cancelled=threading.Event()
        self.running=False;self.status='설치 전';self.lines=deque(maxlen=160)

    def snapshot(self):
        with self.lock:return self.running,self.status,tuple(self.lines)

    def start(self):
        with self.lock:
            if self.running:return
            if not frozen() and not (self.base/'setup-audio.ps1').is_file():raise ValueError('설치 파일 setup-audio.ps1을 찾지 못했습니다.')
            self.running=True;self.cancelled.clear();self.lines.clear();self.status='다운로드·설치 준비 중'
            self.thread=threading.Thread(target=self._run,daemon=True,name='optional-audio-install')
            self.thread.start()

    def _run(self):
        try:
            python=Path(sys.executable)
            if python.name.lower()=='pythonw.exe':python=python.with_name('python.exe')
            executable=Path(getattr(sys,'_base_executable',python))
            if executable.name.lower()=='pythonw.exe':executable=executable.with_name('python.exe')
            env=dict(os.environ);env.update(PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1',PIP_DISABLE_PIP_VERSION_CHECK='1',
                PYTHONPATH=sysconfig.get_path('purelib'))
            command=[str(executable),'-X','utf8',str(self.base/'audio_install.py'),'--worker',str(self.base),str(python)]
            if frozen():
                command=worker_command(self.base,'optional_runtime')+[self.kind]
                env=worker_environment(self.base)
            with self.lock:
                if self.cancelled.is_set():return
                self.process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                    encoding='utf-8',errors='replace',env=env,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0))
                process=self.process
            for line in process.stdout:
                with self.lock:
                    text=line.rstrip()[-2000:];self.lines.append(text)
                    if text.startswith(('1/4 ','2/4 ','3/4 ','4/4 ')) and not self.cancelled.is_set():self.status=text
            code=process.wait()
            with self.lock:
                if self.cancelled.is_set():self.status='설치 취소됨 · 다시 다운로드를 누르면 설치를 재시도합니다.'
                elif code:self.status='설치 실패 · 아래 내용을 확인하고 다시 다운로드를 누르세요.'
                else:
                    importlib.invalidate_caches()
                    if self.kind=='audio':ready(self.base)
                    self.status=('설치 완료 · 가사 연결에서 Spotify 연결을 다시 누르세요.' if self.kind=='browser'
                                 else '설치 완료 · 자동 분석을 켜거나 분석 대기를 시작하세요.')
        except Exception as error:
            with self.lock:
                self.status='설치 실패 · 다시 시도할 수 있습니다.'
                self.lines.append(str(error) if isinstance(error,ValueError) else type(error).__name__)
        finally:
            with self.lock:
                if self.process and self.process.stdout:self.process.stdout.close()
                if self.cancelled.is_set() and self.status=='설치 취소 중':self.status='설치 취소됨 · 다시 다운로드를 누르면 설치를 재시도합니다.'
                self.running=False;self.process=None

    def cancel(self):
        with self.lock:
            if not self.running:return
            self.cancelled.set();self.status='설치 취소 중'
            process=self.process
        if process and process.poll() is None:
            # Only this installer's process tree, never other Python/app processes.
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=10)

    def close(self):
        self.cancel()
        if self.thread:self.thread.join(timeout=3)


class AudioInstallDialog:
    def __init__(self,app,kind='audio'):
        self.app=app;self.last_lines=None;self.after=None
        self.kind=kind
        if kind=='browser' and not getattr(app,'browser_installer',None):app.browser_installer=AudioInstaller(app.audio_installer.base,kind=kind)
        self.installer=app.browser_installer if kind=='browser' else app.audio_installer
        self.window=tk.Toplevel(app.root);self.window.title('AI 음원 분석 · 선택 설치')
        self.window.geometry('600x400');self.window.minsize(520,360)
        self.window.columnconfigure(0,weight=1);self.window.rowconfigure(2,weight=1)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.help=tk.Label(self.window,text='AI 없이도 가사·번역·후리가나, 수동 싱크와 저장된 싱크를 사용할 수 있습니다.\n'
            '재생음을 분석할 때만 아래 버튼으로 설치하세요. API 키는 필요 없습니다.\n'
            'small 모델 약 486MB 포함 · 새 설치 완료 후 전체 약 1.8GB 예상(환경별 차이).\n'
            '설치 중에는 정리 전 파일과 다운로드 임시 공간이 별도로 필요합니다.\n'
            'Python 공식 사이트·PyTorch·PyPI·Hugging Face에서 받습니다. 설치된 모델은 다시 받지 않습니다.\n'
            '이 창을 닫아도 설치는 계속됩니다. 취소하려면 설치 취소를 누르세요.',
            justify='left',anchor='w',wraplength=570)
        self.help.grid(row=0,column=0,sticky='ew',padx=12,pady=10)
        if kind=='browser':
            self.window.title('Spotify 연결 브라우저 · 선택 설치')
            self.help.configure(text='Spotify 로그인과 웹 가사 연결에 필요한 브라우저 모듈을 설치합니다.\n'
                'Python 공식 사이트와 PyPI에서 선택 기능을 다운로드합니다. AI 모델은 받지 않습니다.\n'
                'Microsoft Edge WebView2 Runtime이 필요합니다. 설치 후 Spotify 연결을 다시 누르세요.\n'
                '저장된 가사·번역·싱크는 설치하지 않아도 사용할 수 있습니다.')
        self.status=tk.Label(self.window,justify='left',anchor='w',wraplength=570)
        self.status.grid(row=1,column=0,sticky='ew',padx=12,pady=(0,6))
        frame=tk.Frame(self.window);frame.grid(row=2,column=0,sticky='nsew',padx=12)
        frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        self.log=tk.Text(frame,wrap='word',height=5,state='disabled')
        self.log.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(frame,command=self.log.yview);scroll.grid(row=0,column=1,sticky='ns')
        self.log.configure(yscrollcommand=scroll.set)
        actions=tk.Frame(self.window);actions.grid(row=3,column=0,sticky='ew',padx=12,pady=10)
        self.download=tk.Button(actions,text='AI 다운로드·설치',command=lambda:app.safe(self.start))
        if kind=='browser':self.download.configure(text='브라우저 다운로드·설치')
        self.download.pack(side='left')
        self.cancel=tk.Button(actions,text='설치 취소',command=lambda:app.safe(self.cancel_install))
        self.cancel.pack(side='left',padx=8)
        self.done=tk.Button(actions,text='닫기',command=self.close);self.done.pack(side='right')
        self.window.bind('<Configure>',self.resize)
        self.refresh()

    def resize(self,event):
        if event.widget is self.window:
            for widget in (self.help,self.status):widget.configure(wraplength=max(300,event.width-28))

    def start(self):
        self.installer.start();self.refresh()

    def cancel_install(self):
        # Cancelling a package install may need a moment; keep the UI responsive.
        threading.Thread(target=self.installer.cancel,daemon=True).start()

    def refresh(self):
        if self.after:self.window.after_cancel(self.after)
        running,status,lines=self.installer.snapshot()
        installed=False
        if not running:
            if self.kind=='browser':
                from optional_runtime import installed as runtime_installed
                installed=runtime_installed('browser')
            else:
                try:ready(self.installer.base);installed=True
                except ValueError:pass
            if installed and status=='설치 전':status='이미 설치되어 있습니다. 추가 다운로드 없이 분석할 수 있습니다.'
        self.status.configure(text=status)
        retry='실패' in status or '취소됨' in status
        self.download.configure(state='disabled' if running or installed and not retry else 'normal')
        self.cancel.configure(state='normal' if running else 'disabled')
        if lines!=self.last_lines:
            self.log.configure(state='normal');self.log.delete('1.0','end')
            self.log.insert('1.0','\n'.join(lines));self.log.see('end');self.log.configure(state='disabled');self.last_lines=lines
        self.after=self.window.after(300,self.refresh)

    def close(self):
        if self.after:self.window.after_cancel(self.after);self.after=None
        if self.installer.running:self.app.notify('선택 기능 설치는 계속 진행됩니다. 설치창을 다시 열어 상태를 확인하거나 취소할 수 있습니다.',10)
        self.window.destroy()
        if self.kind=='browser':self.app.browser_install_dialog=None
        else:self.app.audio_install_dialog=None


def worker(base,python):
    from analysis_limits import apply_analysis_limits
    apply_analysis_limits(cpu_percent=2,memory_mb=1024)
    script=Path(base)/'setup-audio.ps1'
    if not script.is_file():raise ValueError('설치 스크립트를 찾지 못했습니다.')
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    return subprocess.call([str(powershell),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                            '-File',str(script),'-Python',str(python)])


if __name__=='__main__':
    if len(sys.argv)!=4 or sys.argv[1]!='--worker':raise SystemExit(2)
    raise SystemExit(worker(sys.argv[2],sys.argv[3]))
