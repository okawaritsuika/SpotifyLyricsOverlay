"""Compact, anchored control for Spotify volume."""
import queue
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from spotify_volume import SpotifyVolumeController
from ui_theme import ACCENT, BORDER, HOVER, MUTED, PANEL, TEXT, ControlHint, transport_icon
from windows import monitor_work_area, window_position, work_area


def _slider_images(master):
    def rounded(width, height, color):
        image = Image.new('RGBA', (width * 3, height * 3))
        ImageDraw.Draw(image).rounded_rectangle((0, 0, width * 3 - 1, height * 3 - 1),
                                                radius=height * 1.5, fill=color)
        return ImageTk.PhotoImage(image.resize((width, height), Image.Resampling.LANCZOS), master=master)

    track = rounded(10, 5, BORDER)
    thumb = rounded(12, 12, ACCENT)
    disabled_thumb = rounded(12, 12, BORDER)
    style = ttk.Style(master)
    style.configure('Volume.Horizontal.TScale', background=PANEL)
    style.element_create('Volume.trough', 'image', track, border=4, sticky='ew')
    style.element_create('Volume.slider', 'image', thumb, ('disabled', disabled_thumb))
    style.layout('Volume.Horizontal.TScale', [
        ('Volume.trough', {'sticky': 'nswe', 'children': [('Volume.slider', {'side': 'left'})]})
    ])
    style.configure('Volume.TButton', padding=5, background=PANEL, borderwidth=0)
    style.map('Volume.TButton', background=[('active', HOVER)])
    return track, thumb, disabled_thumb


class VolumePopup:
    def __init__(self, parent, anchor):
        self.anchor = anchor
        self.step = 1
        self.closed = self.visible = self.dragging = False
        self.pending = self.poll_timer = self.refresh_timer = None
        self.events = queue.Queue()
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title('Spotify 음량')
        self.window.overrideredirect(True)
        self.window.transient(parent)
        self.window.attributes('-topmost', True)
        self.window.configure(bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self.window.bind('<Escape>', self.hide)
        self.window.bind('<FocusOut>', lambda _: self.window.after_idle(self.dismiss_if_unfocused))
        parent.bind('<Configure>', self.reposition, add='+')
        anchor.bind('<Unmap>', self.hide, add='+')

        row = tk.Frame(self.window, bg=PANEL)
        row.pack(padx=8, pady=5)
        self.icons = (transport_icon(self.window, 'volume', TEXT),
                      transport_icon(self.window, 'volume_muted', ACCENT))
        self.mute_button = ttk.Button(row, text='음소거', image=self.icons[0], compound='image',
                                      style='Volume.TButton', command=self.toggle_mute, state='disabled')
        self.mute_button.pack(side='left', padx=(0, 7))
        self.mute_hint = ControlHint(self.mute_button, '음소거')

        self.volume = tk.DoubleVar(self.window, 0)
        self.slider_images = _slider_images(self.window)
        self.slider = ttk.Scale(row, from_=0, to=100, orient='horizontal',
                                variable=self.volume, command=self.change_volume, length=114,
                                style='Volume.Horizontal.TScale', state='disabled')
        self.slider.pack(side='left')
        self.slider.bind('<ButtonPress-1>', self.start_drag)
        self.slider.bind('<ButtonRelease-1>', self.end_drag)
        for key, direction in (('Left', -1), ('Down', -1), ('Right', 1), ('Up', 1)):
            self.slider.bind(f'<{key}>', lambda _, step=direction: self.step_volume(step))
        self.window.bind('<MouseWheel>', self.scroll_volume)

        self.value_label = tk.Label(row, text='—', bg=PANEL, fg=ACCENT, width=4, anchor='e',
                                    font=('Consolas', 10, 'bold'))
        self.value_label.pack(side='left', padx=(7, 2))
        self.status = tk.Label(self.window, bg=PANEL, fg=MUTED, font=('Malgun Gothic', 8),
                               wraplength=174, justify='left', anchor='w')
        self.controller = SpotifyVolumeController(on_state=self.events.put)
        self.controller.start()
        self.show(refresh=False)

    def reposition(self, event=None):
        if not self.visible or (event is not None and event.widget is not self.window.master):
            return
        ax, ay = self.anchor.winfo_rootx(), self.anchor.winfo_rooty()
        aw, ah = self.anchor.winfo_width(), self.anchor.winfo_height()
        left, top, right, bottom = monitor_work_area((aw, ah, ax, ay), minimum_visible=(1, 1)) or work_area()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x = max(left, min(ax + (aw - width) // 2, right - width))
        y = ay - height - 6
        if y < top:
            y = max(top, min(ay + ah + 6, bottom - height))
        self.window.geometry(window_position(x, y))

    def show(self, *, refresh=True):
        if self.visible:
            return
        self.visible = True
        self.window.update_idletasks()
        self.reposition()
        self.window.deiconify()
        self.window.lift()
        self.slider.focus_force()
        if refresh:
            self.controller.request_refresh()
        self.poll_timer = self.window.after(50, self.poll)
        self.refresh_timer = self.window.after(1000, self.refresh)

    def dismiss_if_unfocused(self):
        if not self.visible:
            return
        focused = self.window.focus_get()
        if focused is not None and focused.winfo_toplevel() is self.window:
            return
        # Let the anchor's button command handle a click that toggles the popup.
        x, y = self.window.winfo_pointerxy()
        if self.window.winfo_containing(x, y) is not self.anchor:
            self.hide()

    def hide(self, _=None):
        if self.visible:
            self.end_drag()
            self.visible = False
            self.stop_polling()
            self.window.withdraw()
        return 'break'

    def start_drag(self, event=None):
        if self.slider.instate(['disabled']):
            return
        self.dragging = True
        if event and self.slider.identify(event.x, event.y) != 'Volume.slider':
            self.slider.set(self.slider.get(event.x, event.y))

    def end_drag(self, _=None):
        self.dragging = False
        if self.pending is not None:
            self.flush_volume()

    def change_volume(self, value):
        if self.closed:
            return
        value = max(0, min(100, round(float(value))))
        if value not in (0, 100):
            value = max(0, min(100, round(value / self.step) * self.step))
        self.volume.set(value)
        self.value_label.configure(text=f'{value}%')
        if self.pending is not None:
            self.window.after_cancel(self.pending)
        self.pending = self.window.after(100, self.flush_volume)

    def step_volume(self, direction):
        if not self.slider.instate(['disabled']):
            self.change_volume(self.volume.get() + direction * self.step)
        return 'break'

    def scroll_volume(self, event):
        if event.delta:
            steps = max(1, abs(event.delta) // 120)
            return self.step_volume(steps if event.delta > 0 else -steps)

    def flush_volume(self):
        if self.pending is not None:
            self.window.after_cancel(self.pending)
        self.pending = None
        if not self.closed:
            self.controller.set_volume(round(self.volume.get()))

    def toggle_mute(self):
        if self.pending is not None:
            self.flush_volume()
        self.controller.toggle_mute()

    def refresh(self):
        if self.pending is None and not self.dragging:
            self.controller.request_refresh()
        self.refresh_timer = self.window.after(1000, self.refresh)

    def poll(self):
        state = None
        while True:
            try:
                state = self.events.get_nowait()
            except queue.Empty:
                break
        if state is not None and state['revision'] >= self.controller.get_state()['revision']:
            self.render(state)
        self.poll_timer = self.window.after(50, self.poll)

    def render(self, state):
        available, muted = state['available'], state['muted']
        self.step = state.get('step', 1)
        widget_state = 'normal' if available else 'disabled'
        self.slider.configure(state=widget_state)
        mute_text = '음소거 해제' if muted else '음소거'
        self.mute_button.configure(state=widget_state, image=self.icons[muted], text=mute_text)
        self.mute_hint.text = mute_text
        if self.pending is None and not self.dragging:
            self.volume.set(state['volume'] if available else 0)
            self.value_label.configure(text=f"{state['volume']}%" if available else '—')
        if state['error'] != self.status.cget('text'):
            self.status.configure(text=state['error'])
            if state['error']:
                self.status.pack(fill='x', padx=10, pady=(0, 7))
            else:
                self.status.pack_forget()
            self.window.after_idle(self.reposition)

    def stop_polling(self):
        for timer in (self.poll_timer, self.refresh_timer):
            if timer is not None:
                self.window.after_cancel(timer)
        self.poll_timer = self.refresh_timer = None

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.visible = False
        self.controller.close()
        if self.pending is not None:
            self.window.after_cancel(self.pending)
        self.stop_polling()
        self.window.destroy()
