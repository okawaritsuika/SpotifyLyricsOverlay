"""Shared desktop colors and readable native controls."""
import tkinter as tk
from tkinter import ttk
from PIL import Image,ImageDraw,ImageTk

BG, PANEL, TEXT, MUTED, ACCENT = '#f2f3f7', '#ffffff', '#293044', '#5c6478', '#b81761'
BORDER, HOVER, SELECTED = '#dce0e9', '#f5edf3', '#fce1ef'


def transport_icon(master, kind, color=TEXT):
    """Draw playback symbols with consistent stroke weight and alignment."""
    scale=3
    image=Image.new('RGBA',(16*scale,16*scale))
    draw=ImageDraw.Draw(image)

    def points(vertices):
        return [(round(x*scale),round(y*scale)) for x,y in vertices]

    if kind=='play':
        draw.polygon(points(((4,2),(13,8),(4,14))),fill=color)
    elif kind=='pause':
        for x in (4,9):
            draw.rounded_rectangle((x*scale,3*scale,(x+2.5)*scale,13*scale),radius=2,fill=color)
    elif kind in ('volume','volume_muted'):
        draw.polygon(points(((1,6),(4,6),(8,3),(8,13),(4,10),(1,10))),fill=color)
        if kind=='volume_muted':
            draw.line(points(((10,6),(14,10))),fill=color,width=4)
            draw.line(points(((10,10),(14,6))),fill=color,width=4)
        else:
            for radius in (3.5,6.5):
                draw.arc(tuple(round(value*scale) for value in (7-radius,8-radius,7+radius,8+radius)),
                         -53,53,fill=color,width=4)
    else:
        for start in ((2,8) if kind in ('previous','next') else (5,)):
            vertices=[(start,3.5),(start+4.5,8),(start,12.5)]
            if kind in ('previous','first'):vertices=[(16-x,y) for x,y in vertices]
            draw.line(points(vertices),fill=color,width=5,joint='curve')
    image=image.resize((16,16),Image.Resampling.LANCZOS)
    ink=image.crop(image.getbbox())
    centered=Image.new('RGBA',(16,16))
    centered.alpha_composite(ink,((16-ink.width)//2,(16-ink.height)//2))
    return ImageTk.PhotoImage(centered,master=master)


def configure_theme(root):
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=BG, foreground=TEXT, font=('Malgun Gothic', 10),
                    bordercolor=BORDER, lightcolor=PANEL, darkcolor=PANEL,
                    troughcolor=BG, selectbackground=SELECTED, selectforeground=TEXT)
    style.configure('TLabel', background=BG)
    style.configure('TLabelframe', background=BG)
    style.configure('TLabelframe.Label', foreground=ACCENT, font=('Malgun Gothic', 10, 'bold'))
    for name in ('TEntry', 'TCombobox', 'TSpinbox'):
        style.configure(name, fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT,
                        padding=5, arrowcolor=MUTED)
        style.map(name, fieldbackground=[('readonly', PANEL), ('disabled', BG)],
                  foreground=[('disabled', '#8390a5'), ('readonly', TEXT)])
    style.configure('TButton', padding=(10, 6), background=PANEL)
    style.map('TButton', background=[('active', HOVER)], foreground=[('disabled', '#737c90')])
    style.map('TCheckbutton', background=[('active', BG)], foreground=[('disabled', '#8390a5')])
    style.configure('TNotebook', background=BG, bordercolor=BORDER, tabmargins=(0,0,0,3))
    style.configure('TNotebook.Tab', padding=(13,7), background=PANEL)
    style.map('TNotebook.Tab', background=[('selected', SELECTED), ('active', HOVER)],
              foreground=[('selected', ACCENT)])
    style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    rowheight=30, borderwidth=0)
    style.configure('Treeview.Heading', background=BG, foreground=TEXT,
                    font=('Malgun Gothic', 10, 'bold'), padding=(8, 7), relief='flat')
    style.map('Treeview', background=[('selected', SELECTED)], foreground=[('selected', TEXT)])
    style.map('Treeview.Heading', background=[('active', HOVER)])
    style.configure('TScrollbar', background=PANEL, arrowcolor=MUTED, borderwidth=0)
    style.configure('TScale', background=PANEL)
    style.configure('TProgressbar', background=ACCENT)
    root.option_add('*TCombobox*Listbox.background', PANEL)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', SELECTED)
    root.option_add('*TCombobox*Listbox.selectForeground', TEXT)
    root.option_add('*Menu.background', PANEL)
    root.option_add('*Menu.foreground', TEXT)
    root.option_add('*Menu.activeBackground', SELECTED)
    root.option_add('*Menu.activeForeground', TEXT)
    root.option_add('*Menu.selectColor', ACCENT)


class ControlHint:
    """Explain compact controls on hover or keyboard focus without taking focus."""
    def __init__(self, widget, text, *, background=PANEL, foreground=TEXT):
        self.widget, self.text = widget, text
        self.background, self.foreground = background, foreground
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
        tk.Label(self.window, text=self.text, bg=self.background, fg=self.foreground, padx=10, pady=7,
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
