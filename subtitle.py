"""Outlined, wrapping text that remains legible on a color-keyed background."""
import tkinter as tk


class SubtitleLine(tk.Canvas):
    def __init__(self, parent, *, bg, fg, font, **options):
        self.text_options = {'text': '', 'fg': fg, 'font': font, 'wraplength': 900,
                             'justify':options.pop('justify','center')}
        self.last_width = 0
        super().__init__(parent, bg=bg, height=1, width=1, borderwidth=0,
                         highlightthickness=0, **options)
        self.bind('<Configure>', self._resize)

    def configure(self, cnf=None, **options):
        if cnf:
            options = {**cnf, **options}
        changed = False
        for name in tuple(options):
            if name in self.text_options:
                value=options.pop(name)
                if self.text_options[name]!=value:
                    self.text_options[name]=value
                    changed=True
        result = super().configure(**options) if options else None
        if changed:
            self._draw()
        return result

    config = configure

    def cget(self, key):
        return self.text_options[key] if key in self.text_options else super().cget(key)

    def _resize(self, event):
        if event.width != self.last_width:
            self.last_width = event.width
            self._draw()

    def _draw(self):
        text = self.text_options['text']
        if not text:
            self.itemconfigure('lyric',state='hidden')
            super().configure(height=1)
            return
        width = max(80, min(self.text_options['wraplength'], self.winfo_width()-8))
        if self.winfo_width() < 80:
            width = self.text_options['wraplength']
        center = max(width+8, self.winfo_width()) / 2
        justify=self.text_options['justify']
        anchor='n'
        if justify=='left':center=4;anchor='nw'
        elif justify=='right':center=max(width+8,self.winfo_width())-4;anchor='ne'
        common = dict(text=text, font=self.text_options['font'], width=width,
                      anchor=anchor, justify=justify, tags='lyric',state='normal')
        offsets=[(-2,0),(2,0),(0,-2),(0,2),(-1,-1),(1,-1),(-1,1),(1,1),(0,0)]
        items=self.find_withtag('lyric')
        for index,(dx,dy) in enumerate(offsets):
            fill=self.text_options['fg'] if index==8 else '#090b10'
            if index<len(items):
                self.coords(items[index],center+dx,3+dy)
                self.itemconfigure(items[index],fill=fill,**common)
            else:self.create_text(center+dx,3+dy,fill=fill,**common)
        box = self.bbox('lyric')
        super().configure(height=max(1, box[3]+3) if box else 1)
