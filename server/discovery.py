"""SSDP discovery — listens for agent broadcasts on LAN."""

import logging
import socket
import struct
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    location = headers.get('LOCATION', f'http://{source_ip}:5059')

    logger.info(f'SSDP scan found agent: {node_name} ({source_ip})')

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
    location = headers.get('LOCATION', '')

    if not node_id:
        return

    with _lock:
        if node_id in _discovered_nodes:
            return

        from server.node_registry import get_node, list_nodes
        # Skip already-registered agents — check by both node_id and IP
        if get_node(node_id):
            return
        for n in list_nodes():
            if n.get('ip') == source_ip:
                return

        _discovered_nodes[node_id] = {
            'node_id': node_id,
            'name': node_name,
            'ip': source_ip,
            'location': location,
            'discovered_at': datetime.utcnow().isoformat(),
        }

    logger.info(f'Discovered new agent: {node_name} ({source_ip})')

    for cb in _discovery_callbacks:
        try:
            cb(_discovered_nodes[node_id])
        except Exception as e:
            logger.error(f'Discovery callback error: {e}')


# ============================================================================
# HTTP Probe — fallback when SSDP multicast doesn't work (Docker/VM)
# ============================================================================

def probe_agent(ip: str, port: int = 5059, timeout: int = 3) -> dict:
    """Try to reach an agent directly via HTTP /api/agent/status."""
    try:
        url = f'http://{ip}:{port}/api/agent/status'
        req = urllib.request.Request(url, method='GET')
        req.add_header('User-Agent', 'FanControl-Web')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode()
            import json
            info = json.loads(data)
            info['ip'] = ip
            info['port'] = port
            return info
    except Exception as e:
        logger.debug(f'Probe {ip}:{port} failed: {e}')
        return None


def probe_known_agents(timeout: int = 2) -> List[Dict]:
    """Probe all registered nodes that are offline via HTTP."""
    from server.node_registry import list_nodes
    results = []
    nodes = list_nodes()
    for node in nodes:
        if node.get('status') == 'online':
            continue
        ip = node.get('ip', '')
        if not ip:
            continue
        port = node.get('port', 5059)
        info = probe_agent(ip, port=port, timeout=timeout)
        if info:
            results.append({
                'node_id': node['node_id'],
                'name': node['name'],
                'ip': ip,
                'status': 'online',
                'info': info,
            })
    return results


# ============================================================================
# Subnet Scan — fast TCP probe of all IPs in local subnet
# ============================================================================

def _get_local_subnet() -> tuple:
    """Detect local IP and calculate subnet CIDR. Returns (ip, mask, prefix_len)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        return '127.0.0.1', '255.255.255.0', 24

    # Try to read netmask from /proc/net/if_inet6 or ip addr
    try:
        import fcntl
        import struct
        SIOCGIFNETMASK = 0x891b
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Get first non-loopback interface name
        with open('/proc/net/dev') as f:
            for line in f:
                if ':' in line and not line.strip().startswith('lo'):
                    iface = line.split(':')[0].strip()
                    break
            else:
                iface = 'eth0'
        mask_bytes = fcntl.ioctl(sock.fileno(), SIOCGIFNETMASK, struct.pack('256s', iface.encode()[:15]))
        mask = socket.inet_ntoa(mask_bytes[20:24])
        sock.close()
        prefix = sum(bin(int(b)).count('1') for b in mask.split('.'))
        return ip, mask, prefix
    except Exception:
        # Fallback: assume /24
        parts = ip.split('.')
        mask = f'{parts[0]}.{parts[1]}.{parts[2]}.0'
        return ip, mask, 24


def _tcp_probe(ip: str, port: int = 5059, timeout: float = 0.3) -> bool:
    """Quick TCP connect check — returns True if port is open."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def scan_subnet(port: int = 5059, timeout: float = 0.3, probe_timeout: int = 2) -> List[Dict]:
    """Scan local subnet for FanControl agents via TCP connect + HTTP probe.

    Returns list of dicts: {ip, name, node_id, api_token, ...}
    Excludes already-registered agents.
    """
    from server.node_registry import list_nodes
    existing_ips = {n['ip'] for n in list_nodes() if n.get('ip')}

    ip, mask, prefix = _get_local_subnet()
    logger.info(f'Subnet scan: local IP={ip}, mask={mask}, /{prefix}')

    # Generate all IPs in subnet
    ip_int = struct.unpack('!I', socket.inet_aton(ip))[0]
    mask_int = struct.unpack('!I', socket.inet_aton(mask))[0]
    network = ip_int & mask_int
    broadcast = network | (~mask_int & 0xFFFFFFFF)

    # For /24, skip network and broadcast addresses. For larger subnets, limit scan.
    hosts = []
    addr = network + 1
    while addr < broadcast:
        if addr != ip_int:  # skip self
            hosts.append(socket.inet_ntoa(struct.pack('!I', addr)))
        addr += 1
        if len(hosts) > 1024:  # safety limit
            break

    logger.info(f'Scanning {len(hosts)} hosts on port {port}...')

    # Parallel TCP connect scan
    found_ips = []
    with ThreadPoolExecutor(max_workers=64) as pool:
        futures = {pool.submit(_tcp_probe, h, port, timeout): h for h in hosts}
        for future in as_completed(futures):
            ip_addr = futures[future]
            try:
                if future.result():
                    found_ips.append(ip_addr)
            except Exception:
                pass

    logger.info(f'TCP scan found {len(found_ips)} hosts with open port {port}: {found_ips}')

    # HTTP probe each found IP
    results = []
    for ip_addr in found_ips:
        info = probe_agent(ip_addr, port=port, timeout=probe_timeout)
        if info:
            results.append(info)

    logger.info(f'Subnet scan complete: {len(results)} agents found')
    return results
