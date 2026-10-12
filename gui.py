"""Windowed front end for export_playlists.py.

Lists your iTunes playlists, compares them with the playlists already on a USB
drive (in sync / differs / not on drive / only on drive), and exports the
ticked ones to one or more drives.

    pythonw gui.py
"""
import ctypes
import os
import queue
import shutil
import string
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import export_playlists as ex

CHECKED, UNCHECKED = '☑', '☐'
DRIVE_REMOVABLE = 2

# Pusher160 palette (push-mixxx/skins/push2/gen_skin.py)
INK = '#0B0B0B'
PANEL = '#121211'
LINE = '#E8E8E2'
DIM = '#8C8C86'
FAINT = '#5A5A55'
EDGE = '#3A3A36'
HAIR = '#2A2A28'
LIME = '#C8F53C'
ORANGE = '#E8A33A'
RED = '#FF2B1C'
BODY = 'Arial'


def apply_theme(root):
    """ttk styles in the Pusher160 look: black, hairline outlines, lime accent."""
    families = set(tkfont.families(root))
    display = 'TAN - SPRING' if 'TAN - SPRING' in families else BODY
    root.configure(bg=INK)
    root.option_add('*TCombobox*Listbox.background', PANEL)
    root.option_add('*TCombobox*Listbox.foreground', LINE)
    root.option_add('*TCombobox*Listbox.selectBackground', LIME)
    root.option_add('*TCombobox*Listbox.selectForeground', INK)
    root.option_add('*TCombobox*Listbox.font', (BODY, 10))
    s = ttk.Style(root)
    s.theme_use('clam')
    s.configure('.', background=INK, foreground=LINE, font=(BODY, 10), bordercolor=EDGE,
                darkcolor=INK, lightcolor=INK, troughcolor=INK, focuscolor=LIME,
                selectbackground=LIME, selectforeground=INK, insertcolor=LINE)
    s.configure('TFrame', background=INK)
    s.configure('TLabel', background=INK, foreground=LINE)
    s.configure('Dim.TLabel', foreground=DIM)
    s.configure('Section.TLabel', foreground=DIM, font=(BODY, 9, 'bold'))
    s.configure('Title.TLabel', foreground=LIME, font=(display, 22))
    s.configure('TButton', background=INK, foreground=DIM, bordercolor=EDGE, relief='solid',
                borderwidth=1, padding=(12, 5), font=(BODY, 9, 'bold'))
    s.map('TButton', foreground=[('pressed', INK), ('active', LINE), ('disabled', FAINT)],
          background=[('pressed', LIME), ('active', INK)],
          bordercolor=[('pressed', LIME), ('active', LINE)])
    s.configure('Accent.TButton', foreground=LIME, bordercolor=LIME, font=(display, 13), padding=(18, 3))
    s.map('Accent.TButton', foreground=[('pressed', INK), ('disabled', FAINT), ('active', INK)],
          background=[('pressed', LIME), ('active', LIME)],
          bordercolor=[('disabled', EDGE), ('active', LIME)])
    s.configure('TCheckbutton', background=INK, foreground=LINE, indicatorbackground=INK,
                indicatorforeground=INK, indicatormargin=(0, 0, 8, 0), focuscolor=INK, upperbordercolor=EDGE,
                lowerbordercolor=EDGE)
    s.map('TCheckbutton', indicatorbackground=[('selected', LIME), ('active', HAIR)],
          background=[('active', INK)], foreground=[('active', LINE)])
    s.configure('TEntry', fieldbackground=INK, foreground=DIM, bordercolor=EDGE)
    s.map('TEntry', fieldbackground=[('readonly', INK)], foreground=[('readonly', DIM)])
    s.configure('TCombobox', fieldbackground=INK, background=INK, foreground=LINE, arrowcolor=DIM,
                bordercolor=EDGE, padding=4)
    s.map('TCombobox', fieldbackground=[('readonly', INK)], foreground=[('readonly', LINE)],
          bordercolor=[('focus', LIME)], arrowcolor=[('active', LIME)])
    s.configure('Treeview', background=INK, fieldbackground=INK, foreground=LINE, bordercolor=HAIR,
                rowheight=26, font=(BODY, 10))
    s.map('Treeview', background=[('selected', INK)], foreground=[('selected', LINE)])
    s.configure('Treeview.Heading', background=INK, foreground=DIM, bordercolor=HAIR, relief='flat',
                font=(BODY, 8, 'bold'), padding=(6, 6))
    s.map('Treeview.Heading', background=[('active', INK)], foreground=[('active', LINE)])
    s.configure('Horizontal.TProgressbar', background=LIME, troughcolor=PANEL, bordercolor=HAIR,
                thickness=4)
    s.configure('Vertical.TScrollbar', background=HAIR, troughcolor=INK, bordercolor=INK,
                arrowcolor=FAINT, gripcount=0, arrowsize=10)
    s.map('Vertical.TScrollbar', background=[('active', EDGE)])
    s.configure('TPanedwindow', background=INK)
    s.configure('Sash', sashthickness=10, background=INK, bordercolor=INK, lightcolor=INK, darkcolor=INK)
    return display


def dark_title_bar(root):
    """Windows 10/11: dark window frame (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    if os.name != 'nt':
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):  # 20 = Win10 2004+/Win11, 19 = older Win10 builds
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except (AttributeError, OSError):
        pass


def removable_drives():
    """[(root, label)] of removable drives with media in them (Windows)."""
    if os.name != 'nt':
        return []
    k32 = ctypes.windll.kernel32
    mask, out = k32.GetLogicalDrives(), []
    for i, letter in enumerate(string.ascii_uppercase):
        root = f'{letter}:\\'
        if not mask & (1 << i) or k32.GetDriveTypeW(root) != DRIVE_REMOVABLE:
            continue
        label = ctypes.create_unicode_buffer(261)
        if k32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0):
            out.append((root, label.value))
    return out


def size_text(n):
    return f'{n / 2**30:.1f} GB' if n >= 2**30 else f'{n >> 20} MB'


def short(path, n=34):
    return path if len(path) <= n else '…' + path[-(n - 1):]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('iTunes → USB exporter')
        self.geometry('980x640')
        self.minsize(820, 520)
        self.events = queue.Queue()
        self.playlists = []          # [ex.Playlist] from iTunes
        self.selected = set()        # playlist names ticked for export
        self.drives = {}             # root -> (label, BooleanVar export-to)
        self.library = tk.StringVar(value=ex.DEFAULT_LIBRARY)
        self.compare_with = tk.StringVar()
        self.remove_others = tk.BooleanVar(value=False)
        self.prune = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='')
        self.display_font = apply_theme(self)
        self._build()
        self.after(10, lambda: dark_title_bar(self))
        self.refresh_drives()
        self.load_library()
        self.after(100, self._pump)

    # ------------------------------------------------------------ layout ----
    def _build(self):
        pad = {'padx': 12, 'pady': 6}
        head = ttk.Frame(self)
        head.pack(fill='x', padx=12, pady=(10, 0))
        ttk.Label(head, text='iTunes → USB', style='Title.TLabel').pack(side='left')
        ttk.Label(head, text='PLAYLIST EXPORTER', style='Section.TLabel').pack(side='left', padx=12, pady=(10, 0))
        tk.Frame(self, height=1, bg=HAIR).pack(fill='x', padx=12, pady=(6, 0))

        top = ttk.Frame(self)
        top.pack(fill='x', **pad)
        ttk.Label(top, text='ITUNES LIBRARY', style='Section.TLabel').pack(side='left')
        ttk.Entry(top, textvariable=self.library, state='readonly').pack(side='left', fill='x', expand=True, padx=10)
        ttk.Button(top, text='CHANGE…', command=self.pick_library).pack(side='left')
        ttk.Button(top, text='RELOAD', command=self.load_library).pack(side='left', padx=(6, 0))

        body = ttk.PanedWindow(self, orient='horizontal')
        body.pack(fill='both', expand=True, **pad)
        # long folder paths would otherwise widen the right pane over the table
        self.after_idle(lambda: body.sashpos(0, max(self.winfo_width() - 300, 480)))

        left = ttk.Frame(body)
        cols = ('sel', 'name', 'songs', 'state', 'detail')
        self.tree = ttk.Treeview(left, columns=cols, show='headings', selectmode='none')
        for col, text, width, anchor in (('sel', '', 34, 'center'), ('name', 'PLAYLIST', 220, 'w'),
                                         ('songs', 'SONGS', 60, 'e'), ('state', 'ON DRIVE', 100, 'w'),
                                         ('detail', 'DIFFERENCES', 260, 'w')):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=width, anchor=anchor, stretch=col in ('name', 'detail'))
        self.tree.tag_configure('in sync', foreground=LIME)
        self.tree.tag_configure('differs', foreground=ORANGE)
        self.tree.tag_configure('only on drive', foreground=FAINT)
        self.tree.tag_configure('not on drive', foreground=LINE)
        self.tree.bind('<Button-1>', self._on_click)
        sb = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')
        body.add(left, weight=3)

        right = ttk.Frame(body, padding=(14, 0, 0, 0))
        ttk.Label(right, text='EXPORT TO', style='Section.TLabel').pack(anchor='w')
        self.drive_box = ttk.Frame(right)
        self.drive_box.pack(fill='x', pady=6)
        row = ttk.Frame(right)
        row.pack(fill='x')
        ttk.Button(row, text='REFRESH', command=self.refresh_drives).pack(side='left')
        ttk.Button(row, text='ADD FOLDER…', command=self.add_folder).pack(side='left', padx=6)

        ttk.Label(right, text='COMPARE WITH', style='Section.TLabel').pack(anchor='w', pady=(18, 0))
        self.compare_box = ttk.Combobox(right, textvariable=self.compare_with, state='readonly', width=30)
        self.compare_box.pack(fill='x', pady=6)
        self.compare_box.bind('<<ComboboxSelected>>', lambda e: self.refresh_table(preselect=True))

        ttk.Label(right, text='OPTIONS', style='Section.TLabel').pack(anchor='w', pady=(18, 0))
        ttk.Checkbutton(right, text='Remove playlists on the drive\nthat are not ticked',
                        variable=self.remove_others).pack(anchor='w', pady=2)
        ttk.Checkbutton(right, text='Delete songs no playlist on\nthe drive uses any more',
                        variable=self.prune).pack(anchor='w', pady=2)
        body.add(right, weight=1)

        bottom = ttk.Frame(self)
        bottom.pack(fill='x', **pad)
        ttk.Button(bottom, text='SELECT ALL', command=lambda: self.select_all(True)).pack(side='left')
        ttk.Button(bottom, text='SELECT NONE', command=lambda: self.select_all(False)).pack(side='left', padx=6)
        ttk.Label(bottom, textvariable=self.status, style='Dim.TLabel').pack(side='left', padx=12)
        self.export_btn = ttk.Button(bottom, text='EXPORT', style='Accent.TButton', command=self.start_export)
        self.export_btn.pack(side='right')

        self.bar = ttk.Progressbar(self, mode='determinate')
        self.bar.pack(fill='x', padx=12, pady=(2, 0))
        logf = ttk.Frame(self)
        logf.pack(fill='both', padx=12, pady=(6, 12))
        self.log = tk.Text(logf, height=7, state='disabled', font=('Consolas', 9), bg=PANEL, fg=DIM,
                           relief='flat', borderwidth=0, highlightthickness=1, highlightbackground=HAIR,
                           highlightcolor=HAIR, padx=8, pady=6, insertbackground=LINE)
        lsb = ttk.Scrollbar(logf, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side='left', fill='both', expand=True)
        lsb.pack(side='left', fill='y')

    # ------------------------------------------------------------ drives ----
    def refresh_drives(self):
        found = removable_drives()
        manual = {r: v for r, v in self.drives.items() if v[0] == '(folder)'}
        self.drives = {}
        for root, label in found:
            self.drives[root] = (label, tk.BooleanVar(value=True))
        self.drives.update(manual)
        self._render_drives()

    def add_folder(self):
        path = filedialog.askdirectory(title='Folder to export to (e.g. a drive root)')
        if path:
            path = os.path.normpath(path)
            self.drives[path] = ('(folder)', tk.BooleanVar(value=True))
            self._render_drives()

    def _render_drives(self):
        for w in self.drive_box.winfo_children():
            w.destroy()
        if not self.drives:
            ttk.Label(self.drive_box, text='No USB drive found - plug one in\nand press Refresh.',
                      style='Dim.TLabel').pack(anchor='w')
        for root, (label, var) in self.drives.items():
            try:
                du = shutil.disk_usage(root)
                space = f'{size_text(du.free)} free of {size_text(du.total)}'
            except OSError:
                space = 'not readable'
            name = short(root) if label == '(folder)' else f'{root}  {label}'
            ttk.Checkbutton(self.drive_box, text=f'{name}\n{space}', variable=var).pack(anchor='w', pady=2)
        roots = list(self.drives)
        self.compare_box['values'] = roots
        if self.compare_with.get() not in roots:
            self.compare_with.set(roots[0] if roots else '')
        self.refresh_table(preselect=True)

    # ----------------------------------------------------------- library ----
    def pick_library(self):
        path = filedialog.askopenfilename(title='iTunes Music Library.xml',
                                          filetypes=[('iTunes library', '*.xml'), ('All files', '*.*')])
        if path:
            self.library.set(os.path.normpath(path))
            self.load_library()

    def load_library(self):
        self.status.set('Reading iTunes playlists…')
        path = self.library.get()

        def work():
            try:
                self.events.put(('loaded', ex.load_playlists(path)))
            except Exception as e:
                self.events.put(('error', f'Could not read {path}:\n{e}'))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- table ----
    def refresh_table(self, preselect=False):
        drive = self.compare_with.get()
        on_drive = ex.drive_playlists(drive) if drive and os.path.isdir(drive) else {}
        if preselect and on_drive:
            self.selected = {p.name for p in self.playlists if p.name in on_drive}
        self.tree.delete(*self.tree.get_children())
        for p in self.playlists:
            state, detail = ex.compare(p, on_drive.get(p.name), drive) if drive else ('', '')
            if p.missing:
                detail = ', '.join(filter(None, [detail, f'{len(p.missing)} not found on PC']))
            self.tree.insert('', 'end', iid='it:' + p.name, tags=(state,),
                             values=(CHECKED if p.name in self.selected else UNCHECKED,
                                     p.name, len(p.songs), state, detail))
        itunes = {p.name for p in self.playlists}
        for name, lines in on_drive.items():
            if name not in itunes:
                self.tree.insert('', 'end', iid='dr:' + name, tags=('only on drive',),
                                 values=('', name, len(lines), 'only on drive', 'not in iTunes'))
        self._update_status()

    def _on_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row.startswith('it:'):
            return
        name = row[3:]
        self.selected ^= {name}
        self.tree.set(row, 'sel', CHECKED if name in self.selected else UNCHECKED)
        self._update_status()

    def select_all(self, on):
        self.selected = {p.name for p in self.playlists} if on else set()
        self.refresh_table()

    def _update_status(self):
        songs = sum(len(p.songs) for p in self.playlists if p.name in self.selected)
        self.status.set(f'{len(self.selected)} of {len(self.playlists)} playlists ticked, {songs} songs')

    # ------------------------------------------------------------ export ----
    def start_export(self):
        targets = [r for r, (_, var) in self.drives.items() if var.get()]
        chosen = [p for p in self.playlists if p.name in self.selected]
        if not targets:
            messagebox.showwarning('Export', 'Tick at least one drive under "Export to".')
            return
        if not chosen and not self.remove_others.get():
            messagebox.showwarning('Export', 'Tick at least one playlist.')
            return
        deleting = [t for t, on in (('playlists that are not ticked', self.remove_others.get()),
                                    ('songs no playlist uses', self.prune.get())) if on]
        if deleting and not messagebox.askyesno(
                'Export', f'This will delete {" and ".join(deleting)} on:\n' + '\n'.join(targets) + '\n\nContinue?'):
            return
        self.export_btn.state(['disabled'])
        self.bar['value'] = 0
        remove, prune = self.remove_others.get(), self.prune.get()

        def work():
            for drive in targets:
                self.events.put(('log', f'--- {drive}'))
                try:
                    summary = ex.export(drive, chosen, remove_others=remove, prune=prune,
                                        log=lambda m: self.events.put(('log', m)),
                                        progress=lambda d, t: self.events.put(('progress', d, t)))
                    self.events.put(('log', summary))
                except Exception as e:
                    self.events.put(('log', f'FAILED: {e}'))
            self.events.put(('finished', targets))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ events ----
    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == 'loaded':
                    self.playlists = ev[1]
                    self.refresh_table(preselect=True)
                    if not self.playlists:
                        self.status.set('No playlists of your own in iTunes yet')
                elif kind == 'error':
                    self.status.set('')
                    messagebox.showerror('iTunes library', ev[1])
                elif kind == 'log':
                    self._log(ev[1])
                elif kind == 'progress':
                    self.bar['maximum'], self.bar['value'] = max(ev[2], 1), ev[1]
                elif kind == 'finished':
                    self.export_btn.state(['!disabled'])
                    self._log('Done. Eject the drive(s) before unplugging.')
                    self._render_drives()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')


if __name__ == '__main__':
    App().mainloop()
