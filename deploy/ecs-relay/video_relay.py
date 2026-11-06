#!/usr/bin/env python3
"""Low-latency one-publisher/multi-viewer TCP MPEG-TS relay."""

import logging
import os
import queue
import signal
import socket
import threading
import time


BIND = os.environ.get("RELAY_BIND", "127.0.0.1")
PUBLISH_PORT = 15554
VIEW_PORT = 8554
CHUNK_SIZE = 64 * 1024
VIEW_QUEUE_SIZE = 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

stop_event = threading.Event()
viewers_lock = threading.Lock()
viewers = set()


class Viewer:
    def __init__(self, conn, address):
        self.conn = conn
        self.address = address
        self.queue = queue.Queue(maxsize=VIEW_QUEUE_SIZE)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._send_loop, daemon=True)

    def start(self):
        self.thread.start()

    def enqueue(self, data):
        if self.closed.is_set():
            return
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            # Keep latency bounded: discard old queued video rather than
            # delaying the live stream.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(data)
            except queue.Full:
                self.close()

    def _send_loop(self):
        try:
            self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.conn.settimeout(5)
            while not stop_event.is_set() and not self.closed.is_set():
                try:
                    data = self.queue.get(timeout=1)
                except queue.Empty:
                    continue
                self.conn.sendall(data)
        except (ConnectionError, OSError) as exc:
            logging.info("viewer %s disconnected: %s", self.address, exc)
        finally:
            self.close()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass
        with viewers_lock:
            viewers.discard(self)
        logging.info("viewer %s closed", self.address)


def make_server(port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BIND, port))
    server.listen(8)
    server.settimeout(1)
    return server


def viewer_listener(server):
    logging.info("viewer port listening on TCP %d", VIEW_PORT)
    try:
        while not stop_event.is_set():
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            viewer = Viewer(conn, address)
            with viewers_lock:
                viewers.add(viewer)
            logging.info("viewer connected: %s", address)
            viewer.start()
    finally:
        server.close()


def publish_listener(server):
    logging.info("publisher port listening on TCP %d", PUBLISH_PORT)
    try:
        while not stop_event.is_set():
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            logging.info("publisher connected: %s", address)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(5)
            byte_count = 0
            started = time.monotonic()
            try:
                while not stop_event.is_set():
                    data = conn.recv(CHUNK_SIZE)
                    if not data:
                        break
                    byte_count += len(data)
                    with viewers_lock:
                        current_viewers = tuple(viewers)
                    for viewer in current_viewers:
                        viewer.enqueue(data)
            except (ConnectionError, OSError) as exc:
                logging.info("publisher %s disconnected: %s", address, exc)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                elapsed = max(time.monotonic() - started, 0.001)
                logging.info(
                    "publisher %s closed, %.2f MiB, average %.2f Mbit/s",
                    address,
                    byte_count / 1024 / 1024,
                    byte_count * 8 / elapsed / 1_000_000,
                )
    finally:
        server.close()


def shutdown(_signum=None, _frame=None):
    stop_event.set()
    with viewers_lock:
        current_viewers = tuple(viewers)
    for viewer in current_viewers:
        viewer.close()


def main():
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    with make_server(VIEW_PORT) as view_server, make_server(PUBLISH_PORT) as publish_server:
        viewer_thread = threading.Thread(target=viewer_listener, args=(view_server,), daemon=True)
        viewer_thread.start()
        try:
            publish_listener(publish_server)
        finally:
            shutdown()
            viewer_thread.join(timeout=2)


if __name__ == "__main__":
    main()
