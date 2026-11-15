"""Compact, keyboard accessible catalog rows with individual performer colors."""
import math
import tkinter as tk
from tkinter import font, ttk

from explorer_symbols import series_symbol


BG, PANEL, TEXT, MUTED, ACCENT = '#f7f6f8', '#ffffff', '#27232c', '#605765', '#a82359'
BORDER, HOVER, SELECTED = '#ddd6df', '#f4eff4', '#fbe4ee'


class CatalogList(tk.Frame):
    """A selectable, vertically scrolling list that draws only visible rows.

    Each performer preview keeps its own name, CV and color. The neutral row
    selection never implies that a group has one character's personal color.
    """
    row_height = 92

    def __init__(self, parent, columns, empty_text='검색 결과가 없습니다.'):
        super().__init__(parent, bg=BG, highlightthickness=1, highlightbackground=BORDER)
        self.columns = tuple(columns)
        self.empty_text = empty_text
        self._rows, self._order = {}, []
        self._selected = None
        self._hover = None
        self._draw_after = None
        self._link_commands = {}
        self._link_click = False
        self.canvas = tk.Canvas(self, bg=PANEL, highlightthickness=0, takefocus=True,
                                cursor='arrow', yscrollincrement=1)
        self.scrollbar = ttk.Scrollbar(self, orient='vertical', command=self.yview, style='Explorer.Vertical.TScrollbar')
        self.scrollbar.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.configure(yscrollcommand=self._scrolled)
        self.title_font = font.Font(self, family='Malgun Gothic', size=11, weight='bold')
        self.body_font = font.Font(self, family='Malgun Gothic', size=9)
        self.small_font = font.Font(self, family='Malgun Gothic', size=9)
        self.title_link_font = self.title_font.copy();self.title_link_font.configure(underline=True)
        self.body_link_font = self.body_font.copy();self.body_link_font.configure(underline=True)
        # Let consumers bind activation and selection on the list itself.
        tags = self.canvas.bindtags()
        self.canvas.bindtags((tags[0], str(self), *tags[1:]))
        self.canvas.bind('<Configure>', lambda _: self._schedule_draw())
        self.canvas.bind('<Button-1>', self._clicked)
        self.canvas.bind('<Double-1>', self._double_clicked)
        self.canvas.bind('<Motion>', self._moved)
        self.canvas.bind('<Leave>', self._left)
        self.canvas.bind('<MouseWheel>', self._wheel)
        self.canvas.bind('<Button-4>', lambda _: self._scroll(-1))
        self.canvas.bind('<Button-5>', lambda _: self._scroll(1))
        for key in ('Up', 'Down', 'Home', 'End', 'Prior', 'Next'):
            self.canvas.bind('<'+key+'>', self._key)
        self.canvas.bind('<Return>', self._activate)
        self.canvas.bind('<FocusIn>', lambda _: self._schedule_draw())
        self.canvas.bind('<FocusOut>', lambda _: self._schedule_draw())
        self.bind('<Destroy>', self._destroyed, add='+')

    def insert(self, parent, index, iid, values, tags=(), *, subtitle='', preview=(), badge='', series=(),
               title_command=None):
        if iid in self._rows:raise ValueError('Duplicate catalog row: '+iid)
        self._rows[iid] = {'values': tuple(values), 'tags': tuple(tags), 'subtitle': subtitle,
                           'preview': tuple(dict(person) for person in preview), 'badge': badge,
                           'series': tuple(dict.fromkeys(series)), 'title_command': title_command}
        if index == 'end':self._order.append(iid)
        else:self._order.insert(int(index), iid)
        self._schedule_draw()
        return iid

    def delete(self, *identities):
        removed = set(identities)
        for identity in removed:self._rows.pop(identity, None)
        self._order = [identity for identity in self._order if identity not in removed]
        if self._selected in removed:self._selected = None
        if not self._order:self.canvas.yview_moveto(0)
        self._hover = None
        self._link_commands.clear();self.canvas.configure(cursor='arrow')
        self._schedule_draw()

    def get_children(self, _=''):
        return tuple(self._order)

    def exists(self, identity):
        return identity in self._rows

    def item(self, identity, option=None):
        row = self._rows[identity]
        return row.get(option) if option else dict(row)

    def set(self, identity, column):
        return self._rows[identity]['values'][self.columns.index(column)]

    def selection(self):
        return (self._selected,) if self._selected in self._rows else ()

    def selection_set(self, identity):
        if isinstance(identity, (tuple, list)):identity = identity[0] if identity else None
        if identity not in self._rows:return
        if self._selected != identity:
            self._selected = identity
            self._schedule_draw()
            self.event_generate('<<TreeviewSelect>>', when='tail')

    def see(self, identity):
        if identity not in self._rows:return
        height = max(1, self.canvas.winfo_height())
        self.canvas.configure(scrollregion=(0,0,self.canvas.winfo_width(),
            max(height, len(self._order)*self.row_height)))
        top = self._order.index(identity) * self.row_height
        viewport = self.canvas.canvasy(0)
        if top < viewport or height < self.row_height:self._move_to(top)
        elif top+self.row_height > viewport+height:self._move_to(top+self.row_height-height)

    def focus_set(self):
        self.canvas.focus_set()

    def yview(self, *args):
        if len(args)==3 and args[0]=='scroll' and args[2]=='units':
            args = ('scroll', int(args[1])*self.row_height, 'units')
        result = self.canvas.yview(*args)
        if args:self._schedule_draw()
        return result

    def _move_to(self, top):
        total = max(1, len(self._order)*self.row_height)
        self.canvas.yview_moveto(max(0, top)/total)
        self._schedule_draw()

    def _scrolled(self, first, last):
        self.scrollbar.set(first, last)
        self._schedule_draw()

    def _schedule_draw(self):
        if self._draw_after is None:self._draw_after = self.after_idle(self._draw)

    def _destroyed(self, event):
        if event.widget is self and self._draw_after is not None:
            self.after_cancel(self._draw_after);self._draw_after = None

    def _identity_at(self, y):
        position = int(self.canvas.canvasy(y)//self.row_height)
        return self._order[position] if 0 <= position < len(self._order) else None

    def _clicked(self, event):
        self.canvas.focus_set()
        command = self._command_at(event.x, event.y)
        self._link_click = command is not None
        if command:
            command()
            return 'break'
        identity = self._identity_at(event.y)
        if identity:self.selection_set(identity)

    def _command_at(self, x, y):
        x, y = self.canvas.canvasx(x), self.canvas.canvasy(y)
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            if item in self._link_commands:return self._link_commands[item]
        return None

    def _double_clicked(self, event):
        # A link can replace the rows on the first click; the second click must
        # still not reach the containing list's playback binding.
        if self._link_click or self._command_at(event.x, event.y):return 'break'

    def _moved(self, event):
        self.canvas.configure(cursor='hand2' if self._command_at(event.x, event.y) else 'arrow')
        identity = self._identity_at(event.y)
        if identity != self._hover:self._hover = identity;self._schedule_draw()

    def _left(self, _):
        self.canvas.configure(cursor='arrow')
        if self._hover is not None:self._hover = None;self._schedule_draw()

    def _scroll(self, steps):
        self.canvas.yview_scroll(steps*self.row_height, 'units');self._schedule_draw()
        return 'break'

    def _wheel(self, event):
        return self._scroll(-int(event.delta/120) or (-1 if event.delta > 0 else 1))

    def _key(self, event):
        if not self._order:return 'break'
        index = self._order.index(self._selected) if self._selected in self._rows else 0
        page = max(1, self.canvas.winfo_height()//self.row_height)
        if event.keysym == 'Home':index = 0
        elif event.keysym == 'End':index = len(self._order)-1
        else:index += {'Up':-1, 'Down':1, 'Prior':-page, 'Next':page}[event.keysym]
        identity = self._order[max(0, min(len(self._order)-1, index))]
        self.selection_set(identity);self.see(identity)
        return 'break'

    def _activate(self, _):
        if self.selection():self.event_generate('<<ListActivate>>', when='tail')
        return 'break'

    @staticmethod
    def _fit(value, width, face):
        value = str(value).replace('\n', ' ')
        if face.measure(value) <= width:return value
        left, right = 0, len(value)
        while left < right:
            middle = (left+right+1)//2
            if face.measure(value[:middle]+'…') <= width:left = middle
            else:right = middle-1
        return value[:left]+'…' if width >= face.measure('…') else ''

    def _text(self, x, y, value, width, face, color=TEXT, extra_tags=(), command=None):
        item = self.canvas.create_text(x, y, text=self._fit(value, max(0, width), face),
            font=face, fill=color, anchor='nw', tags=('rows',)+extra_tags)
        if callable(command):
            self._link_commands[item] = command
            self.canvas.addtag_withtag('name_link', item)
        return item

    def _draw(self):
        self._draw_after = None
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        region = (0, 0, width, max(height, len(self._order)*self.row_height))
        if str(self.canvas.cget('scrollregion')) != ' '.join(str(value) for value in region):
            self.canvas.configure(scrollregion=region)
        self.canvas.delete('rows')
        self._link_commands.clear()
        if not self._order:
            self._text(18, 22, self.empty_text, width-36, self.body_font, MUTED)
            return
        top = self.canvas.canvasy(0)
        start = max(0, int(top//self.row_height)-1)
        end = min(len(self._order), math.ceil((top+height)/self.row_height)+1)
        for index in range(start, end):
            identity = self._order[index];row = self._rows[identity]
            y = index*self.row_height
            selected = identity == self._selected
            background = SELECTED if selected else HOVER if identity == self._hover else PANEL
            self.canvas.create_rectangle(0, y, width, y+self.row_height, fill=background,
                                         outline='', tags='rows')
            if selected:
                self.canvas.create_rectangle(0, y+9, 3, y+self.row_height-9,
                                             fill=ACCENT, outline='', tags='rows')
            badge = row['badge']
            badge_width = self.small_font.measure(badge)+22 if badge else 0
            title_x = 16
            for key in row['series']:
                self.canvas.create_image(title_x, y+10, image=series_symbol(self, key, 20),
                                         anchor='nw', tags=('rows', 'series_symbol'))
                title_x += 24
            title_command = row['title_command']
            self._text(title_x, y+9, row['values'][0], width-title_x-18-badge_width,
                       self.title_link_font if callable(title_command) else self.title_font,
                       extra_tags=('row_title',), command=title_command)
            if badge:
                color = ACCENT if 'Spotify' in badge else '#8b471f' if '제한' in badge else MUTED
                self._text(width-badge_width, y+11, badge, badge_width-10, self.small_font, color)
            self._text(16, y+33, row['subtitle'], width-32, self.small_font, MUTED)
            people = row['preview']
            if not people:
                self._text(16, y+58, '캐릭터 · CV 자료 미확인', width-32, self.body_font, MUTED)
            else:
                shown = min(3 if width >= 680 else 2, len(people))
                remaining = len(people)-shown
                more_width = min(110, 28+min(6, remaining)*9) if remaining else 0
                cell = max(40, (width-32-more_width)/shown)
                for number, person in enumerate(people[:shown]):
                    x = 16+number*cell
                    dot = person.get('color') or MUTED
                    color = person.get('text_color') or MUTED
                    self.canvas.create_oval(x, y+58, x+7, y+65, fill=dot, outline='',
                                             tags=('rows', 'performer_color'))
                    command = person.get('command')
                    self._text(x+13, y+53, person['name'], cell-23,
                               self.body_link_font if callable(command) else self.body_font, color,
                               ('performer_name',), command=command)
                    if not person.get('hide_voice') and (person.get('voice') or person.get('kind') == 'idol'):
                        self._text(x+13, y+72, 'CV. '+(person.get('voice') or '미확인'),
                                   cell-23, self.small_font, color, ('performer_voice',))
                if remaining:
                    x = width-more_width-12
                    for number, person in enumerate(people[shown:shown+6]):
                        dot = person.get('color') or MUTED
                        self.canvas.create_oval(x+number*9, y+57, x+number*9+6, y+63,
                                                fill=dot, outline='', tags=('rows', 'performer_color'))
                    self._text(x, y+71, f'+{remaining}명', more_width, self.small_font, MUTED)
            self.canvas.create_line(14, y+self.row_height-1, width-14, y+self.row_height-1,
                                    fill=BORDER, tags='rows')
        if self.canvas.focus_get() is self.canvas:
            self.configure(highlightbackground=ACCENT)
        else:self.configure(highlightbackground=BORDER)
