"""SSDP announcer — broadcasts server presence on LAN for agent auto-discovery."""

import logging
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger('fancontrol')

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
SSDP_INTERVAL = 60


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


def start_announcer(server_name: str, port: int = 5059) -> Optional[threading.Thread]:
    """Start SSDP broadcast for server discovery by agents."""
    def _announce_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            response = _build_ssdp_response(server_name, port)

            logger.info(f'SSDP server announcer started: {server_name}')

            while True:
                try:
                    sock.sendto(response.encode(), (SSDP_ADDR, SSDP_PORT))
                except Exception as e:
                    logger.debug(f'SSDP server announce failed: {e}')
                time.sleep(SSDP_INTERVAL)
        except Exception as e:
            logger.error(f'SSDP server announcer error: {e}')

    thread = threading.Thread(target=_announce_loop, daemon=True)
    thread.start()
    return thread
