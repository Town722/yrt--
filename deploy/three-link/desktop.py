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
import serial


NAME = {'lora': 'LoRa', 'wifi': 'Wi-Fi 网桥', '4g': '4G'}
OUTCOME = {'adopted': '采用', 'duplicate': '重复', 'late': '晚到'}
SETTINGS = (
    ('serial', 'LoRa 串口'), ('wifi_ip', '电脑网桥 IP'), ('wifi_peer', 'RK3588 网桥 IP'),
    ('wifi_port', '网桥 UDP 端口'), ('ecs', 'ECS IPv4'), ('lte_port', 'ECS 数传端口'),
    ('lte_interface', 'Windows 4G 接口索引'), ('lte_ip', 'Windows 4G 网卡 IP'),
    ('lte_at', 'EC200A AT 串口'),
)


def data_directory():
    if getattr(sys, 'frozen', False):
        folder = Path(sys.executable).parent
        if (folder / 'windows.json').is_file():
            return folder
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'YRT' / 'ThreeLink'


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
        self.logs = directory / self.config['logs']
        self.logs.mkdir(parents=True, exist_ok=True)
        self.rx = self.logs / 'rx.jsonl'
        self.worker = None
        self.stop_signal = threading.Event()
        self.result = None
        self.live_run = ''
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
        self.links = {link: tk.BooleanVar(value=True) for link in fusion.LINKS}
        self.replay_links = {link: tk.BooleanVar(value=True) for link in fusion.LINKS}
        self.route_text = tk.StringVar()
        self.make_window()
        self.load_runs()
        self.update_routes()
        self.tick()

    def make_window(self):
        root = self.root
        root.title('YRT 三链路地面站')
        root.geometry('970x760')
        root.minsize(840, 670)
        root.configure(bg='#edf2f5')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.option_add('*Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame', background='#edf2f5')
        style.configure('TLabel', background='#edf2f5', foreground='#163147')
        style.configure('TCheckbutton', background='#edf2f5', foreground='#163147', padding=(3, 2))
        style.configure('TButton', padding=(10, 6))
        style.configure('Primary.TButton', background='#006f66', foreground='white')
        style.map('Primary.TButton', background=[('disabled', '#76938d'), ('active', '#008477')])
        style.configure('Treeview', font=('Microsoft YaHei UI', 10), rowheight=28,
                        background='white', fieldbackground='white', foreground='#163147')
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'), background='#dbe7ed')

        header = tk.Frame(root, bg='#143449', padx=26, pady=18)
        header.pack(fill='x')
        tk.Label(header, text='三链路地面站', font=('Microsoft YaHei UI', 21, 'bold'),
                 fg='white', bg='#143449').pack(anchor='w')
        tk.Label(header, text='LoRa / Wi-Fi 网桥 / 4G   ·   真机发送模拟指令',
                 fg='#c8dee9', bg='#143449').pack(anchor='w')

        body = ttk.Frame(root, padding=(22, 15))
        body.pack(fill='both', expand=True)
        form = ttk.Frame(body)
        form.pack(fill='x')
        for label, var in (('指令条数', self.count), ('频率 Hz', self.hz)):
            ttk.Label(form, text=label).pack(side='left', padx=(0, 5))
            ttk.Entry(form, textvariable=var, width=6).pack(side='left', padx=(0, 15))
        for link in fusion.LINKS:
            ttk.Checkbutton(form, text=NAME[link], variable=self.links[link]).pack(side='left', padx=(0, 5))
        self.settings_button = ttk.Button(form, text='接口设置', command=self.edit_settings)
        self.settings_button.pack(side='right')
        self.dial_button = ttk.Button(form, text='4G 拨号', command=self.dial_modem)
        self.dial_button.pack(side='right', padx=5)
        ttk.Label(body, textvariable=self.route_text).pack(anchor='w', pady=(8, 10))

        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=(0, 8))
        self.start_button = ttk.Button(actions, text='开始发包', style='Primary.TButton', command=self.start_run)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(actions, text='停止发包', command=self.stop_run, state='disabled')
        self.stop_button.pack(side='left', padx=6)
        ttk.Label(actions, text='实验', padding=(12, 0, 4, 0)).pack(side='left')
        self.run_list = ttk.Combobox(actions, textvariable=self.selected, width=18, state='readonly')
        self.run_list.pack(side='left')
        self.run_list.bind('<<ComboboxSelected>>', lambda _: self.render())
        ttk.Button(actions, text='打开日志目录', command=lambda: os.startfile(self.logs)).pack(side='right')

        self.tx_table = self.table(body, ('链路', '已提交', '提交失败', '最近一次失败'), (110, 85, 85, 460), 3)
        ttk.Label(body, text='已提交表示交给本机接口。车端是否收到，以接收日志为准。').pack(anchor='w', pady=(4, 10))

        receive = ttk.Frame(body)
        receive.pack(fill='x')
        ttk.Label(receive, text='车端接收', font=('Microsoft YaHei UI', 12, 'bold')).pack(side='left')
        ttk.Button(receive, text='导出 CSV', command=self.export).pack(side='right')
        ttk.Button(receive, text='导入日志', command=self.import_log).pack(side='right', padx=5)
        self.fetch_button = ttk.Button(receive, text='从 RK3588 取日志', command=self.fetch_log)
        self.fetch_button.pack(side='right')
        self.rx_status = tk.StringVar()
        ttk.Label(body, textvariable=self.rx_status).pack(anchor='w', pady=(7, 5))
        tabs = ttk.Notebook(body)
        tabs.pack(fill='both', expand=True)
        arrivals, replay = ttk.Frame(tabs, padding=6), ttk.Frame(tabs, padding=6)
        tabs.add(arrivals, text='接收明细')
        tabs.add(replay, text='回放')
        ttk.Label(arrivals, text='同序号首份质量为 1.000，副本晚到越久分越低。基于车端计时。').pack(anchor='w', pady=(0, 5))
        self.arrivals_table = self.table(arrivals, ('到达顺序', '指令序号', '链路', '指令值', '车端处理', '比首份晚 ms', '质量 0–1'),
                                         (86, 86, 104, 70, 96, 112, 110), 6, scroll=True)
        replay_scope = ttk.Frame(replay)
        replay_scope.pack(fill='x', pady=(0, 6))
        ttk.Label(replay_scope, text='参与回放').pack(side='left', padx=(0, 8))
        for link in fusion.LINKS:
            ttk.Checkbutton(replay_scope, text=NAME[link], variable=self.replay_links[link],
                            command=self.render).pack(side='left', padx=(0, 5))
        self.replay_table = self.table(replay, ('回放范围', '有效更新', '最长更新空档', '采用来源'), (230, 90, 145, 340), 2)
        ttk.Separator(body).pack(fill='x', pady=(12, 6))
        ttk.Label(body, textvariable=self.status, wraplength=720).pack(fill='x', anchor='w')

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
        self.route_text.set(f"{cfg['serial']} · 9600    |    {cfg['wifi_peer']}:{cfg['wifi_port']} / UDP"
                            f"    |    {cfg['ecs']}:{cfg['lte_port']} / TCP，接口 {cfg['lte_interface']}")

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
        if self.arrival_view != source:
            self.fill(self.arrivals_table, [])
            self.arrival_view = source
            self.arrival_count = 0
        for row in fusion.arrival_details(rows):
            order = row.get('rx_order', 0)
            if order > self.arrival_count:
                self.arrivals_table.insert('', 'end', values=(order, row['seq'], NAME[row['link']], row['value'],
                    OUTCOME[row['outcome']], f"{row['relative_delay_ms']:.1f}", f"{row['quality_0_1']:.3f}"))
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
            ttk.Entry(dialog, textvariable=fields[key], width=24).grid(row=i, column=1, pady=3)

        def save():
            updated = dict(self.config, **{key: value.get().strip() for key, value in fields.items()})
            try:
                for key in ('wifi_ip', 'wifi_peer', 'ecs', 'lte_ip'):
                    ipaddress.IPv4Address(updated[key])
                for key in ('wifi_port', 'lte_port', 'lte_interface'):
                    updated[key] = int(updated[key])
                    if not 1 <= updated[key] <= 65535:
                        raise ValueError('端口或接口索引超出范围。')
                if not updated['serial'] or not updated['lte_at'] or updated['serial'].upper() == updated['lte_at'].upper():
                    raise ValueError('请为 LoRa 和 EC200A 填入各自的串口。')
                self.settings.write_text(json.dumps(updated, indent=2) + '\n', encoding='utf-8')
            except (OSError, ValueError) as error:
                messagebox.showerror('保存设置失败', str(error), parent=dialog)
                return
            self.config = updated
            self.update_routes()
            dialog.destroy()

        ttk.Button(dialog, text='保存', style='Primary.TButton', command=save).grid(row=len(fields), column=1, sticky='e', pady=(10, 0))

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
        try:
            run = fusion.reserve_run(self.logs)
        except OSError as error:
            self.status.set(f'无法创建实验 · {error}')
            return
        self.live_run = fusion.format_run(run)
        self.live_rows = []
        self.selected.set(self.live_run)
        self.stop_signal.clear()
        self.result = None
        config = dict(self.config, logs=str(self.logs))

        def send():
            try:
                fusion.transmit(config, hz, range(1, count + 1), omitted, run=run, stop=self.stop_signal)
                # Let the LoRa radio deliver its queued end frame before offering the log fetch.
                if 'lora' not in omitted:
                    time.sleep(2)
            except Exception as error:
                self.result = str(error)

        self.start_live_feed()
        self.worker = threading.Thread(target=send)
        self.worker.start()
        self.start_button.configure(state='disabled')
        self.settings_button.configure(state='disabled')
        self.dial_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.fetch_button.configure(state='disabled')
        self.status.set(f'正在发包 · {self.live_run}')
        self.load_runs()

    def stop_run(self):
        self.stop_signal.set()
        self.stop_button.configure(state='disabled')
        self.status.set('已停止生成新指令，正在保存本轮日志。')

    def dial_modem(self):
        port = self.config['lte_at']

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
            self.status.set(f'已导入 {len(rows)} 条接收记录 · {datetime.now():%H:%M:%S}')
            self.render()
            return True
        except (OSError, ValueError, TypeError) as error:
            self.status.set(f'导入失败 · {error}')
            return False

    def fetch_log(self, automatic=False):
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

    def tick(self):
        self.pull_live_rows()
        if self.worker is not None:
            if self.worker.is_alive():
                self.render_tx()
            else:
                self.worker = None
                self.stop_live_feed()
                self.start_button.configure(state='normal')
                self.settings_button.configure(state='normal')
                self.dial_button.configure(state='normal')
                self.stop_button.configure(state='disabled')
                if self.fetch is None:
                    self.fetch_button.configure(state='normal')
                self.status.set(f'测试失败 · {self.result}' if self.result else f'本轮已保存 · {self.live_run}')
                self.load_runs()
                if self.fetch is None and not self.result:
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
