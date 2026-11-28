"""Desktop input worker. Tk objects are owned by the video window."""

import http.client
import ipaddress
import json
import secrets
import shutil
import subprocess
from tempfile import TemporaryFile
import threading

from control_protocol import LIMIT, ControlHTTPS, identifier
from controller_probe import State, open_xinput, read_gamepad


STATE_TEXT = {
    'offline': '等待车端遥控服务',
    'usb_offline': '3588 未连接底盘 USB',
    'busy': '车端正由另一条链路控制',
    'locked': '摇杆回中，按原装解锁键',
    'armed': '遥控已解锁',
}


class RemoteControl:
    def __init__(self, lte, config):
        self.config = config.copy()
        self.lte = lte
        self.stop = threading.Event()
        self.process = None
        self.status = '正在连接遥控…'
        self.session = secrets.token_hex(16)
        self.thread = threading.Thread(target=self.run, name='yrt-remote-control', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(self):
        try:
            self.get_state, self.state = open_xinput(), State()
            slots = [slot for slot in range(4) if read_gamepad(self.get_state, self.state, slot)[0]]
            if len(slots) != 1:
                raise OSError('请只连接一个原装遥控器接收器')
            self.slot = slots[0]
            if self.lte:
                self.gateway()
            else:
                self.bridge()
        except (OSError, ValueError, http.client.HTTPException) as error:
            if not self.stop.is_set():
                self.status = str(error)

    def frame(self, seq, state):
        connected, values, unlock = read_gamepad(self.get_state, self.state, self.slot, include_triggers=True)
        self.status = STATE_TEXT.get(state, STATE_TEXT['offline']) if connected else '遥控器已断开'
        if not connected or self.stop.is_set():
            values, unlock = [0] * 6, False
        return {'seq': seq, 'session': self.session, 'connected': connected and not self.stop.is_set(),
                'input': values, 'unlock': unlock}

    def bridge(self):
        ssh = shutil.which('ssh.exe')
        if not ssh:
            raise OSError('Windows 未安装 OpenSSH 客户端')
        host = str(ipaddress.IPv4Address(self.config['wifi_peer']))
        command = [ssh, '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4',
                   '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=2',
                   'ztl@' + host, 'exec python3 /opt/yrt-remote-control/control_receiver.py bridge']
        with TemporaryFile() as errors:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=errors, creationflags=subprocess.CREATE_NO_WINDOW) as process:
                self.process = process
                try:
                    while not self.stop.is_set():
                        line = process.stdout.readline(LIMIT + 1)
                        if not line or len(line) > LIMIT:
                            break
                        reply = json.loads(line)
                        if not isinstance(reply, dict) or type(reply.get('seq')) is not int:
                            raise OSError('车端返回了无效的遥控请求')
                        data = json.dumps(self.frame(reply['seq'], reply.get('state')))
                        process.stdin.write((data + '\n').encode('ascii'))
                        process.stdin.flush()
                finally:
                    if process.poll() is None:
                        process.terminate()
                    self.process = None
            if not self.stop.is_set():
                errors.seek(0)
                lines = errors.read(2048).decode('utf-8', 'replace').strip().splitlines()
                raise OSError('网桥遥控已断开' + (f'，{lines[-1]}' if lines else ''))

    def gateway(self):
        link = ControlHTTPS(self.config.get('gateway_url', ''), self.config.get('gateway_token', ''))
        nonce, state = None, 'offline'
        seq = 0
        try:
            while not self.stop.is_set():
                seq += 1
                data = {'nonce': nonce, 'frame': self.frame(seq, state)} if nonce else {}
                reply = link.exchange('pc', data)
                if not isinstance(reply, dict):
                    raise OSError('4G 网关返回了无效的遥控状态')
                nonce, state = reply.get('nonce'), reply.get('state')
                if not identifier(nonce):
                    nonce = None
                    self.status = STATE_TEXT['offline']
                self.stop.wait(0.035)
        finally:
            link.close()
