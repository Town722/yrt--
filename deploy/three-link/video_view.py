"""Native ground-station video preview. Tk only runs on the UI thread."""

import http.client
from io import BytesIO
import shutil
import subprocess
from tempfile import TemporaryFile
import threading
import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from remote_control import RemoteControl
from video_transport import VideoHTTPS, jpeg_frames, video_frames


BRIDGE = 'Wi-Fi 网桥'
LTE = '4G 低延迟'


class Preview:
    def __init__(self, mode, config):
        self.mode = mode
        self.config = config.copy()
        self.stop = threading.Event()
        self.process = None
        self.frame = None
        self.captured_ns = None
        self.status = '正在连接…'
        threading.Thread(target=self.read, name='yrt-video', daemon=True).start()

    def close(self):
        self.stop.set()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                # EOF on SSH stdin makes the vehicle release the camera.
                process.stdin.close()
                process.terminate()
            except OSError:
                pass

    def read(self):
        while not self.stop.is_set():
            try:
                if self.mode == BRIDGE:
                    self.bridge()
                else:
                    self.lte()
            except (OSError, ValueError, http.client.HTTPException) as error:
                self.status = str(error)
            if self.stop.wait(1):
                return

    def show(self, image, captured_ns=None):
        self.captured_ns = captured_ns
        self.frame = time.monotonic(), image
        self.status = '实时画面'

    def bridge(self):
        ssh = shutil.which('ssh.exe')
        if not ssh:
            raise OSError('Windows 未安装 OpenSSH 客户端')
        dest = 'ztl@' + self.config['wifi_peer']
        command = [ssh, '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', '-o',
                   'ServerAliveInterval=3', '-o', 'ServerAliveCountMax=2', dest,
                   'exec python3 "$HOME/.local/lib/yrt-camera/vehicle_camera.py" bridge']
        with TemporaryFile() as errors:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                  creationflags=subprocess.CREATE_NO_WINDOW) as process:
                self.process = process
                try:
                    for jpeg in jpeg_frames(process.stdout):
                        if self.stop.is_set():
                            return
                        self.show(jpeg)
                finally:
                    if process.poll() is None:
                        process.terminate()
                    self.process = None
            if not self.stop.is_set():
                errors.seek(0)
                detail = errors.read(4096).decode('utf-8', 'replace').strip().splitlines()
                raise OSError(detail[-1] if detail else '网桥画面已断开，正在重连')

    def lte(self):
        import av

        link = VideoHTTPS(self.config.get('gateway_url', ''), self.config.get('gateway_token', ''))
        decoder = None
        try:
            link.conn.request('GET', link.path + 'h264/watch', headers={'Authorization': link.authorization})
            with link.conn.getresponse() as response:
                if response.status != 200:
                    raise OSError(f'4G 视频连接失败（HTTP {response.status}）')
                self.status = '等待车端 H.264 画面…'
                for payload, captured_ns, key in video_frames(response):
                    if self.stop.is_set():
                        return
                    if not payload:
                        continue
                    if decoder is None:
                        if not key:
                            continue
                        decoder = av.CodecContext.create('h264', 'r')
                        decoder.thread_count = 1
                        decoder.options = {'flags': 'low_delay'}
                    try:
                        for picture in decoder.decode(av.Packet(payload)):
                            self.show(picture.to_image(), captured_ns)
                    except av.error.FFmpegError:
                        decoder = None
                raise OSError('4G 图传已断开，正在重连')
        finally:
            link.close()


class VideoWindow(tk.Toplevel):
    def __init__(self, root, settings, palette):
        super().__init__(root)
        self.settings = settings
        self.palette = palette
        self.title('车载图传与遥控 · YRT')
        self.geometry('760x610')
        self.minsize(480, 360)
        self.configure(bg=palette['paper'])
        self.protocol('WM_DELETE_WINDOW', self.close)
        controls = ttk.Frame(self, padding=(14, 10))
        controls.pack(fill='x')
        ttk.Label(controls, text='链路').pack(side='left', padx=(0, 8))
        self.mode = tk.StringVar(value=BRIDGE)
        selector = ttk.Combobox(controls, textvariable=self.mode, values=(BRIDGE, LTE), width=15, state='readonly')
        selector.pack(side='left')
        selector.bind('<<ComboboxSelected>>', self.switch)
        self.start_button = ttk.Button(controls, text='开始预览', command=self.start, style='Primary.TButton')
        self.start_button.pack(side='left', padx=(12, 6))
        self.stop_button = ttk.Button(controls, text='停止预览', command=self.stop, state='disabled')
        self.stop_button.pack(side='left')
        self.status = tk.StringVar(value='已停止')
        ttk.Label(self, textvariable=self.status, padding=(14, 8), wraplength=650).pack(side='bottom', fill='x')
        operator = ttk.Frame(self, padding=(14, 0, 14, 8))
        operator.pack(fill='x')
        self.control_start = ttk.Button(operator, text='开始遥控', command=self.start_control, state='disabled')
        self.control_start.pack(side='left')
        self.control_stop = ttk.Button(operator, text='停止遥控', command=self.stop_control, state='disabled')
        self.control_stop.pack(side='left', padx=(6, 10))
        self.control_status = tk.StringVar(value='遥控已停止')
        ttk.Label(operator, textvariable=self.control_status, wraplength=335).pack(side='left', fill='x', expand=True)
        self.canvas = tk.Canvas(self, bg=palette['navy'], highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=14)
        self.canvas.bind('<Configure>', self.resize)
        self.message = self.canvas.create_text(0, 0, text='选择链路后开始预览', fill=palette['soft_blue'])
        self.picture = self.canvas.create_image(0, 0)
        self.session = None
        self.remote = None
        self.photo = self.displayed = None
        self.timer = self.after(33, self.refresh)

    def resize(self, event):
        self.canvas.coords(self.message, event.width / 2, event.height / 2)
        self.canvas.coords(self.picture, event.width / 2, event.height / 2)
        self.displayed = None

    def switch(self, _):
        if self.session:
            self.start()

    def start(self):
        self.stop()
        self.session = Preview(self.mode.get(), self.settings())
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')

    def stop(self):
        self.stop_control()
        if self.session:
            self.session.close()
            self.session = None
        self.clear()
        self.status.set('已停止')
        self.start_button.configure(state='normal')
        self.stop_button.configure(state='disabled')

    def has_live_picture(self):
        sample = self.session.frame if self.session else None
        limit = 0.55 if self.mode.get() == LTE else 1.0
        return bool(sample and time.monotonic() - sample[0] <= limit and self.photo is not None)

    def start_control(self):
        if self.remote is None and self.has_live_picture():
            self.remote = RemoteControl(self.mode.get() == LTE, self.settings())
            self.control_status.set(self.remote.status)
            self.control_start.configure(state='disabled')
            self.control_stop.configure(state='normal')

    def stop_control(self, message='遥控已停止'):
        if self.remote:
            self.remote.close()
            self.remote = None
        self.control_status.set(message)
        self.control_start.configure(state='disabled')
        self.control_stop.configure(state='disabled')

    def clear(self):
        self.canvas.itemconfigure(self.picture, image='')
        self.canvas.itemconfigure(self.message, state='normal')
        self.photo = self.displayed = None

    def refresh(self):
        if self.session:
            state = self.session.status
            sample = self.session.frame
            expiry = .55 if self.mode.get() == LTE else 2.5
            if sample is None or time.monotonic() - sample[0] > expiry:
                if self.photo is not None:
                    self.clear()
                if state == '实时画面':
                    state = '画面中断，等待新帧…'
                self.canvas.itemconfigure(self.message, text=state)
            elif sample is not self.displayed:
                self.displayed = sample
                try:
                    value = sample[1]
                    with (value.copy() if isinstance(value, Image.Image) else Image.open(BytesIO(value), formats=('JPEG',))) as image:
                        if image.width > 1280 or image.height > 720:
                            raise ValueError('视频帧尺寸超出限制')
                        image.thumbnail((max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())))
                        self.photo = ImageTk.PhotoImage(image, master=self)
                    self.canvas.itemconfigure(self.picture, image=self.photo)
                    self.canvas.itemconfigure(self.message, state='hidden')
                except (OSError, ValueError) as error:
                    state = str(error)
            self.status.set(state)
        else:
            self.canvas.itemconfigure(self.message, text='选择链路后开始预览')
        live = self.has_live_picture()
        if self.remote:
            if not live:
                self.stop_control('图传中断，遥控已停止')
            elif not self.remote.thread.is_alive():
                self.stop_control(self.remote.status)
            else:
                self.control_status.set(self.remote.status)
        self.control_start.configure(state='normal' if live and self.remote is None else 'disabled')
        self.timer = self.after(33, self.refresh)

    def close(self):
        self.after_cancel(self.timer)
        self.stop()
        self.destroy()
