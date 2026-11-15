"""Prepare grouped AI translation requests and receive copied replies."""
import queue
import threading
import tkinter as tk
from tkinter import ttk


class BatchTranslationDialog:
    def __init__(self, app):
        self.app = app
        self.closed = False
        self.batches = []
        self.progress = {}
        self._own_prompts = set()
        self._poll_id = None
        self._prepare_poll_id = None
        self._prepare_thread = None
        self.preparing = False
        self.window = tk.Toplevel(app.root)
        w = self.window
        w.title('번역 없는 곡 · AI 일괄 번역')
        w.geometry('820x760')
        w.minsize(640, 600)
        w.attributes('-topmost', False)
        w.columnconfigure(0, weight=1)
        w.rowconfigure(4, weight=1)
        self.help = ttk.Label(w, text='1. 지시문을 복사해 AI에게 요청하세요.  2. AI 답변 전체를 복사하면 번역이 채워집니다.\n저장된 원문에서 번역이 비어 있는 구절만 모읍니다. 원문이 없는 곡은 먼저 가사를 가져오세요.', wraplength=785)
        self.help.grid(row=0, column=0, sticky='ew', padx=12, pady=(10, 6))

        options = ttk.Frame(w)
        options.grid(row=1, column=0, sticky='ew', padx=12, pady=3)
        ttk.Label(options, text='한 묶음에').pack(side='left')
        self.batch_size = tk.StringVar(w, value='5')
        self.size_input = ttk.Spinbox(options, from_=1, to=50, textvariable=self.batch_size, width=5)
        self.size_input.pack(side='left', padx=6)
        ttk.Label(options, text='곡 (1–50)').pack(side='left')
        self.prepare_button = app.button(options, '묶음 만들기 / 새로고침', lambda: self.invoke(self.prepare))
        self.prepare_button.pack(side='right')

        navigation = ttk.Frame(w)
        navigation.grid(row=2, column=0, sticky='ew', padx=12, pady=4)
        navigation.columnconfigure(1, weight=1)
        self.previous_button = app.button(navigation, '이전', lambda: self.move(-1))
        self.previous_button.grid(row=0, column=0, padx=(0, 6))
        self.batch_choice = ttk.Combobox(navigation, state='readonly')
        self.batch_choice.grid(row=0, column=1, sticky='ew')
        self.batch_choice.bind('<<ComboboxSelected>>', lambda event: self.show_batch())
        self.next_button = app.button(navigation, '다음', lambda: self.move(1))
        self.next_button.grid(row=0, column=2, padx=(6, 0))

        songs_frame = ttk.Frame(w)
        songs_frame.grid(row=3, column=0, sticky='ew', padx=12, pady=4)
        songs_frame.columnconfigure(0, weight=1)
        self.songs = ttk.Treeview(songs_frame, columns=('title', 'artist', 'missing'), show='headings', height=3)
        for key, label, width in (('title', '곡 제목', 270), ('artist', '아티스트', 200), ('missing', '요청 구절', 85)):
            self.songs.heading(key, text=label)
            self.songs.column(key, width=width, minwidth=65, stretch=key != 'missing')
        self.songs.grid(row=0, column=0, sticky='ew')
        scroll = ttk.Scrollbar(songs_frame, command=self.songs.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.songs.configure(yscrollcommand=scroll.set)

        self.notebook = ttk.Notebook(w)
        self.notebook.grid(row=4, column=0, sticky='nsew', padx=12, pady=4)
        self.prompt_frame, self.reply_frame = ttk.Frame(self.notebook), ttk.Frame(self.notebook)
        self.prompt = self.text_tab(self.prompt_frame, 'AI 지시문')
        self.prompt.configure(state='disabled')
        self.reply = self.text_tab(self.reply_frame, '답변 직접 붙여넣기')
        self.auto_apply = tk.BooleanVar(w, value=True)
        self.auto_button = ttk.Checkbutton(w, text='이 창이 열려 있을 때 복사한 AI 답변 자동 적용', variable=self.auto_apply, command=self.toggle_auto)
        self.auto_button.grid(row=5, column=0, sticky='w', padx=12, pady=4)
        result_frame = ttk.LabelFrame(w, text='번역 저장 확인', padding=(8, 4))
        result_frame.grid(row=6, column=0, sticky='ew', padx=12, pady=4)
        self.result_status = tk.StringVar(w, value='아직 이 창에서 처리한 답변이 없습니다.')
        self.result_label = ttk.Label(result_frame, textvariable=self.result_status, wraplength=765)
        self.result_label.pack(fill='x')
        self.saved_status = tk.StringVar(w)
        self.saved_label = ttk.Label(result_frame, textvariable=self.saved_status, wraplength=765)
        self.saved_label.pack(fill='x', pady=(3, 0))
        self.update_saved_summary(self.app.batch_translations.summary())
        self.status = tk.StringVar(w)
        self.status_label = ttk.Label(w, textvariable=self.status, wraplength=785)
        self.status_label.grid(row=7, column=0, sticky='ew', padx=12, pady=4)

        footer = ttk.Frame(w)
        footer.grid(row=8, column=0, sticky='ew', padx=9, pady=(4, 10))
        self.buttons = []
        for column, (label, action) in enumerate((('지시문 복사', self.copy_prompt), ('답변 붙여넣기', self.paste_reply), ('답변 적용', self.apply_manual), ('닫기', self.close))):
            footer.columnconfigure(column, weight=1)
            button = app.button(footer, label, lambda action=action: self.invoke(action))
            button.grid(row=0, column=column, sticky='ew', padx=3)
            self.buttons.append(button)
        self.copy_button = self.buttons[0]
        self._clipboard_seen = self.read_clipboard()
        w.bind('<Configure>', self.resize)
        w.bind('<Destroy>', self.destroyed)
        w.protocol('WM_DELETE_WINDOW', self.close)
        # This utility window is deliberately not a hotkey target. The overlay
        # continues accepting its shortcuts when the user returns to it.
        self.update_choices(0)
        self.show_batch()
        self.invoke(self.prepare)
        self._poll_id = w.after(800, self.poll_clipboard)

    def text_tab(self, frame, title):
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.notebook.add(frame, text=title)
        text = tk.Text(frame, wrap='word', font=('Malgun Gothic', 10), height=5, undo=True)
        text.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        text.configure(yscrollcommand=scroll.set)
        text.bind('<Tab>', lambda event: self.focus_next(text))
        text.bind('<Shift-Tab>', lambda event: self.focus_next(text, previous=True))
        return text

    @staticmethod
    def focus_next(widget, previous=False):
        target = widget.tk_focusPrev() if previous else widget.tk_focusNext()
        target.focus_set()
        return 'break'

    def resize(self, event):
        if event.widget is self.window:
            for label in (self.help, self.status_label):
                label.configure(wraplength=max(300, event.width - 28))
            for label in (self.result_label, self.saved_label):
                label.configure(wraplength=max(300, event.width - 48))

    def update_saved_summary(self, result):
        self.saved_status.set(f'AI 번역 누적 저장 · {result["total_songs"]:,}곡 · {result["total_lines"]:,}구절')

    def invoke(self, action):
        try:
            action()
        except (ValueError, OSError, tk.TclError) as error:
            self.status.set(str(error))

    def prepare(self):
        if self.preparing:
            return
        try:
            count = int(self.batch_size.get())
        except ValueError:
            raise ValueError('한 묶음의 곡 수를 1부터 50까지 입력하세요.') from None
        if not 1 <= count <= 50:
            raise ValueError('한 묶음의 곡 수를 1부터 50까지 입력하세요.')
        self.status.set('보관한 가사를 모아 번역이 없는 구절을 확인하고 있습니다…')
        self.window.configure(cursor='wait')
        self.prepare_button.configure(state='disabled')
        self.size_input.configure(state='disabled')
        self.preparing = True
        results = queue.Queue(maxsize=1)
        app = self.app

        def build():
            # Both operations use stored documents only. Never access a Tk
            # variable or schedule a Tk callback from the worker thread.
            try:
                documents = app.collect_translation_documents()
                batches = app.batch_translations.prepare(documents, songs_per_batch=count)
                results.put((True, batches))
            except Exception as error:
                results.put((False, str(error)))

        self._prepare_thread = threading.Thread(target=build, daemon=True, name='translation-batches')
        self._prepare_thread.start()
        self._prepare_poll_id = self.window.after(60, lambda: self.prepared(results))

    def prepared(self, results):
        self._prepare_poll_id = None
        if self.closed:
            return
        try:
            success, result = results.get_nowait()
        except queue.Empty:
            self._prepare_poll_id = self.window.after(60, lambda: self.prepared(results))
            return
        self.preparing = False
        self.window.configure(cursor='')
        self.prepare_button.configure(state='normal')
        self.size_input.configure(state='normal')
        if not success:
            self.status.set(f'묶음을 만들지 못했습니다: {result}')
            return
        batches = result
        self.batches = batches
        self._own_prompts.update(batch['prompt'] for batch in batches)
        self.update_choices(0)
        self.show_batch()
        songs = sum(len(batch['songs']) for batch in batches)
        missing = sum(self.missing_lines(song) for batch in batches for song in batch['songs'])
        self.status.set(f'번역할 {songs}곡 · {missing}구절을 {len(batches)}묶음으로 나눴습니다. 지시문을 복사하세요.' if batches else '저장된 가사에서 번역이 비어 있는 구절을 찾지 못했습니다. 새 가사를 가져온 뒤 새로고침하세요.')

    @staticmethod
    def missing_lines(song):
        return sum(bool(line.original.strip()) and not line.korean.strip() for line in song['lines'])

    def update_choices(self, selected):
        labels = []
        for index, batch in enumerate(self.batches):
            remaining = self.progress.get(batch['batch_id'])
            state = '완료' if remaining == 0 else f'{remaining}구절 남음' if remaining is not None else '대기'
            labels.append(f'{index + 1} / {len(self.batches)} 묶음 · {len(batch["songs"])}곡 · {len(batch["prompt"]):,}자 · {state}')
        self.batch_choice.configure(values=labels)
        if labels:
            self.batch_choice.current(max(0, min(selected, len(labels) - 1)))
        else:
            self.batch_choice.set('번역할 가사 없음')

    def show_batch(self):
        index = self.batch_choice.current()
        batch = self.batches[index] if 0 <= index < len(self.batches) else None
        self.songs.delete(*self.songs.get_children())
        self.prompt.configure(state='normal')
        self.prompt.delete('1.0', 'end')
        if batch:
            self.prompt.insert('1.0', batch['prompt'])
            for song in batch['songs']:
                self.songs.insert('', 'end', values=(song['title'], song['artist'], self.missing_lines(song)))
        self.prompt.configure(state='disabled')
        self.copy_button.configure(state='normal' if batch else 'disabled')
        self.previous_button.configure(state='normal' if index > 0 else 'disabled')
        self.next_button.configure(state='normal' if batch and index + 1 < len(self.batches) else 'disabled')

    def move(self, offset):
        index = self.batch_choice.current() + offset
        if 0 <= index < len(self.batches):
            self.batch_choice.current(index)
            self.show_batch()

    def copy_prompt(self):
        index = self.batch_choice.current()
        if not 0 <= index < len(self.batches):
            return
        prompt = self.batches[index]['prompt']
        self.window.clipboard_clear()
        self.window.clipboard_append(prompt)
        self._clipboard_seen = prompt
        self.status.set(f'{index + 1}번째 묶음 지시문을 복사했습니다. AI에게 붙여넣고 답변 전체를 복사하세요.')

    def read_clipboard(self):
        try:
            return self.window.clipboard_get()
        except tk.TclError:
            return None

    def toggle_auto(self):
        self._clipboard_seen = self.read_clipboard()
        self.status.set('새로 복사한 AI 답변을 자동 적용합니다.' if self.auto_apply.get() else '자동 적용을 껐습니다. 답변을 직접 붙여넣은 뒤 적용하세요.')

    def poll_clipboard(self):
        self._poll_id = None
        if self.closed:
            return
        try:
            if self.auto_apply.get() and not self.preparing:
                text = self.read_clipboard()
                if text is not None and text != self._clipboard_seen:
                    self._clipboard_seen = text
                    if text not in self._own_prompts and self.app.batch_translations.is_reply(text):
                        self.apply_reply(text, automatic=True)
        finally:
            if not self.closed:
                self._poll_id = self.window.after(800, self.poll_clipboard)

    def paste_reply(self):
        text = self.read_clipboard()
        if text is None:
            raise ValueError('클립보드에 텍스트가 없습니다. AI 답변 전체를 다시 복사하세요.')
        self.reply.delete('1.0', 'end')
        self.reply.insert('1.0', text)
        self.notebook.select(self.reply_frame)
        self.reply.focus_set()

    def apply_manual(self):
        self.apply_reply(self.reply.get('1.0', 'end-1c'))

    def apply_reply(self, text, automatic=False):
        if self.preparing:
            self.status.set('가사 묶음을 준비하고 있습니다. 완료된 뒤 답변 적용을 눌러 주세요.')
            return
        try:
            result = self.app.apply_translation_reply(text)
        except (ValueError, OSError, tk.TclError) as error:
            if automatic:
                self.reply.delete('1.0', 'end')
                self.reply.insert('1.0', text)
            self.result_status.set('답변 적용 실패 · 이번 답변은 저장하지 않았습니다.')
            self.status.set(f'답변을 적용하지 못했습니다: {error}\n답변 직접 붙여넣기 탭에서 확인한 뒤 다시 적용하세요.')
            return
        self._clipboard_seen = text
        batch_id = result.get('batch_id')
        if batch_id is not None:
            self.progress[batch_id] = result['remaining']
            self.update_choices(self.batch_choice.current())
        mode = '자동 적용' if automatic else '직접 적용'
        changed_latin = result.get('changed_latin_lines', 0)
        if result['lines']:
            outcome = f'새 번역 저장 완료 · {result["songs"]}곡 · {result["lines"]}구절'
        elif changed_latin:
            outcome = '영어 원문 확인 필요'
        elif result['preserved_lines']:
            outcome = f'기존 번역 유지 · {result["received_songs"]}곡 · {result["received_lines"]}구절 확인'
        else:
            outcome = f'이미 반영된 답변 · {result["received_songs"]}곡 · {result["received_lines"]}구절 확인'
        self.result_status.set(f'{mode} · {outcome}\n이미 반영 {result["already_lines"]}구절 · 기존 번역 유지 {result["preserved_lines"]}구절 · 이 묶음 남음 {result["remaining"]}구절')
        self.update_saved_summary(result)
        if changed_latin:
            self.status.set(f'영어 표현이 바뀐 {changed_latin}구절은 보류했습니다. 새로고침해 남은 구절을 다시 요청하세요.')
        else:
            self.status.set('다음 묶음의 지시문을 복사하거나 새로고침해 남은 번역을 모으세요.')

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        if self._poll_id is not None:
            self.window.after_cancel(self._poll_id)
            self._poll_id = None
        if self._prepare_poll_id is not None:
            self.window.after_cancel(self._prepare_poll_id)
            self._prepare_poll_id = None
        if self.app.translation_dialog is self:
            self.app.translation_dialog = None

    def close(self):
        if self.closed:
            return
        self.cleanup()
        self.window.destroy()

    def destroyed(self, event):
        if event.widget is self.window:
            self.cleanup()
