"""SSDP announcer — broadcasts agent presence on LAN."""

import logging
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger('fancontrol')

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
SSDP_INTERVAL = 60


def _build_ssdp_response(node_id: str, node_name: str, port: int = 5059, api_token: str = '') -> str:
    ip = _get_local_ip()
    return (
        'HTTP/1.1 200 OK\r\n'
        'CACHE-CONTROL: max-age=60\r\n'
        'EXT: \r\n'
        f'LOCATION: http://{ip}:{port}\r\n'
        'SERVER: FanControl-Web/3.4.1\r\n'
        f'USN: urn:fancontrol-web:agent:{node_id}\r\n'
        'ST: urn:fancontrol-web:agent\r\n'
        f'X-FanControl-Name: {node_name}\r\n'
        f'X-FanControl-Id: {node_id}\r\n'
        f'X-FanControl-Token: {api_token}\r\n'
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


def start_announcer(node_id: str, node_name: str, port: int = 5059, api_token: str = '') -> Optional[threading.Thread]:
    def _announce_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            response = _build_ssdp_response(node_id, node_name, port, api_token)

            logger.info(f'SSDP announcer started for {node_name}')

            while True:
                try:
                    sock.sendto(response.encode(), (SSDP_ADDR, SSDP_PORT))
                except Exception as e:
                    logger.debug(f'SSDP send failed: {e}')
                time.sleep(SSDP_INTERVAL)
        except Exception as e:
            logger.error(f'SSDP announcer error: {e}')

    thread = threading.Thread(target=_announce_loop, daemon=True)
    thread.start()
    return thread


def _handle_msearch(node_id: str, node_name: str, port: int = 5059):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', SSDP_PORT))

        mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton('0.0.0.0')
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        response = _build_ssdp_response(node_id, node_name, port)

        while True:
            data, addr = sock.recvfrom(1024)
            if b'M-SEARCH' in data:
                sock.sendto(response.encode(), addr)
    except Exception as e:
        logger.error(f'SSDP listener error: {e}')
