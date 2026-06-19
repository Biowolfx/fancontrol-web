"""SSDP discovery — listens for agent broadcasts on LAN."""

import logging
import socket
import threading
import time
from typing import Dict, List

logger = logging.getLogger('fancontrol')

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
DISCOVERY_TIMEOUT = 5

_discovered_nodes: Dict[str, Dict] = {}
_lock = threading.Lock()


def scan_for_agents(timeout: int = DISCOVERY_TIMEOUT) -> List[Dict]:
    global _discovered_nodes

    with _lock:
        _discovered_nodes.clear()

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)

        msearch = (
            'M-SEARCH * HTTP/1.1\r\n'
            'HOST: 239.255.255.250:1900\r\n'
            'MAN: "ssdp:discover"\r\n'
            'ST: urn:fancontrol-web:agent\r\n'
            'MX: 3\r\n'
            '\r\n'
        )
        sock.sendto(msearch.encode(), (SSDP_ADDR, SSDP_PORT))

        start = time.time()
        while time.time() - start < timeout:
            try:
                data, addr = sock.recvfrom(1024)
                _parse_response(data.decode(errors='ignore'), addr[0])
            except socket.timeout:
                break

        sock.close()
    except Exception as e:
        logger.error(f'Discovery scan failed: {e}')

    with _lock:
        return list(_discovered_nodes.values())


def _parse_response(data: str, source_ip: str):
    global _discovered_nodes

    headers = {}
    for line in data.split('\r\n'):
        if ':' in line:
            key, _, value = line.partition(':')
            headers[key.strip().upper()] = value.strip()

    usn = headers.get('USN', '')
    if 'urn:fancontrol-web:agent:' not in usn:
        return

    node_id = usn.split('urn:fancontrol-web:agent:')[-1]
    node_name = headers.get('X-FANCONTROL-NAME', node_id)
    location = headers.get('LOCATION', f'http://{source_ip}:5059')

    with _lock:
        _discovered_nodes[node_id] = {
            'node_id': node_id,
            'name': node_name,
            'ip': source_ip,
            'location': location,
        }


def get_discovered_nodes() -> List[Dict]:
    with _lock:
        return list(_discovered_nodes.values())
