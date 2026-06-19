"""Socket.IO event handlers for FanControl Web."""

from core.state import get_state, _init_complete


def register_handlers(socketio):
    """Register Socket.IO event handlers."""
    
    @socketio.on('connect')
    def handle_socket_connect():
        """Send initial state on client connection.
        Wait for init_hardware() to complete so the client always
        receives the correct 'initialized' flag (avoids wizard flash)."""
        _init_complete.wait(timeout=15)
        socketio.emit('update', get_state())

    @socketio.on('get_state')
    def handle_get_state():
        """Handle state request from client"""
        socketio.emit('update', get_state())

    from server.agent_handlers import register_agent_handlers
    register_agent_handlers(socketio)
