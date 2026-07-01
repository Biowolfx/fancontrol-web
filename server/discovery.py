"""SSDP discovery — listens for agent broadcasts on LAN."""

import logging
import socket
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List

logger = logging.getLogger('fancontrol')

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
DISCOVERY_TIMEOUT = 5

_discovered_nodes: Dict[str, Dict] = {}
_lock = threading.Lock()


def scan_for_agents(timeout: int = DISCOVERY_TIMEOUT) -> List[Dict]:
    """Send M-SEARCH and collect responses. Preserves existing discovered nodes."""
    logger.info('Starting SSDP M-SEARCH scan...')

    found = []

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
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
        logger.info('M-SEARCH sent to 239.255.255.250:1900')

        start = time.time()
        while time.time() - start < timeout:
            try:
                data, addr = sock.recvfrom(1024)
                decoded = data.decode(errors='ignore')
                logger.debug(f'SSDP response from {addr[0]}: {decoded[:100]}')
                _parse_response(decoded, addr[0])
            except socket.timeout:
                break

        sock.close()
    except Exception as e:
        logger.error(f'Discovery scan failed: {e}')

    with _lock:
        found = list(_discovered_nodes.values())

    logger.info(f'SSDP scan complete: {len(found)} agents found')
    return found


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
    api_token = headers.get('X-FANCONTROL-TOKEN', '')
    location = headers.get('LOCATION', f'http://{source_ip}:5059')

    logger.info(f'SSDP scan found agent: {node_name} ({source_ip})')

    with _lock:
        _discovered_nodes[node_id] = {
            'node_id': node_id,
            'name': node_name,
            'ip': source_ip,
            'api_token': api_token,
            'location': location,
        }


def get_discovered_nodes() -> List[Dict]:
    with _lock:
        return list(_discovered_nodes.values())


# ============================================================================
# Continuous SSDP Listener
# ============================================================================

_discovery_callbacks: List[Callable] = []
_listener_running = False


def on_agent_discovered(callback: Callable):
    """Register callback for when new agent is discovered."""
    if callback not in _discovery_callbacks:
        _discovery_callbacks.append(callback)


def start_discovery_listener():
    """Start continuous SSDP listener for agent broadcasts."""
    global _listener_running
    if _listener_running:
        return

    _listener_running = True

    def _listen_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass  # SO_REUSEPORT not available on all platforms
            sock.bind(('', SSDP_PORT))

            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton('0.0.0.0')
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1)

            logger.info('SSDP discovery listener started on port %d', SSDP_PORT)

            while _listener_running:
                try:
                    data, addr = sock.recvfrom(1024)
                    _parse_and_notify(data.decode(errors='ignore'), addr[0])
                except socket.timeout:
                    continue
                except Exception as e:
                    logger.debug(f'Discovery listener error: {e}')

            sock.close()
        except Exception as e:
            logger.error(f'Discovery listener failed: {e}')

    thread = threading.Thread(target=_listen_loop, daemon=True)
    thread.start()


def _parse_and_notify(data: str, source_ip: str):
    """Parse SSDP response and notify if new agent."""
    global _discovered_nodes

    headers = {}
    for line in data.split('\r\n'):
        if ':' in line:
            key, _, value = line.partition(':')
            headers[key.strip().upper()] = value.strip()

    # Accept both ST and USN matching for agent detection
    st = headers.get('ST', '')
    usn = headers.get('USN', '')
    is_agent = (st == 'urn:fancontrol-web:agent' or 'urn:fancontrol-web:agent:' in usn)

    if not is_agent:
        return

    node_id = headers.get('X-FANCONTROL-ID', '')
    # Fallback: extract from USN if X-FanControl-Id header missing
    if not node_id and 'urn:fancontrol-web:agent:' in usn:
        node_id = usn.split('urn:fancontrol-web:agent:')[-1]

    node_name = headers.get('X-FANCONTROL-NAME', node_id)
    api_token = headers.get('X-FANCONTROL-TOKEN', '')
    location = headers.get('LOCATION', '')

    if not node_id:
        return

    with _lock:
        if node_id in _discovered_nodes:
            return

        from server.node_registry import get_node_by_token, get_node
        if get_node(node_id) or get_node_by_token(api_token):
            return

        _discovered_nodes[node_id] = {
            'node_id': node_id,
            'name': node_name,
            'ip': source_ip,
            'api_token': api_token,
            'location': location,
            'discovered_at': datetime.utcnow().isoformat(),
        }

    logger.info(f'Discovered new agent: {node_name} ({source_ip})')

    for cb in _discovery_callbacks:
        try:
            cb(_discovered_nodes[node_id])
        except Exception as e:
            logger.error(f'Discovery callback error: {e}')
