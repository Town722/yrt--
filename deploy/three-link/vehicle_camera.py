"""RK3588 USB camera capture for the bridge and LTE viewer."""

import argparse
from contextlib import contextmanager
import fcntl
import http.client
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from video_transport import VideoHTTPS, jpeg_frames
import h264_capture


STOP = threading.Event()


def camera_device():
    configured = os.environ.get('YRT_CAMERA_DEVICE')
    if configured:
        return configured
    candidates = sorted(Path('/dev/v4l/by-id').glob('*-video-index0'))
    astra = [path for path in candidates if 'Astra_Pro_HD_Camera' in path.name]
    if len(astra or candidates) != 1:
        raise OSError('未找到唯一的 USB 摄像头，请检查连接或设置 YRT_CAMERA_DEVICE')
    return str((astra or candidates)[0])


@contextmanager
def camera_lock():
    cache = Path.home() / '.cache' / 'yrt-camera'
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (cache / 'camera.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OSError('摄像头正被另一路图传占用，请重试') from error
        yield


@contextmanager
def capture(width, height):
    with camera_lock():
        command = ['v4l2-ctl', '--silent', '--device', camera_device(),
                   f'--set-fmt-video=width={width},height={height},pixelformat=MJPG', '--set-parm=15',
                   '--stream-mmap=3', '--stream-to=-']
        with subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL) as process:
            try:
                yield process
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()


def bridge():
    with capture(640, 480) as process:
        def close_on_ssh_exit():
            os.read(0, 1)
            STOP.set()
            if process.poll() is None:
                process.terminate()

        threading.Thread(target=close_on_ssh_exit, daemon=True).start()
        for frame in jpeg_frames(process.stdout):
            if STOP.is_set():
                return
            sys.stdout.buffer.write(frame)
            sys.stdout.buffer.flush()
        if not STOP.is_set():
            raise OSError('摄像头已断开或未输出 MJPEG')


def publish(link):
    latest = None
    with capture(320, 240) as process:
        capture_started = time.monotonic()
        def keep_latest():
            nonlocal latest
            for frame in jpeg_frames(process.stdout):
                latest = time.monotonic(), frame

        reader = threading.Thread(target=keep_latest, daemon=True)
        reader.start()
        next_demand = next_upload = last_timestamp = 0
        try:
            while not STOP.is_set() and reader.is_alive():
                now = time.monotonic()
                if now >= next_demand:
                    if link.request('GET', 'demand')[0] != 200:
                        return
                    next_demand = now + 1
                sample = latest
                if sample is not None and sample[0] != last_timestamp and now >= next_upload:
                    last_timestamp, jpeg = sample
                    # About 1 Mbit/s at most, with up to five new frames per second.
                    next_upload = now + max(0.20, len(jpeg) / 120_000)
                    if link.request('POST', 'frame', jpeg, 'image/jpeg')[0] == 409:
                        return
                if now - (sample[0] if sample else capture_started) > 4:
                    raise OSError('摄像头超过 4 秒没有输出新帧')
                STOP.wait(.03)
        finally:
            if process.poll() is None:
                process.terminate()
            reader.join(timeout=2)
        if not STOP.is_set():
            raise OSError('摄像头已断开或未输出 MJPEG')


def lte():
    sys.path.insert(0, '/opt/yrt-mobile-gateway')
    import vehicle_agent

    vehicle = vehicle_agent.Agent()
    if not vehicle_agent.LTE_INTERFACE:
        raise ValueError('车端 4G 网口尚未配置')
    link = VideoHTTPS(vehicle.base, vehicle.token, vehicle_agent.BoundHTTPSConnection)
    last_error = ''
    h264_available = True
    check_again = 0.0
    try:
        while not STOP.is_set():
            try:
                if h264_available or time.monotonic() >= check_again:
                    try:
                        h264 = link.request('GET', 'h264/demand')[0] == 200
                        h264_available = True
                    except OSError as error:
                        if 'HTTP 404' not in str(error):
                            raise
                        h264 = h264_available = False
                        check_again = time.monotonic() + 5
                    if h264:
                        with camera_lock():
                            h264_capture.stream(vehicle.base, vehicle.token, vehicle_agent.BoundHTTPSConnection,
                                                camera_device(), STOP)
                        continue
                if link.request('GET', 'demand')[0] == 200:
                    publish(link)
                last_error = ''
            except (OSError, ValueError, http.client.HTTPException) as error:
                if str(error) != last_error:
                    print(f'4G 图传：{error}', file=sys.stderr, flush=True)
                    last_error = str(error)
                link.close()
            STOP.wait(1)
    finally:
        link.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('bridge', 'lte'))
    args = parser.parse_args()
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    try:
        bridge() if args.mode == 'bridge' else lte()
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
