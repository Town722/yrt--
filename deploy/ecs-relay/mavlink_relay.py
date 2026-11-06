#!/usr/bin/env python3
"""Relay one vehicle and one ground station's MAVLink byte streams over TCP."""

import asyncio
import logging
import os
import socket


BIND = os.environ.get("RELAY_BIND", "127.0.0.1")
AIR_PORT = int(os.environ.get("AIR_PORT", "14551"))
GCS_PORT = int(os.environ.get("GCS_PORT", "14550"))


class Relay:
    def __init__(self):
        self.peers = [None, None]

    async def handle(self, side, reader, writer):
        role = ("vehicle", "ground station")[side]
        previous = self.peers[side]
        self.peers[side] = writer
        if previous is not None:
            previous.close()
        writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        logging.info("%s connected", role)
        try:
            while self.peers[side] is writer and (data := await reader.read(4096)):
                if self.peers[side] is not writer:
                    break
                peer = self.peers[1 - side]
                if peer is None or peer.is_closing():
                    continue
                try:
                    peer.write(data)
                    await asyncio.wait_for(peer.drain(), timeout=3)
                except (OSError, TimeoutError):
                    peer.close()
        except OSError:
            logging.info("%s connection lost", role)
        finally:
            if self.peers[side] is writer:
                self.peers[side] = None
            writer.close()
            logging.info("%s disconnected", role)


async def main():
    relay = Relay()
    air = await asyncio.start_server(lambda r, w: relay.handle(0, r, w), BIND, AIR_PORT)
    async with air:
        gcs = await asyncio.start_server(lambda r, w: relay.handle(1, r, w), BIND, GCS_PORT)
        async with gcs:
            logging.info("listening on %s TCP %d (vehicle) and %d (ground station)", BIND, AIR_PORT, GCS_PORT)
            await asyncio.gather(air.serve_forever(), gcs.serve_forever())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
