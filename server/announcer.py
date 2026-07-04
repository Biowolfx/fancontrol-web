"""SSDP announcer — broadcasts server presence on LAN + responds to M-SEARCH."""

import logging
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger('fancontrol')

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
SSDP_INTERVAL = 60

# Track active stop events so we can restart announcer
_active_stop_events: list[threading.Event] = []


def _build_ssdp_response(server_name: str, port: int = 5059) -> str:
    ip = _get_local_ip()
    return (
        'HTTP/1.1 200 OK\r\n'
        'CACHE-CONTROL: max-age=60\r\n'
        'EXT: \r\n'
        f'LOCATION: http://{ip}:{port}\r\n'
        'SERVER: FanControl-Web/3.7.1\r\n'
        f'USN: urn:fancontrol-web:server:{ip}\r\n'
        'ST: urn:fancontrol-web:server\r\n'
        f'X-FanControl-Name: {server_name}\r\n'
        f'X-FanControl-Port: {port}\r\n'
        '\r\n'
    )


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((SSDP_ADDR, SSDP_PORT))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def stop_announcers():
    """Signal all active announcer threads to stop."""
    for evt in _active_stop_events:
        evt.set()
    _active_stop_events.clear()


def start_announcer(server_name: str, port: int = 5059) -> Optional[threading.Thread]:
    """Start SSDP broadcast for server discovery by agents."""
    stop_event = threading.Event()
    _active_stop_events.append(stop_event)

    def _announce_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            response = _build_ssdp_response(server_name, port)

            logger.info(f'SSDP server announcer started: {server_name}')

            while not stop_event.is_set():
                try:
                    sock.sendto(response.encode(), (SSDP_ADDR, SSDP_PORT))
                except Exception as e:
                    logger.debug(f'SSDP server announce failed: {e}')
                stop_event.wait(SSDP_INTERVAL)
        except Exception as e:
            logger.error(f'SSDP server announcer error: {e}')

    thread = threading.Thread(target=_announce_loop, daemon=True)
    thread.start()

    # Also start M-SEARCH responder so wizard/agents can actively discover this server
    _start_msearch_responder(server_name, port, stop_event)

    return thread


def _start_msearch_responder(server_name: str, port: int = 5059, stop_event: Optional[threading.Event] = None):
    """Listen for M-SEARCH queries and respond with server info."""
    if stop_event is None:
        stop_event = threading.Event()

    def _respond_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            sock.bind(('', SSDP_PORT))

            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton('0.0.0.0')
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1)

            response = _build_ssdp_response(server_name, port)
            logger.info('SSDP M-SEARCH responder started for server')

            while not stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                    decoded = data.decode(errors='ignore')
                    if 'M-SEARCH' in decoded:
                        # Check if the search is for our type
                        if 'urn:fancontrol-web:server' in decoded:
                            logger.debug(f'M-SEARCH from {addr[0]} — responding')
                            sock.sendto(response.encode(), addr)
                except socket.timeout:
                    continue
        except Exception as e:
            logger.error(f'SSDP M-SEARCH responder error: {e}')

    thread = threading.Thread(target=_respond_loop, daemon=True)
    thread.start()
