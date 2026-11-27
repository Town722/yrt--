"""RK3588 hardware H.264 on a continuous HTTPS upload."""

from contextlib import contextmanager
import socket
import time

from video_transport import VideoHTTPS, video_packet


@contextmanager
def hardware_encoder(device):
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstApp', '1.0')
    from gi.repository import Gst, GstApp

    Gst.init(None)
    escaped_device = device.replace('\\', '\\\\').replace('"', '\\"')
    pipeline = Gst.parse_launch(
        f'v4l2src device="{escaped_device}" io-mode=2 ! '
        'image/jpeg,width=1280,height=720,framerate=30/1 ! jpegdec idct-method=1 ! '
        'videoconvert ! video/x-raw,format=NV12 ! videorate drop-only=true ! '
        'video/x-raw,framerate=20/1 ! queue leaky=downstream max-size-buffers=1 '
        'max-size-bytes=0 max-size-time=0 ! mpph264enc rc-mode=cbr bps=1200000 bps-max=1280000 '
        'gop=10 max-pending=1 max-reenc=0 profile=baseline header-mode=each-idr ! '
        'h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! '
        'appsink name=out sync=false max-buffers=2 drop=false'
    )
    sink = pipeline.get_by_name('out')
    bus = pipeline.get_bus()
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        pipeline.set_state(Gst.State.NULL)
        raise OSError('无法启动 RK3588 H.264 硬件编码器')

    def read():
        sample = GstApp.AppSink.try_pull_sample(sink, 250 * Gst.MSECOND)
        if sample is None:
            event = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if event:
                if event.type == Gst.MessageType.ERROR:
                    raise OSError(str(event.parse_error()[0]))
                raise OSError('摄像头停止输出')
            return None
        buffer = sample.get_buffer()
        key = not buffer.has_flags(Gst.BufferFlags.DELTA_UNIT)
        age = pipeline.get_clock().get_time() - pipeline.get_base_time() - buffer.pts
        stamp = time.time_ns() - max(0, age) if age < 10 * Gst.SECOND else time.time_ns()
        return buffer.extract_dup(0, buffer.get_size()), stamp, key

    try:
        yield read
    finally:
        pipeline.set_state(Gst.State.NULL)


def stream(base, token, connection, device, stop):
    link = VideoHTTPS(base, token, connection)
    conn = link.conn
    try:
        conn.putrequest('POST', link.path + 'h264/push')
        conn.putheader('Authorization', link.authorization)
        conn.putheader('Content-Type', 'application/octet-stream')
        conn.putheader('Transfer-Encoding', 'chunked')
        conn.endheaders()
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
        conn.sock.settimeout(.55)
        with hardware_encoder(device) as read:
            updated = time.monotonic()
            while not stop.is_set():
                sample = read()
                if sample is None:
                    if time.monotonic() - updated > 3:
                        raise OSError('摄像头没有输出 H.264 画面')
                    continue
                updated = time.monotonic()
                if time.time_ns() - sample[1] > 300_000_000:
                    raise OSError('4G 上行积压，正在恢复实时画面')
                packet = video_packet(*sample)
                conn.send(f'{len(packet):x}\r\n'.encode() + packet + b'\r\n')
    finally:
        link.close()
