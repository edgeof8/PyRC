import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pyrc_core.client.irc_client_logic import IRCClient_Logic

logger = logging.getLogger("pyrc.commands.server.reconnect")

COMMAND_DEFINITIONS = [
    {
        "name": "reconnect",
        "handler": "handle_reconnect_command",
        "help": {
            "usage": "/reconnect",
            "description": "Disconnects and then reconnects to the current server.",
            "aliases": []
        }
    }
]

def handle_reconnect_command(client: "IRCClient_Logic", args_str: str):
    """Handles the /reconnect command."""
    if not client.server or client.port is None: # Check both server and port
        help_data = client.script_manager.get_help_text_for_command(
            "reconnect"
        )
        usage_msg = (
            help_data["help_text"]
            if help_data
            else "Cannot reconnect: No server configured or port missing. Use /server or /connect first."
        )
        client.add_message(
            usage_msg,
            "error", # Use semantic key
            context_name="Status",
        )
        return

    current_server: str = client.server
    current_port: int = client.port

    client.add_message(
        f"Reconnecting to {current_server}:{current_port}...",
        "system", # Use semantic key
        context_name="Status",
    )
    logger.info(
        f"User initiated /reconnect to {current_server}:{current_port}"
    )

    # Disconnect if currently connected
    if client.network_handler.connected:
        client.network_handler.disconnect_gracefully("Reconnecting")

    client.network_handler.update_connection_params(
        server=current_server,
        port=current_port,
        use_ssl=client.use_ssl,
        channels_to_join=client.network_handler.channels_to_join_on_connect,
    )
