"""Small Spotify volume panel; audio work stays off the Tk thread."""
import queue
import tkinter as tk
from tkinter import ttk

from spotify_volume import SpotifyVolumeController
from ui_theme import BG, TEXT, MUTED, ACCENT


class VolumePopup:
    def __init__(self, parent, anchor):
        self.closed = False
        self.pending = None
        self.dragging = False
        self.step = 1
        self.events = queue.Queue()
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title('Spotify 음량')
        self.window.configure(bg=BG)
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.attributes('-topmost', True)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda event: self.close())
        self.volume = tk.DoubleVar(self.window, 0)
        header = tk.Frame(self.window, bg=BG)
        header.pack(fill='x', padx=16, pady=(12, 4))
        tk.Label(header, text='Spotify 음량', bg=BG, fg=TEXT,
                 font=('Malgun Gothic', 10, 'bold')).pack(side='left')
        self.value_label = tk.Label(header, text='—', bg=BG, fg=ACCENT,
                                    font=('Malgun Gothic', 10))
        self.value_label.pack(side='right')
        self.slider = ttk.Scale(self.window, from_=0, to=100, orient='horizontal',
                                variable=self.volume, command=self.change_volume,
                                length=250, state='disabled')
        self.slider.pack(fill='x', padx=16, pady=4)
        self.slider.bind('<ButtonPress-1>', self.start_drag, add='+')
        self.slider.bind('<ButtonRelease-1>', self.end_drag, add='+')
        self.slider.bind('<Left>', lambda event:self.step_volume(-1))
        self.slider.bind('<Right>', lambda event:self.step_volume(1))
        footer = tk.Frame(self.window, bg=BG)
        footer.pack(fill='x', padx=16, pady=(6, 4))
        self.mute_button = ttk.Button(footer, text='음소거', command=self.toggle_mute,
                                      state='disabled')
        self.mute_button.pack(side='left')
        ttk.Button(footer, text='닫기', command=self.close).pack(side='right')
        self.status = tk.Label(self.window, text='Spotify 음량을 확인하는 중…',
                               bg=BG, fg=MUTED, font=('Malgun Gothic', 9),
                               wraplength=250, justify='left', anchor='w')
        self.status.pack(fill='x', padx=16, pady=(4, 12))
        self.controller = SpotifyVolumeController(on_state=self.events.put)
        self.controller.start()
        self.poll_timer = self.window.after(50, self.poll)
        self.refresh_timer = self.window.after(1000, self.refresh)
        self.window.update_idletasks()
        from windows import work_area
        left, top, right, bottom = work_area()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x = max(left, min(anchor.winfo_rootx(), right-width-8))
        y = anchor.winfo_rooty()-height-32
        if y < top:y = min(anchor.winfo_rooty()+anchor.winfo_height()+8, bottom-height-32)
        self.window.geometry(f'+{x}+{max(top, y)}')
        self.window.deiconify()
        self.window.lift()
        self.slider.focus_set()

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.window.focus_set()
        self.controller.request_refresh()

    def start_drag(self, _=None):
        self.dragging = True

    def end_drag(self, _=None):
        self.dragging = False
        if self.pending is not None:self.flush_volume()

    def change_volume(self, value):
        if self.closed:return
        value = max(0, min(100, round(float(value)/self.step)*self.step))
        self.volume.set(value)
        self.value_label.configure(text=f'{value}%')
        if self.pending is not None:self.window.after_cancel(self.pending)
        self.pending = self.window.after(100, self.flush_volume)

    def step_volume(self, direction):
        if not self.slider.instate(['disabled']):
            self.change_volume(self.volume.get()+direction*self.step)
        return 'break'

    def flush_volume(self):
        if self.pending is not None:self.window.after_cancel(self.pending)
        self.pending = None
        if not self.closed:self.controller.set_volume(round(self.volume.get()))

    def toggle_mute(self):
        if self.closed:return
        if self.pending is not None:self.flush_volume()
        self.controller.toggle_mute()

    def refresh(self):
        if self.closed:return
        if self.pending is None and not self.dragging:self.controller.request_refresh()
        self.refresh_timer = self.window.after(1000, self.refresh)

    def poll(self):
        if self.closed:return
        state = None
        while True:
            try:state = self.events.get_nowait()
            except queue.Empty:break
        if state is not None and state['revision'] >= self.controller.get_state()['revision']:
            available = state['available']
            self.step = state.get('step', 1)
            self.slider.configure(state='normal' if available else 'disabled')
            self.mute_button.configure(state='normal' if available else 'disabled',
                                       text='음소거 해제' if state['muted'] else '음소거')
            if self.pending is None and not self.dragging:
                if available:
                    # Updating the Tk variable must never send a volume command.
                    self.volume.set(state['volume'])
                    self.value_label.configure(text=f"{state['volume']}%")
                else:self.value_label.configure(text='—')
            self.status.configure(text=state['error'] or (
                'Spotify 음소거 중' if state['muted'] else f'Spotify와 연동 중 · {self.step}% 단위'))
        self.poll_timer = self.window.after(50, self.poll)

    def close(self):
        if self.closed:return
        self.closed = True
        self.controller.close()
        for timer in (self.pending, self.poll_timer, self.refresh_timer):
            if timer is not None:self.window.after_cancel(timer)
        self.window.destroy()
