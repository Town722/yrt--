"""Relay the freshest PC input and vehicle challenge. No commands are queued."""

from collections import deque
import threading
import time

from control_protocol import LIMIT, LTE_MAX_AGE, PREFIX, identifier, valid_frame


condition = threading.Condition()
challenges = deque(maxlen=32)
latest = None
status = None
pc_seen = -10


def handle_control(handler, route, authorized):
    global latest, status, pc_seen
    role = route.path.removeprefix(PREFIX)
    if role not in ('pc', 'vehicle'):
        return handler.send_json(404, {'error': 'unknown control route'})
    if not authorized(handler, role):
        handler.close_connection = True
        return handler.send_json(401, {'error': 'unauthorized'})
    try:
        if not 1 <= int(handler.headers.get('Content-Length', 0)) <= LIMIT:
            handler.close_connection = True
            raise ValueError
        body = handler.read_json()
        nonce = body.get('nonce')
        if role == 'vehicle':
            if not identifier(nonce) or not isinstance(body.get('state'), str):
                raise ValueError
        elif nonce is not None and (not identifier(nonce) or not valid_frame(body.get('frame'))):
            raise ValueError
    except (ValueError, TypeError):
        return handler.send_json(400, {'error': 'invalid control message'})

    with condition:
        now = time.monotonic()
        while challenges and now - challenges[0][1] >= LTE_MAX_AGE:
            challenges.popleft()
        if latest is not None and now - latest[2] >= LTE_MAX_AGE:
            latest = None
        if role == 'vehicle':
            challenges.append((nonce, now))
            status = body['state'][:32], now
            reply = {'active': now - pc_seen < 1}
            if reply['active']:
                condition.wait_for(lambda: latest is not None and latest[2] >= now, 0.12)
            if latest:
                reply.update(nonce=latest[0], frame=latest[1])
        else:
            pc_seen = now
            if nonce is not None and any(candidate == nonce for candidate, _ in challenges):
                frame = body['frame']
                if (latest is None or (frame['session'] == latest[1]['session']
                                       and frame['seq'] > latest[1]['seq'])):
                    latest = nonce, frame, now
                    condition.notify_all()
            reply = {'nonce': challenges[-1][0] if challenges else None,
                     'state': status[0] if status and now - status[1] < LTE_MAX_AGE else 'offline'}
    return handler.send_json(200, reply)
