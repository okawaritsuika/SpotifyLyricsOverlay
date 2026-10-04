"""Shared desktop colors and readable native controls."""
import tkinter as tk
from tkinter import ttk
from PIL import Image,ImageDraw,ImageTk

BG, PANEL, TEXT, MUTED, ACCENT = '#10151f', '#1d2635', '#f4f7fc', '#afbbcf', '#99e3c8'


def transport_arrow(master, *, right=False, double=False):
    """Center arrow artwork independently of font ascent and display scaling."""
    scale=3
    image=Image.new('RGBA',(16*scale,16*scale))
    draw=ImageDraw.Draw(image)
    for start in ((2,8) if double else (5,)):
        points=[(start,3),(start+5,7.5),(start,12)]
        if not right:points=[(15-x,y) for x,y in points]
        draw.line([((x+.5)*scale-.5,(y+.5)*scale-.5) for x,y in points],
                  fill=TEXT,width=5,joint='curve')
    image=image.resize((16,16),Image.Resampling.LANCZOS)
    ink=image.crop(image.getbbox())
    centered=Image.new('RGBA',(16,16))
    centered.alpha_composite(ink,((16-ink.width)//2,(16-ink.height)//2))
    return ImageTk.PhotoImage(centered,master=master)


def configure_theme(root):
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=BG, foreground=TEXT, font=('Malgun Gothic', 10),
                    bordercolor='#35455a', lightcolor=PANEL, darkcolor=PANEL,
                    troughcolor=BG, selectbackground='#345a62', selectforeground=TEXT)
    style.configure('TLabel', background=BG)
    style.configure('TLabelframe', background=BG)
    style.configure('TLabelframe.Label', foreground=ACCENT, font=('Malgun Gothic', 10, 'bold'))
    for name in ('TEntry', 'TCombobox', 'TSpinbox'):
        style.configure(name, fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT,
                        padding=5, arrowcolor=MUTED)
        style.map(name, fieldbackground=[('readonly', PANEL), ('disabled', BG)],
                  foreground=[('disabled', '#8390a5'), ('readonly', TEXT)])
    style.configure('TButton', padding=(10, 6), background=PANEL)
    style.map('TButton', background=[('active', '#35455a')], foreground=[('disabled', '#8390a5')])
    style.map('TCheckbutton', background=[('active', BG)], foreground=[('disabled', '#8390a5')])
    style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    rowheight=30, borderwidth=0)
    style.configure('Treeview.Heading', background='#273448', foreground=TEXT,
                    font=('Malgun Gothic', 10, 'bold'), padding=(8, 7), relief='flat')
    style.map('Treeview', background=[('selected', '#345a62')], foreground=[('selected', TEXT)])
    style.map('Treeview.Heading', background=[('active', '#35455a')])
    style.configure('TScrollbar', background=PANEL, arrowcolor=MUTED, borderwidth=0)
    style.configure('TScale', background=PANEL)
    style.configure('TProgressbar', background=ACCENT)
    root.option_add('*TCombobox*Listbox.background', PANEL)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', '#345a62')
    root.option_add('*TCombobox*Listbox.selectForeground', TEXT)


class ControlHint:
    """Explain compact controls on hover or keyboard focus without taking focus."""
    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.pending = None
        self.window = None
        widget.bind('<Enter>', self.schedule, add='+')
        widget.bind('<FocusIn>', self.schedule, add='+')
        for event in ('<Leave>', '<FocusOut>', '<ButtonPress>', '<Unmap>', '<Destroy>'):
            widget.bind(event, self.hide, add='+')

    def schedule(self, _=None):
        self.hide()
        self.pending = self.widget.after(500, self.show)

    def show(self):
        self.pending = None
        if not self.widget.winfo_viewable():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-topmost', bool(self.widget.winfo_toplevel().attributes('-topmost')))
        tk.Label(self.window, text=self.text, bg=PANEL, fg=TEXT, padx=10, pady=7,
                 font=('Malgun Gothic', 9), justify='left', wraplength=320).pack()
        self.window.update_idletasks()
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() - self.window.winfo_reqheight() - 5
        if y < 0:
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.window.geometry(f'{x:+d}{y:+d}')
        self.window.deiconify()

    def hide(self, _=None):
        if self.pending:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.window:
            self.window.destroy()
            self.window = None
