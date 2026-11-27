"""Native Windows ground station for the YRT bench tests."""

from collections import Counter
from datetime import datetime
import ipaddress
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import fusion
from mobile_gateway import GatewayLink
import serial
from video_view import VideoWindow


NAME = {'lora': 'LoRa', 'wifi': 'Wi-Fi 网桥', '4g': '4G'}
DESIGN = {
    'ink': '#253b53', 'muted': '#6d7e8e', 'line': '#e5dccb', 'paper': '#f7f4ec',
    'card': '#fffefa', 'navy': '#12374b', 'blue': '#7899d6', 'gold': '#d2b47c',
    'green': '#397968', 'red': '#b45a53', 'amber': '#a77c40',
    'soft_green': '#e9f2ec', 'soft_blue': '#edf1f8', 'soft_gold': '#f7f0e1',
}
OUTCOME = {'adopted': '采用', 'duplicate': '延迟', 'late': '晚到'}
OUTCOME_COLORS = {
    'adopted': (DESIGN['soft_green'], DESIGN['green']),
    'duplicate': (DESIGN['soft_gold'], DESIGN['amber']),
    'late': ('#f6e9e6', DESIGN['red']),
}
SETTINGS = (
    ('serial', 'LoRa 串口'), ('wifi_ip', '电脑网桥 IP'), ('wifi_peer', 'RK3588 网桥 IP'),
    ('wifi_port', '网桥 UDP 端口'), ('ecs', 'ECS IPv4'), ('lte_port', 'ECS 数传端口'),
    ('lte_ip', 'Windows 4G 网卡 IP（用于自动识别接口）'),
    ('lte_at', 'EC200A AT 串口'),
    ('gateway_url', '手机控制网关 HTTPS 地址'), ('gateway_token', '手机控制 PC 令牌（32+ 位）'),
)


def data_directory():
    if getattr(sys, 'frozen', False):
        folder = Path(sys.executable).parent
        if (folder / 'windows.json').is_file():
            return folder
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'YRT' / 'ThreeLink'


def asset_path(name):
    bundled = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / name
    if bundled.is_file():
        return bundled
    return Path(__file__).resolve().parents[2] / 'ground-station' / name


class Station:
    def __init__(self, root, directory):
        self.root = root
        self.directory = directory
        self.settings = directory / 'windows.json'
        directory.mkdir(parents=True, exist_ok=True)
        if not self.settings.exists():
            shutil.copyfile(Path(__file__).with_name('windows.json'), self.settings)
        self.config = json.loads(self.settings.read_text(encoding='utf-8-sig'))
        self.config.setdefault('lte_at', 'COM7')
        self.config.setdefault('gateway_url', '')
        self.config.setdefault('gateway_token', '')
        self.config.setdefault('vehicle_mode', 'bridge')
        self.logs = directory / self.config['logs']
        self.logs.mkdir(parents=True, exist_ok=True)
        self.rx = self.logs / 'rx.jsonl'
        self.worker = None
        self.stop_signal = threading.Event()
        self.result = None
        self.clock_sync = None
        self.live_run = ''
        self.run_started_at = 0.0
        self.run_elapsed_s = 0.0
        self.live_feed = None
        self.live_rows = []
        self.arrival_view = None
        self.arrival_count = 0
        self.fetch = None
        self.dial_worker = None
        self.dial_result = ''
        self.pending_rx = self.logs / 'rx-download.part'
        self.status = tk.StringVar(value='就绪。请确认 RK3588 已上电、EC200A 已拨号。')
        self.selected = tk.StringVar()
        self.count = tk.StringVar(value='20')
        self.hz = tk.StringVar(value='1')
        self.vehicle_mode = tk.StringVar(value=('网桥实时' if self.config.get('vehicle_mode', 'bridge') == 'bridge'
                                                else '4G实验后回传'))
        self.vehicle_mode.trace_add('write', lambda *_: self.config.__setitem__(
            'vehicle_mode', 'lte' if self.vehicle_mode.get() == '4G实验后回传' else 'bridge'))
        self.links = {link: tk.BooleanVar(value=True) for link in fusion.LINKS}
        self.replay_links = {link: tk.BooleanVar(value=True) for link in fusion.LINKS}
        self.route_text = tk.StringVar()
        self.gateway = GatewayLink(self.config)
        self.video_window = None
        self.make_window()
        self.load_runs()
        self.update_routes()
        self.tick()

    def make_window(self):
        root = self.root
        root.title('YRT')
        root.geometry('970x760')
        root.minsize(840, 670)
        root.configure(bg=DESIGN['paper'])
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.option_add('*Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame', background=DESIGN['paper'])
        style.configure('Card.TFrame', background=DESIGN['card'])
        style.configure('TLabel', background=DESIGN['paper'], foreground=DESIGN['ink'])
        style.configure('Card.TLabel', background=DESIGN['card'], foreground=DESIGN['ink'])
        style.configure('Muted.TLabel', background=DESIGN['card'], foreground=DESIGN['muted'])
        style.configure('Section.TLabel', background=DESIGN['card'], foreground=DESIGN['ink'],
                        font=('Microsoft YaHei UI', 12, 'bold'))
        unchecked = self.checkbox_image(root, False)
        checked = self.checkbox_image(root, True)
        self._checkbox_images = (unchecked, checked)
        style.element_create('YRT.Checkbutton.indicator', 'image', unchecked, ('selected', checked), sticky='')
        style.layout('TCheckbutton', [
            ('Checkbutton.padding', {'sticky': 'nswe', 'children': [
                ('YRT.Checkbutton.indicator', {'side': 'left', 'sticky': ''}),
                ('Checkbutton.focus', {'side': 'left', 'sticky': '', 'children': [
                    ('Checkbutton.label', {'sticky': 'nswe'})
                ]})
            ]})
        ])
        style.configure('TCheckbutton', background=DESIGN['paper'], foreground=DESIGN['ink'], padding=(5, 4))
        style.configure('Card.TCheckbutton', background=DESIGN['card'], foreground=DESIGN['ink'], padding=(5, 4))
        style.configure('TButton', padding=(12, 8), background=DESIGN['card'], foreground=DESIGN['ink'],
                        bordercolor=DESIGN['line'], lightcolor=DESIGN['line'], darkcolor=DESIGN['line'],
                        focuscolor=DESIGN['blue'])
        style.map('TButton', background=[('disabled', DESIGN['paper']), ('active', DESIGN['soft_blue'])],
                  foreground=[('disabled', DESIGN['muted'])])
        style.configure('Primary.TButton', padding=(13, 8), background=DESIGN['blue'], foreground='white',
                        bordercolor=DESIGN['blue'], lightcolor=DESIGN['blue'], darkcolor=DESIGN['blue'])
        style.map('Primary.TButton', background=[('disabled', '#aab8d1'), ('active', '#5e81c3')],
                  foreground=[('disabled', 'white')])
        style.configure('Danger.TButton', padding=(12, 8), background=DESIGN['red'], foreground='white',
                        bordercolor=DESIGN['red'], lightcolor=DESIGN['red'], darkcolor=DESIGN['red'])
        style.map('Danger.TButton', background=[('disabled', '#c6ced2'), ('active', '#9f4c48')])
        style.configure('TEntry', padding=(8, 6), fieldbackground=DESIGN['card'], foreground=DESIGN['ink'],
                        bordercolor=DESIGN['line'], insertcolor=DESIGN['ink'])
        style.configure('TCombobox', padding=(7, 5), fieldbackground=DESIGN['card'], foreground=DESIGN['ink'],
                        bordercolor=DESIGN['line'], arrowcolor=DESIGN['muted'])
        style.map('TCombobox', fieldbackground=[('readonly', DESIGN['card'])],
                  foreground=[('readonly', DESIGN['ink'])], selectbackground=[('readonly', DESIGN['soft_blue'])],
                  selectforeground=[('readonly', DESIGN['ink'])])
        style.configure('TNotebook', background=DESIGN['card'], bordercolor=DESIGN['line'], tabmargins=(0, 0, 0, 0))
        style.configure('TNotebook.Tab', width=8, padding=(14, 8), background=DESIGN['soft_blue'],
                        foreground=DESIGN['muted'], bordercolor=DESIGN['line'])
        style.map('TNotebook.Tab', background=[('selected', DESIGN['card']), ('active', DESIGN['soft_blue'])],
                  foreground=[('selected', DESIGN['blue']), ('active', DESIGN['ink'])])
        style.configure('Treeview', font=('Microsoft YaHei UI', 10), rowheight=30, borderwidth=0,
                        background=DESIGN['card'], fieldbackground=DESIGN['card'], foreground=DESIGN['ink'])
        style.map('Treeview', background=[('selected', DESIGN['soft_blue'])],
                  foreground=[('selected', DESIGN['ink'])])
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'),
                        background=DESIGN['soft_blue'], foreground=DESIGN['ink'], bordercolor=DESIGN['line'])
        style.configure('TSeparator', background=DESIGN['line'])

        header = tk.Frame(root, bg=DESIGN['navy'], padx=22, pady=10)
        header.pack(fill='x')
        self.brand_photo = tk.PhotoImage(master=root, file=str(asset_path('brand-logo.png'))).subsample(10, 10)
        brand = tk.Canvas(header, width=175, height=60, bg=DESIGN['navy'], highlightthickness=0, bd=0)
        brand.pack()
        brand.create_image(87, 30, image=self.brand_photo, anchor='center')
        tk.Frame(root, bg=DESIGN['gold'], height=1).pack(fill='x')

        body = ttk.Frame(root, padding=(18, 14))
        body.pack(fill='both', expand=True)
        controls = ttk.Frame(body, style='Card.TFrame', padding=(14, 12))
        controls.pack(fill='x')
        form = ttk.Frame(controls, style='Card.TFrame')
        form.pack(fill='x')
        for label, var in (('指令条数', self.count), ('频率 Hz', self.hz)):
            ttk.Label(form, text=label, style='Card.TLabel').pack(side='left', padx=(0, 5))
            ttk.Entry(form, textvariable=var, width=6).pack(side='left', padx=(0, 15))
        ttk.Label(form, text='车端数据', style='Card.TLabel').pack(side='left', padx=(0, 5))
        ttk.Combobox(form, textvariable=self.vehicle_mode, width=14, state='readonly',
                     values=('网桥实时', '4G实验后回传')).pack(side='left', padx=(0, 12))
        for link in fusion.LINKS:
            ttk.Checkbutton(form, text=NAME[link], variable=self.links[link], style='Card.TCheckbutton').pack(
                side='left', padx=(0, 5))
        self.settings_button = ttk.Button(form, text='接口设置', command=self.edit_settings)
        self.settings_button.pack(side='right')
        self.dial_button = ttk.Button(form, text='4G 拨号', command=self.dial_modem)
        self.dial_button.pack(side='right', padx=5)
        ttk.Label(controls, textvariable=self.route_text, style='Muted.TLabel').pack(anchor='w', pady=(8, 10))

        actions = ttk.Frame(controls, style='Card.TFrame')
        actions.pack(fill='x', pady=(0, 8))
        self.start_button = ttk.Button(actions, text='开始发包', style='Primary.TButton', command=self.start_run)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(actions, text='停止发包', style='Danger.TButton', command=self.stop_run, state='disabled')
        self.stop_button.pack(side='left', padx=6)
        ttk.Label(actions, text='实验', style='Card.TLabel', padding=(12, 0, 4, 0)).pack(side='left')
        self.run_list = ttk.Combobox(actions, textvariable=self.selected, width=18, state='readonly')
        self.run_list.pack(side='left')
        self.run_list.bind('<<ComboboxSelected>>', lambda _: self.render())
        ttk.Button(actions, text='打开日志目录', command=lambda: os.startfile(self.logs)).pack(side='right')
        ttk.Button(actions, text='图传', command=self.open_video).pack(side='right', padx=6)

        self.tx_table = self.table(controls, ('链路', '已提交', '提交失败', '最近一次失败'), (110, 85, 85, 460), 3)
        ttk.Label(controls, text='已提交表示交给本机接口。车端是否收到，以接收日志为准。',
                  style='Muted.TLabel').pack(anchor='w', pady=(5, 0))

        received = ttk.Frame(body, style='Card.TFrame', padding=(14, 12))
        received.pack(fill='both', expand=True, pady=(10, 0))
        receive = ttk.Frame(received, style='Card.TFrame')
        receive.pack(fill='x')
        ttk.Label(receive, text='车端接收', style='Section.TLabel').pack(side='left')
        ttk.Button(receive, text='导出 CSV', command=self.export).pack(side='right')
        ttk.Button(receive, text='导入日志', command=self.import_log).pack(side='right', padx=5)
        self.fetch_button = ttk.Button(receive, text='从 RK3588 取日志', command=self.fetch_log)
        self.fetch_button.pack(side='right')
        self.rx_status = tk.StringVar()
        ttk.Label(received, textvariable=self.rx_status, style='Muted.TLabel').pack(anchor='w', pady=(8, 6))
        tabs = ttk.Notebook(received)
        tabs.pack(fill='both', expand=True)
        arrivals, replay = ttk.Frame(tabs, padding=8, style='Card.TFrame'), ttk.Frame(tabs, padding=8, style='Card.TFrame')
        tabs.add(arrivals, text='接收明细')
        tabs.add(replay, text='回放')
        ttk.Label(arrivals, text='相对首达差由车端单调时钟计算；发送→接收差经 SSH 校准，不依赖系统日期。低于校时精度的估计不显示为有效时延，CSV 保留原始值。',
                   style='Muted.TLabel').pack(anchor='w', pady=(0, 7))
        self.arrivals_table = self.table(arrivals, ('到达顺序', '指令序号', '链路', '指令值', '车端处理', '相对首达 ms', '发送提交→接收 ms'),
                                         (78, 78, 99, 63, 81, 102, 112), 5, scroll=True)
        for outcome, (background, foreground) in OUTCOME_COLORS.items():
            self.arrivals_table.tag_configure(outcome, background=background, foreground=foreground)
        replay_scope = ttk.Frame(replay, style='Card.TFrame')
        replay_scope.pack(fill='x', pady=(0, 6))
        ttk.Label(replay_scope, text='参与回放', style='Card.TLabel').pack(side='left', padx=(0, 8))
        for link in fusion.LINKS:
            ttk.Checkbutton(replay_scope, text=NAME[link], variable=self.replay_links[link],
                            style='Card.TCheckbutton', command=self.render).pack(side='left', padx=(0, 5))
        self.replay_table = self.table(replay, ('回放范围', '有效更新', '最长更新空档', '采用来源'), (230, 90, 145, 340), 2)
        ttk.Separator(body).pack(fill='x', pady=(10, 6))
        ttk.Label(body, textvariable=self.status, wraplength=720).pack(fill='x', anchor='w')

    def open_video(self):
        if self.video_window is None or not self.video_window.winfo_exists():
            self.video_window = VideoWindow(self.root, lambda: self.config, DESIGN)
        self.video_window.deiconify()
        self.video_window.lift()

    @staticmethod
    def checkbox_image(root, selected):
        size = 18
        image = tk.PhotoImage(master=root, width=size, height=size)
        fill = DESIGN['blue'] if selected else DESIGN['card']
        border = fill if selected else '#b8ae9e'
        for y in range(size):
            for x in range(size):
                color = border if x in (0, size - 1) or y in (0, size - 1) else fill
                image.put(color, to=(x, y))
        if selected:
            def stroke(start, end):
                x0, y0 = start
                x1, y1 = end
                dx, dy = abs(x1 - x0), abs(y1 - y0)
                sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
                error = dx - dy
                while True:
                    for ox, oy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                        image.put(DESIGN['card'], to=(x0 + ox, y0 + oy))
                    if (x0, y0) == (x1, y1):
                        break
                    twice = 2 * error
                    if twice > -dy:
                        error -= dy
                        x0 += sx
                    if twice < dx:
                        error += dx
                        y0 += sy
            stroke((3, 8), (7, 12))
            stroke((7, 12), (14, 5))
        return image

    @staticmethod
    def table(parent, columns, widths, height, scroll=False):
        table = ttk.Treeview(parent, columns=columns, show='headings', height=height, selectmode='none')
        for col, width in zip(columns, widths):
            table.heading(col, text=col, anchor='w')
            table.column(col, width=width, minwidth=60, stretch=col == columns[-1])
        if scroll:
            scrollbar = ttk.Scrollbar(parent, orient='vertical', command=table.yview)
            scrollbar.pack(side='right', fill='y')
            table.configure(yscrollcommand=scrollbar.set)
            table.pack(side='left', fill='both', expand=True)
        else:
            table.pack(fill='x')
        return table

    @staticmethod
    def fill(table, rows):
        table.delete(*table.get_children())
        for row in rows:
            table.insert('', 'end', values=row)

    def tx_path(self):
        return self.logs / f'tx-{self.selected.get()}.jsonl'

    def load_runs(self):
        files = sorted(self.logs.glob('tx-*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
        names = [file.stem[3:] for file in files]
        if self.live_run and self.live_run not in names:
            names.insert(0, self.live_run)
        self.run_list.configure(values=names)
        if names and self.selected.get() not in names:
            self.selected.set(names[0])
        self.render()

    def update_routes(self):
        cfg = self.config
        ports = fusion.list_ports.comports() if cfg.get('serial_auto', True) else ()
        detected = []
        for role in ('lora', '4g'):
            try:
                detected.append(fusion.windows_serial_port(cfg, role, ports))
            except OSError:
                detected.append('未识别')
        self.route_text.set(f"LoRa {detected[0]} · 4G AT {detected[1]}    |    {cfg['wifi_peer']}:{cfg['wifi_port']} / UDP"
                            f"    |    {cfg['ecs']}:{cfg['lte_port']} / TCP")

    def render_tx(self):
        counts = Counter()
        errors = {}
        path = self.tx_path()
        if path.exists():
            for row in fusion.read_log(path):
                if 0 < row['seq'] < fusion.END:
                    counts[row['link'], row['outcome']] += 1
                    if row.get('error'):
                        errors[row['link']] = row['error']
        self.fill(self.tx_table, [(NAME[link], counts[link, 'submitted'], counts[link, 'failed'], errors.get(link, '—'))
                                  for link in fusion.LINKS])

    def render(self):
        self.render_tx()
        self.fill(self.replay_table, [])
        run = self.selected.get()
        rows = [row for row in fusion.read_log(self.rx) if row['run'] == run] if run and self.rx.is_file() else []
        if not rows and run and run == self.live_run:
            self.render_live()
            return
        self.render_arrivals(rows, (run, 'saved', self.rx.stat().st_mtime_ns if rows else 0))
        if not rows:
            self.rx_status.set('选择实验后，从 RK3588 取日志或导入 rx.jsonl。')
            return
        omitted = [link for link, var in self.replay_links.items() if not var.get()]
        try:
            complete, received, _, (updates, gap, sources) = fusion.summarize(self.rx, run, omitted)
        except (OSError, ValueError) as error:
            self.rx_status.set('导入的日志中没有本轮记录。' if str(error) == 'Run not found'
                               else f'无法回放 · {error}')
            return
        figures = '   '.join(f'{NAME[link]} {received[link]} 份' for link in fusion.LINKS)
        self.rx_status.set(figures + ('   ·   起止帧齐全' if complete else '   ·   缺起止帧，观察窗口不完整'))
        if len(omitted) == len(fusion.LINKS):
            self.fill(self.replay_table, [('请选择至少一条链路', '—', '—', '—')])
        else:
            scope = '排除 ' + ' / '.join(NAME[link] for link in omitted) if omitted else '全部链路'
            by_source = ' / '.join(f'{NAME[link]} {n}' for link, n in sources.items())
            self.fill(self.replay_table, [(scope, updates, f'{gap:.1f} ms', by_source or '—')])

    def render_arrivals(self, rows, source):
        tx_path = self.tx_path()
        tx_rows = list(fusion.read_log(tx_path)) if tx_path.is_file() else []
        if self.arrival_view != source:
            self.fill(self.arrivals_table, [])
            self.arrival_view = source
            self.arrival_count = 0
        for row in fusion.arrival_details(rows, tx_rows):
            order = row.get('rx_order', 0)
            if order > self.arrival_count:
                outcome = row['outcome']
                send_delay = row.get('send_to_receive_ms')
                delay_state = row.get('send_time_status')
                if delay_state == 'sync_uncertain':
                    send_delay = f'~{send_delay:.1f}' if send_delay is not None else '校时精度低'
                elif delay_state == 'sync_negative_uncertain':
                    send_delay = '校时误差内'
                elif delay_state == 'below_sync_resolution':
                    send_delay = '低于校时精度'
                elif delay_state == 'sync_check':
                    send_delay = '校准异常'
                elif delay_state == 'board_rebooted':
                    send_delay = '校时后板子重启'
                elif delay_state == 'sync_failed':
                    send_delay = '校时失败'
                elif delay_state == 'missing_sync':
                    send_delay = '历史记录无校时'
                elif send_delay is not None:
                    send_delay = f'{send_delay:.1f}'
                else:
                    send_delay = '—'
                self.arrivals_table.insert('', 'end', values=(order, row['seq'], NAME[row['link']], row['value'],
                    OUTCOME[outcome], f"{row['relative_delay_ms']:.1f}", send_delay), tags=(outcome,))
                self.arrival_count = order

    def render_live(self):
        if self.selected.get() != self.live_run:
            return
        counts = Counter(row['link'] for row in self.live_rows if 0 < row['seq'] < fusion.END)
        figures = '   '.join(f'{NAME[link]} {counts[link]} 份' for link in fusion.LINKS)
        self.rx_status.set(figures + '   ·   通过 SSH 实时查看，完整数据以取回的车端日志为准')
        self.render_arrivals(self.live_rows, (self.live_run, 'live'))

    def start_live_feed(self):
        ssh = shutil.which('ssh.exe')
        if not ssh:
            return
        run = self.live_run
        destination = f"ztl@{self.config['wifi_peer']}"
        incoming = queue.Queue()
        try:
            process = subprocess.Popen([ssh, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', '-o',
                                        'ServerAliveInterval=3', '-o', 'ServerAliveCountMax=2', destination,
                                        'tail -n 0 -F /var/lib/yrt-fusion/rx.jsonl'], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            return

        def read():
            with process.stdout:
                for raw in process.stdout:
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    if row['run'] == run:
                        incoming.put(row)

        self.live_feed = (process, incoming)
        threading.Thread(target=read, daemon=True).start()

    def pull_live_rows(self):
        if self.live_feed is None:
            return
        _, incoming = self.live_feed
        before = len(self.live_rows)
        while True:
            try:
                self.live_rows.append(incoming.get_nowait())
            except queue.Empty:
                break
        if len(self.live_rows) > before:
            tx_rows = list(fusion.read_log(self.tx_path())) if self.tx_path().is_file() else []
            enriched = list(fusion.arrival_details(self.live_rows, tx_rows))
            self.gateway.post_live(self.live_run, enriched[before:])
            self.render_live()

    def stop_live_feed(self):
        if self.live_feed is not None:
            process, _ = self.live_feed
            if process.poll() is None:
                process.terminate()
            self.live_feed = None

    def edit_settings(self):
        dialog = tk.Toplevel(self.root, padx=16, pady=12)
        dialog.title('接口设置')
        dialog.transient(self.root)
        dialog.grab_set()
        fields = {}
        for i, (key, label) in enumerate(SETTINGS):
            ttk.Label(dialog, text=label).grid(row=i, column=0, sticky='w', pady=3, padx=(0, 10))
            fields[key] = tk.StringVar(dialog, str(self.config[key]))
            ttk.Entry(dialog, textvariable=fields[key], width=42 if key == 'gateway_url' else 24,
                      show='●' if key == 'gateway_token' else '').grid(row=i, column=1, pady=3)
        auto = tk.BooleanVar(dialog, value=self.config.get('serial_auto', True))
        ttk.Checkbutton(dialog, text='自动识别 LoRa 和 4G 串口', variable=auto).grid(
            row=len(fields), column=0, columnspan=2, sticky='w', pady=(6, 0))

        def save():
            updated = dict(self.config, **{key: value.get().strip() for key, value in fields.items()})
            updated['serial_auto'] = auto.get()
            try:
                for key in ('wifi_ip', 'wifi_peer', 'ecs', 'lte_ip'):
                    ipaddress.IPv4Address(updated[key])
                for key in ('wifi_port', 'lte_port'):
                    updated[key] = int(updated[key])
                    if not 1 <= updated[key] <= 65535:
                        raise ValueError('端口超出范围。')
                updated['gateway_url'] = updated['gateway_url'].rstrip('/')
                if bool(updated['gateway_url']) != bool(updated['gateway_token']):
                    raise ValueError('手机控制网关地址和 PC 令牌需要同时填写。')
                if updated['gateway_url'] and (not updated['gateway_url'].startswith('https://') or
                                               len(updated['gateway_token']) < 32):
                    raise ValueError('网关须使用 HTTPS，令牌至少 32 位。')
                if not auto.get():
                    if not updated['serial'] or not updated['lte_at'] or updated['serial'].upper() == updated['lte_at'].upper():
                        raise ValueError('请为 LoRa 和 EC200A 填入各自的串口。')
                self.settings.write_text(json.dumps(updated, indent=2) + '\n', encoding='utf-8')
            except (OSError, ValueError) as error:
                messagebox.showerror('保存设置失败', str(error), parent=dialog)
                return
            self.config = updated
            self.gateway.close()
            self.gateway = GatewayLink(self.config)
            self.update_routes()
            dialog.destroy()

        ttk.Button(dialog, text='保存', style='Primary.TButton', command=save).grid(row=len(fields) + 1, column=1, sticky='e', pady=(10, 0))

    def start_run(self):
        if self.worker is not None or self.dial_worker is not None:
            return
        try:
            count, hz = int(self.count.get()), float(self.hz.get())
            if not 1 <= count <= 5000 or not 0.1 <= hz <= 10:
                raise ValueError
        except ValueError:
            self.status.set('请输入 1–5000 条指令和 0.1–10 Hz。')
            return
        omitted = [link for link, enabled in self.links.items() if not enabled.get()]
        if len(omitted) == len(fusion.LINKS):
            self.status.set('请至少选择一条链路。')
            return
        self.update_routes()
        try:
            run = fusion.reserve_run(self.logs)
        except OSError as error:
            self.status.set(f'无法创建实验 · {error}')
            return
        self.live_run = fusion.format_run(run)
        self.config['vehicle_mode'] = 'lte' if self.vehicle_mode.get() == '4G实验后回传' else 'bridge'
        self.run_started_at = time.monotonic()
        self.run_elapsed_s = 0.0
        self.live_rows = []
        self.selected.set(self.live_run)
        self.stop_signal.clear()
        self.result = None
        self.clock_sync = None
        config = dict(self.config, logs=str(self.logs))

        def send():
            try:
                self.clock_sync = fusion.synchronize_monotonic_clock(config)
                fusion.transmit(config, hz, range(1, count + 1), omitted, run=run,
                                stop=self.stop_signal, clock_sync=self.clock_sync)
                # Let the LoRa radio deliver its queued end frame before offering the log fetch.
                if 'lora' not in omitted:
                    time.sleep(2)
            except Exception as error:
                self.result = str(error)

        if self.config['vehicle_mode'] == 'bridge':
            self.start_live_feed()
        else:
            self.stop_live_feed()
        self.worker = threading.Thread(target=send)
        self.worker.start()
        self.start_button.configure(state='disabled')
        self.settings_button.configure(state='disabled')
        self.dial_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.fetch_button.configure(state='disabled')
        self.status.set(f'正在校准 RK3588 单调时钟并发包 · {self.live_run}')
        self.load_runs()

    def stop_run(self):
        self.stop_signal.set()
        self.stop_button.configure(state='disabled')
        self.status.set('已停止生成新指令，正在保存本轮日志。')

    def dial_modem(self):
        if self.worker is not None or self.dial_worker is not None:
            self.status.set('实验或拨号正在运行，未执行新的拨号命令。')
            return
        self.update_routes()
        try:
            port = fusion.windows_serial_port(self.config, '4g')
        except OSError as error:
            self.status.set(str(error))
            return

        def dial():
            try:
                with serial.Serial(port, 115200, timeout=0.2, write_timeout=2) as line:
                    line.reset_input_buffer()
                    line.write(b'AT+QNETDEVCTL=1,1,1\r')
                    response = bytearray()
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        response.extend(line.read(line.in_waiting or 1))
                        if b'+QNETDEVSTATUS: 1' in response:
                            break
                        if b'ERROR' in response:
                            break
                    line.write(b'AT+QNETDEVCTL?\r')
                    time.sleep(0.5)
                    state = line.read(line.in_waiting or 1).decode('ascii', errors='replace')
                    connected = any(row.startswith('+QNETDEVCTL:') and row.endswith(',1')
                                    for row in state.splitlines())
                    self.dial_result = ('EC200A 数据连接已建立。' if connected else
                                        '未确认 4G 拨号成功，请核对 AT 口、SIM 和信号。')
            except (OSError, ValueError) as error:
                self.dial_result = f'4G 拨号失败 · {error}'

        self.start_button.configure(state='disabled')
        self.dial_button.configure(state='disabled')
        self.settings_button.configure(state='disabled')
        self.status.set(f'正在通过 {port} 拨号。')
        self.dial_worker = threading.Thread(target=dial)
        self.dial_worker.start()

    def import_log(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[('接收日志', '*.jsonl')])
        if path:
            self.accept_log(Path(path))

    def accept_log(self, path):
        try:
            rows = list(fusion.read_log(path))
            required = {'run', 'seq', 'link', 'value', 'outcome', 'mono_ns'}
            if not rows or not all(isinstance(row, dict) and required <= row.keys() and
                                   row['link'] in fusion.LINKS and isinstance(row['seq'], int) and
                                   row['outcome'] in ('start', 'end', 'adopted', 'late', 'duplicate') and
                                   isinstance(row['mono_ns'], int) for row in rows):
                raise ValueError('文件里没有有效的车端接收日志。')
            if path.resolve() != self.rx.resolve():
                staging = self.rx.with_suffix('.new')
                shutil.copyfile(path, staging)
                staging.replace(self.rx)
            if self.config.get('vehicle_mode', 'bridge') == 'bridge' and self.gateway.enabled:
                run = self.live_run or self.selected.get()
                run_rows = [row for row in rows if row.get('run') == run]
                if run_rows:
                    tx_path = self.logs / f'tx-{run}.jsonl'
                    tx_rows = list(fusion.read_log(tx_path)) if tx_path.is_file() else []
                    self.gateway.upload_station_log(run, list(fusion.arrival_details(run_rows, tx_rows)))
            self.status.set(f'已导入 {len(rows)} 条接收记录 · {datetime.now():%H:%M:%S}')
            self.render()
            return True
        except (OSError, ValueError, TypeError) as error:
            self.status.set(f'导入失败 · {error}')
            return False

    def accept_gateway_rows(self, run, rows):
        try:
            current = list(fusion.read_log(self.rx)) if self.rx.is_file() else []
            merged = [row for row in current if row.get('run') != run] + rows
            staging = self.logs / 'rx-gateway-import.jsonl'
            staging.write_text(''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n'
                                           for row in merged), encoding='utf-8')
            if self.accept_log(staging):
                staging.unlink(missing_ok=True)
                self.selected.set(run)
                self.load_runs()
                tx_path = self.logs / f'tx-{run}.jsonl'
                tx_rows = list(fusion.read_log(tx_path)) if tx_path.is_file() else []
                self.gateway.upload_station_log(run, list(fusion.arrival_details(rows, tx_rows)))
                self.status.set(f'已通过 4G 收到 {run} 的 {len(rows)} 条车端记录。')
        except (OSError, ValueError, TypeError) as error:
            self.status.set(f'4G 车端数据导入失败 · {error}')

    def fetch_log(self, automatic=False):
        if self.worker is not None:
            self.status.set('实验仍在运行，结束后才能取回本轮车端日志。')
            return
        if self.fetch is not None:
            self.status.set('正在取回车端日志，请等待当前传输完成。')
            return
        if self.config.get('vehicle_mode') == 'lte':
            if not self.gateway.enabled:
                self.status.set('4G 取日志需要配置手机控制网关。')
                return
            run = self.live_run or self.selected.get()
            if run:
                self.gateway.upload_vehicle_log(run)
                self.status.set(f'已通过 4G 请求车端回传 · {run}')
            return
        scp = shutil.which('scp.exe')
        if not scp:
            self.status.set('未找到 Windows OpenSSH。可以直接导入板端 rx.jsonl。')
            return
        endpoint = f"ztl@{self.config['wifi_peer']}:/var/lib/yrt-fusion/rx.jsonl"
        try:
            self.pending_rx.unlink(missing_ok=True)
            # OpenSSH owns authentication in a separate terminal. No password is stored by the app.
            options = ['-o', 'BatchMode=yes'] if automatic else []
            self.fetch = subprocess.Popen([scp, '-o', 'ConnectTimeout=7', *options, endpoint, str(self.pending_rx)],
                                          creationflags=subprocess.CREATE_NO_WINDOW if automatic else subprocess.CREATE_NEW_CONSOLE)
            self.fetch_button.configure(state='disabled')
            self.status.set('正在自动取回车端日志。' if automatic else
                            '正在通过 SSH 取日志。如果终端提示，请在终端输入板端密码。')
        except OSError as error:
            self.status.set(f'无法启动 SCP · {error}')

    def export(self):
        path = self.tx_path()
        if not path.is_file() or not self.rx.is_file():
            self.status.set('先选择一轮实验，并取回车端日志。')
            return
        try:
            fusion.summarize(self.rx, self.selected.get())
        except ValueError:
            self.status.set('当前车端日志无法回放所选实验，请核对实验编号或重新取日志。')
            return
        output = filedialog.asksaveasfilename(parent=self.root, initialdir=self.logs,
                                              initialfile=f'merged-{self.selected.get()}.csv', defaultextension='.csv',
                                              filetypes=[('CSV 表格', '*.csv')])
        if output:
            try:
                fusion.merge(path, self.rx, Path(output))
                self.status.set(f'已导出 {output}')
            except (OSError, ValueError) as error:
                self.status.set(f'导出失败 · {error}')

    def process_gateway_events(self):
        for _ in range(50):
            try:
                event = self.gateway.incoming.get_nowait()
            except queue.Empty:
                break
            command_id = event.get('id')
            kind = event.get('command')
            try:
                if kind == 'start':
                    if self.worker is not None:
                        self.status.set('已有实验运行中，手机开始命令未执行。')
                    else:
                        self.count.set(str(event['count']))
                        self.hz.set(str(event['hz']))
                        for link in fusion.LINKS:
                            self.links[link].set(link in event['links'])
                        self.vehicle_mode.set('4G实验后回传' if event.get('vehicle_mode') == 'lte' else '网桥实时')
                        self.start_run()
                elif kind == 'stop':
                    self.stop_run()
                elif kind == 'dial':
                    self.dial_modem()
                elif kind == 'fetch_vehicle':
                    run = event.get('run') or self.live_run or self.selected.get()
                    if run:
                        self.selected.set(run)
                        if self.config.get('vehicle_mode') == 'lte':
                            self.gateway.upload_vehicle_log(run)
                        else:
                            self.fetch_log()
                elif kind == 'vehicle_data':
                    self.accept_gateway_rows(event['run'], event['rows'])
                elif kind == 'vehicle_data_timeout':
                    self.status.set(f"4G 回传仍未到达 · {event['run']}，可稍后再次取日志。")
            except (KeyError, TypeError, ValueError) as error:
                self.status.set(f'手机指令未执行 · {error}')
            if isinstance(command_id, int):
                self.gateway.acknowledge(command_id)

    def publish_gateway_state(self):
        table_rows = [self.tx_table.item(item, 'values') for item in self.tx_table.get_children()]
        links = {}
        for index, link in enumerate(fusion.LINKS):
            values = table_rows[index] if index < len(table_rows) else ()
            links[link] = {'enabled': self.links[link].get(),
                           'submitted': int(values[1]) if len(values) > 1 else 0,
                           'failed': int(values[2]) if len(values) > 2 else 0,
                           'last_failure': str(values[3]) if len(values) > 3 else '—'}
        self.gateway.set_state({
            'status': self.status.get(), 'run_id': self.live_run or self.selected.get(),
            'running': bool(self.worker and self.worker.is_alive()),
            'count': self.count.get(), 'hz': self.hz.get(),
            'sent': max((int(row[1]) for row in table_rows if len(row) > 1 and str(row[1]).isdigit()), default=0),
            'elapsed_s': round(time.monotonic() - self.run_started_at, 1) if self.run_started_at else self.run_elapsed_s,
            'vehicle_mode': self.config.get('vehicle_mode', 'bridge'), 'links': links,
            'route': self.route_text.get(), 'gateway_error': self.gateway.last_error,
        })

    def tick(self):
        self.process_gateway_events()
        self.pull_live_rows()
        if self.worker is not None:
            if self.worker.is_alive():
                self.render_tx()
            else:
                self.worker = None
                self.run_elapsed_s = max(0.0, time.monotonic() - self.run_started_at)
                self.run_started_at = 0.0
                self.stop_live_feed()
                self.start_button.configure(state='normal')
                self.settings_button.configure(state='normal')
                self.dial_button.configure(state='normal')
                self.stop_button.configure(state='disabled')
                if self.fetch is None:
                    self.fetch_button.configure(state='normal')
                if self.result:
                    message = f'测试失败 · {self.result}'
                elif self.clock_sync and self.clock_sync.get('status') in ('ok', 'uncertain'):
                    rtt_ms = self.clock_sync['rtt_ns'] / 1e6
                    message = f'本轮已保存 · {self.live_run} · 单调时钟校准 RTT {rtt_ms:.1f} ms'
                else:
                    error = (self.clock_sync or {}).get('error', 'SSH 校准不可用')
                    message = f'本轮已保存 · {self.live_run} · {error}'
                self.status.set(message)
                self.load_runs()
                if not self.result and self.config.get('vehicle_mode') == 'lte' and self.gateway.enabled:
                    self.gateway.upload_vehicle_log(self.live_run)
                    self.status.set(message + ' · 正在通过 4G 请求车端批量回传')
                elif self.fetch is None and not self.result and self.config.get('vehicle_mode') == 'bridge':
                    self.fetch_log(automatic=True)
        if self.dial_worker is not None and not self.dial_worker.is_alive():
            self.dial_worker = None
            self.start_button.configure(state='normal')
            self.dial_button.configure(state='normal')
            self.settings_button.configure(state='normal')
            self.status.set(self.dial_result)
        if self.fetch is not None and self.fetch.poll() is not None:
            if self.fetch.returncode == 0 and self.pending_rx.exists():
                self.accept_log(self.pending_rx)
            else:
                self.status.set('未取回日志。网桥恢复后，可点击「从 RK3588 取日志」补全接收记录。')
            self.pending_rx.unlink(missing_ok=True)
            self.fetch = None
            if self.worker is None:
                self.fetch_button.configure(state='normal')
        self.publish_gateway_state()
        self.root.after(400, self.tick)

    def close(self):
        if self.worker is not None:
            self.stop_run()
            self.root.after(400, self.close)
            return
        if self.dial_worker is not None:
            self.root.after(400, self.close)
            return
        self.stop_live_feed()
        if self.video_window is not None and self.video_window.winfo_exists():
            self.video_window.close()
        self.gateway.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        Station(root, data_directory())
    except (ValueError, OSError, KeyError) as error:
        messagebox.showerror('地面站启动失败', str(error), parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == '__main__':
    main()
