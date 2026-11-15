"""Edit global shortcuts by pressing the desired keys in a local Tk dialog."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from shortcuts import ACTIONS,DEFAULTS,ShortcutMatcher,shortcut_label,validate_bindings

MODIFIER_KEYS={'Shift_L':0xA0,'Shift_R':0xA1,'Control_L':0xA2,'Control_R':0xA3,'Alt_L':0xA4,'Alt_R':0xA5,'Win_L':0x5B,'Win_R':0x5C,'Super_L':0x5B,'Super_R':0x5C}

class ShortcutDialog:
    def __init__(self,parent,hotkeys,on_save):
        self.hotkeys=hotkeys;self.on_save=on_save;self.values=deepcopy(hotkeys.bindings);self.scheduler=parent._root()
        self.capturing=None;self.held=set();self.closed=False
        self.window=tk.Toplevel(parent);self.window.title('단축키 설정')
        self.window.geometry('610x500');self.window.minsize(510,400)
        self.window.attributes('-topmost',False)
        self.window.columnconfigure(0,weight=1);self.window.rowconfigure(1,weight=1)
        ttk.Label(self.window,text='기능을 선택하고 「키 지정」을 누른 뒤 원하는 조합을 누르세요.\n왼쪽 / 오른쪽 Shift를 구분합니다. Esc는 키 지정 취소입니다.',wraplength=480).grid(row=0,column=0,sticky='ew',padx=14,pady=12)
        area=ttk.Frame(self.window);area.grid(row=1,column=0,sticky='nsew',padx=14)
        area.columnconfigure(0,weight=1);area.rowconfigure(0,weight=1)
        self.tree=ttk.Treeview(area,columns=('action','key'),show='headings',selectmode='browse',height=11)
        self.tree.heading('action',text='기능');self.tree.heading('key',text='단축키')
        self.tree.column('action',width=270,minwidth=210);self.tree.column('key',width=230,minwidth=160)
        self.tree.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(area,orient='vertical',command=self.tree.yview);scroll.grid(row=0,column=1,sticky='ns');self.tree.configure(yscrollcommand=scroll.set)
        for action,(_,label) in ACTIONS.items():self.tree.insert('','end',iid=action,values=(label,shortcut_label(self.values[action])))
        first = next(iter(ACTIONS))
        self.tree.selection_set(first);self.tree.focus(first)
        self.status=tk.StringVar(value='방향키·F1–F24는 단독 지정 가능 · 가사 오버레이와 편집창에 포커스가 있을 때 동작합니다.')
        ttk.Label(self.window,textvariable=self.status,wraplength=480).grid(row=2,column=0,sticky='ew',padx=14,pady=10)
        controls=ttk.Frame(self.window);controls.grid(row=3,column=0,sticky='ew',padx=14,pady=(0,12))
        self.buttons=[]
        for column,(title,command) in enumerate([('키 지정',self.begin),('사용 안 함',self.disable),('기본값',self.defaults),('저장',self.save),('닫기',self.close)]):
            controls.columnconfigure(column,weight=1)
            button=ttk.Button(controls,text=title,command=command);button.grid(row=0,column=column,sticky='ew',padx=2);self.buttons.append(button)
        self.window.bind('<KeyPress>',self.key_down);self.window.bind('<KeyRelease>',self.key_up)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.bind('<Destroy>',self.destroyed)
        self.window.grab_set()

    def refresh(self):
        for action,(_,label) in ACTIONS.items():self.tree.item(action,values=(label,shortcut_label(self.values[action])))
    def begin(self):
        if not self.tree.selection():return
        self.capturing=self.tree.selection()[0];self.held.clear();self.hotkeys.suspend(True)
        for button in self.buttons:button.configure(state='disabled')
        self.status.set('단축키 입력 준비 중…');self.scheduler.after(20,self.arm)
    def arm(self):
        if self.closed or not self.capturing:return
        if self.hotkeys.thread.is_alive() and not self.hotkeys.ready.is_set():self.scheduler.after(20,self.arm);return
        self.status.set(ACTIONS[self.capturing][1]+' · 원하는 키 조합을 누르세요. (Esc 취소)');self.window.focus_force()
    def finish(self):
        self.capturing=None;self.held.clear();self.hotkeys.suspend(False)
        for button in self.buttons:button.configure(state='normal')
    def key_up(self,event):
        self.held.discard(MODIFIER_KEYS.get(event.keysym));return 'break' if self.capturing else None
    def key_down(self,event):
        if not self.capturing:return
        if event.keysym in MODIFIER_KEYS:self.held.add(MODIFIER_KEYS[event.keysym]);return 'break'
        if event.keysym=='Escape':self.finish();self.status.set('키 지정을 취소했습니다.');return 'break'
        if self.hotkeys.thread.is_alive() and not self.hotkeys.ready.is_set():return 'break'
        mods=0
        for key in self.held:mods|=ShortcutMatcher.modifier_bits[key]
        if event.state&4:mods|=2
        if event.state&1:mods|=4
        side='right' if 0xA1 in self.held else 'left' if 0xA0 in self.held else 'any'
        proposed={**self.values,self.capturing:{'key':event.keycode,'modifiers':mods,'shift':side}}
        try:self.values=validate_bindings(proposed)
        except ValueError as error:self.status.set(str(error));return 'break'
        self.finish();self.refresh();self.status.set('키를 지정했습니다. 저장을 눌러 적용하세요.');return 'break'
    def disable(self):
        if self.tree.selection():self.values[self.tree.selection()[0]]=None;self.refresh()
    def defaults(self):self.values=deepcopy(DEFAULTS);self.refresh();self.status.set('기본값을 불러왔습니다. 저장을 눌러 적용하세요.')
    def save(self):
        try:self.on_save(validate_bindings(self.values))
        except (ValueError,OSError) as error:self.status.set(str(error));return
        self.status.set('저장·적용했습니다. 가사 앱 창을 클릭한 상태에서 사용하세요.')
    def close(self):
        if self.closed:return
        self.closed=True
        if self.capturing:self.finish()
        self.window.destroy()
    def destroyed(self,event):
        if event.widget is self.window and not self.closed:
            self.closed=True
            if self.capturing:self.capturing=None;self.hotkeys.suspend(False)
