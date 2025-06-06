import logging
import time
import os
import socket
import threading
import uuid # For unique transfer IDs
from typing import Dict, Optional, Any, List, Tuple, Deque
from collections import deque # Import Deque
import logging.handlers # For RotatingFileHandler

# Assuming these will be accessible via client_logic.config or similar
# from config import (
#     DCC_ENABLED, DCC_DOWNLOAD_DIR, DCC_UPLOAD_DIR, DCC_AUTO_ACCEPT,
#     DCC_MAX_FILE_SIZE, DCC_PORT_RANGE_START, DCC_PORT_RANGE_END, DCC_TIMEOUT,
#     DCC_BLOCKED_EXTENSIONS
# )
from pyrc_core.app_config import AppConfig # Import AppConfig class
from pyrc_core.dcc.dcc_transfer import DCCTransfer, DCCSendTransfer, DCCReceiveTransfer, DCCTransferStatus, DCCTransferType
from pyrc_core.dcc.dcc_protocol import parse_dcc_ctcp, format_dcc_send_ctcp, format_dcc_accept_ctcp, format_dcc_checksum_ctcp, format_dcc_resume_ctcp
from pyrc_core.dcc.dcc_security import validate_download_path, sanitize_filename
from pyrc_core.dcc.dcc_ctcp_handler import DCCCTCPHandler
from pyrc_core.dcc.dcc_passive_offer_manager import DCCPassiveOfferManager
from pyrc_core.dcc.dcc_send_manager import DCCSendManager # Added import

logger = logging.getLogger("pyrc.dcc.manager") # For general manager operations
dcc_event_logger = logging.getLogger("pyrc.dcc.events") # For detailed DCC events

def setup_dcc_specific_logger(config: AppConfig):
    """Sets up the dedicated DCC event logger."""
    if not config.dcc_log_enabled:
        dcc_event_logger.disabled = True
        logger.info("Dedicated DCC event logging is disabled via config.")
        return

    # Ensure the logs directory exists (similar to irc_client_logic.py)
    log_dir = os.path.join(config.BASE_DIR, "logs")
    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError as e:
            logger.error(f"Could not create logs directory '{log_dir}' for DCC logs: {e}")
            dcc_event_logger.disabled = True # Disable if dir can't be made
            return

    log_file_path = os.path.join(log_dir, config.dcc_log_file)

    # Prevent adding handlers multiple times if this function were called again (e.g., rehash)
    if dcc_event_logger.hasHandlers():
        # Attempt to remove existing file handlers to reconfigure, or just return if config is unchanged
        # For simplicity now, we'll assume it's setup once. Rehash might need more robust handler management.
        logger.debug("DCC event logger already has handlers. Skipping reconfiguration for now.")
        # To properly reconfigure on rehash, we'd need to close and remove existing handlers.
        # For now, if it's already set up, we assume it's fine.
        # If DCC_LOG_ENABLED was turned off then on, it might not re-enable without handler removal.
        # However, dcc_event_logger.disabled = True would still take effect.
        if dcc_event_logger.disabled and config.dcc_log_enabled: # Re-enabling
             dcc_event_logger.disabled = False
             logger.info("Re-enabled dedicated DCC event logging.")
        return


    dcc_event_logger.setLevel(config.dcc_log_level_str)

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    try:
        handler = logging.handlers.RotatingFileHandler(
            log_file_path,
            maxBytes=config.dcc_log_max_bytes,
            backupCount=config.dcc_log_backup_count,
            encoding="utf-8"
        )
        handler.setFormatter(formatter)
        dcc_event_logger.addHandler(handler)
        dcc_event_logger.propagate = False # Don't send to root logger if we have our own file
        logger.info(f"Dedicated DCC event logger configured: File='{log_file_path}', Level={logging.getLevelName(config.dcc_log_level_int)}")
    except Exception as e:
        logger.error(f"Failed to setup dedicated DCC file logger: {e}", exc_info=True)
        dcc_event_logger.disabled = True


class DCCManager:
    def __init__(self, client_logic_ref: Any, event_manager_ref: Any, config: AppConfig):
        self.client_logic = client_logic_ref
        self.event_manager = event_manager_ref
        self.config = config # Store the AppConfig object
        self.transfers: Dict[str, DCCTransfer] = {}
        self.dcc_config = self._load_dcc_config() # This will now use self.config
        self._lock = threading.Lock() # Protects self.transfers. Used by sub-managers.
        self._cleanup_timer: Optional[threading.Timer] = None

        # Setup the dedicated DCC logger instance BEFORE instantiating sub-managers
        # This logger will be used by DCCManager and passed to DCCTransfer instances
        setup_dcc_specific_logger(self.config) # Pass the config object
        self.dcc_event_logger = dcc_event_logger # Store a reference if needed, or just use the global one.

        self.ctcp_handler = DCCCTCPHandler(self)
        self.passive_offer_manager = DCCPassiveOfferManager(self)
        self.send_manager = DCCSendManager(self) # Instantiate send manager

        if not self.dcc_config.get("enabled", False):
            logger.info("DCCManager initialized, but DCC is disabled in configuration.")
        else:
            logger.info("DCCManager initialized and DCC is enabled.")
            self.dcc_event_logger.info("DCCManager initialized and DCC is enabled via app_config.")
            # Ensure download/upload directories exist
            self._ensure_dir_exists(self.dcc_config["download_dir"])
            # Upload dir is less critical to pre-create as source path is absolute for send.

            # Start cleanup timer if DCC and cleanup are enabled
            if self.dcc_config.get("cleanup_enabled", True):
                self._start_cleanup_timer()

    def _load_dcc_config(self) -> Dict[str, Any]:
        # Load relevant DCC settings from the self.config object
        return {
            "enabled": self.config.dcc_enabled,
            "download_dir": self.config.dcc_download_dir,
            "upload_dir": self.config.dcc_upload_dir,
            "auto_accept": self.config.dcc_auto_accept,
            "max_file_size": self.config.dcc_max_file_size,
            "port_range_start": self.config.dcc_port_range_start,
            "port_range_end": self.config.dcc_port_range_end,
            "timeout": self.config.dcc_timeout,
            "blocked_extensions": self.config.dcc_blocked_extensions,
            "passive_mode_token_timeout": self.config.dcc_passive_mode_token_timeout,
            "checksum_verify": self.config.dcc_checksum_verify,
            "checksum_algorithm": self.config.dcc_checksum_algorithm.lower(),
            "resume_enabled": self.config.dcc_resume_enabled,
            "cleanup_enabled": self.config.dcc_cleanup_enabled,
            "cleanup_interval_seconds": self.config.dcc_cleanup_interval_seconds,
            "transfer_max_age_seconds": self.config.dcc_transfer_max_age_seconds,
            "advertised_ip": self.config.dcc_advertised_ip,
        }

    def _start_cleanup_timer(self):
        """Starts or restarts the cleanup timer."""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()

        interval = self.dcc_config.get("cleanup_interval_seconds", 3600)
        if interval <= 0:
            self.dcc_event_logger.info("DCC cleanup interval is non-positive, automatic cleanup disabled.")
            return

        self.dcc_event_logger.debug(f"Scheduling next DCC cleanup in {interval} seconds.")
        self._cleanup_timer = threading.Timer(interval, self._scheduled_cleanup_task)
        self._cleanup_timer.daemon = True  # Ensure timer doesn't prevent exit
        self._cleanup_timer.start()

    def _scheduled_cleanup_task(self):
        """Called by the cleanup timer to perform cleanup and reschedule."""
        if not self.dcc_config.get("cleanup_enabled", True):
            self.dcc_event_logger.info("DCC cleanup is disabled. Skipping scheduled cleanup task.")
            return
        self.dcc_event_logger.info("Running scheduled DCC cleanup task...")
        self._cleanup_finished_transfers()
        # Reschedule the timer if still enabled
        if self.dcc_config.get("enabled") and self.dcc_config.get("cleanup_enabled", True):
            self._start_cleanup_timer()
        else:
            self.dcc_event_logger.info("DCC or DCC cleanup disabled, stopping cleanup timer.")

    def _cleanup_finished_transfers(self):
        """Removes old completed/failed/cancelled/timed-out transfers from self.transfers."""
        if not self.dcc_config.get("cleanup_enabled", True):
            self.dcc_event_logger.debug("DCC cleanup is disabled. Skipping cleanup.")
            return

        with self._lock:  # Ensure thread safety
            now = time.monotonic()
            max_age_seconds = self.dcc_config.get("transfer_max_age_seconds", 86400 * 3)
            transfer_ids_to_remove: List[str] = []

            for transfer_id, transfer in self.transfers.items():
                if transfer.status in [DCCTransferStatus.COMPLETED,
                                     DCCTransferStatus.FAILED,
                                     DCCTransferStatus.CANCELLED,
                                     DCCTransferStatus.TIMED_OUT]:
                    if transfer.end_time and (now - transfer.end_time > max_age_seconds):
                        transfer_ids_to_remove.append(transfer_id)
                        self.dcc_event_logger.info(
                            f"Marking transfer {transfer_id} ('{transfer.original_filename}') for cleanup "
                            f"(age: {now - transfer.end_time:.0f}s > max_age: {max_age_seconds}s)."
                        )

            cleaned_count = 0
            for transfer_id_to_remove in transfer_ids_to_remove:
                if transfer_id_to_remove in self.transfers:
                    del self.transfers[transfer_id_to_remove]
                    cleaned_count += 1

            if cleaned_count > 0:
                self.dcc_event_logger.info(f"Cleaned up {cleaned_count} old DCC transfer(s).")
                self.client_logic.add_message(
                    f"DCC: Cleaned up {cleaned_count} old transfer(s).",
                    "debug",
                    context_name="DCC"
                )
            else:
                self.dcc_event_logger.debug("DCC cleanup: No old transfers found to remove in this cycle.")

            # Potentially trigger a UI update if the /dcc list might change significantly
            if cleaned_count > 0 and hasattr(self.client_logic, "ui_needs_update"):
                self.client_logic.ui_needs_update.set()

    def shutdown(self):
        """Cleanup method to be called when the client is shutting down."""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None
        self.dcc_event_logger.info("DCCManager shutdown complete.")

    def _cleanup_stale_passive_offers(self):
        """Removes pending passive offers that have timed out by delegating to DCCPassiveOfferManager."""
        cleaned_count = self.passive_offer_manager.cleanup_stale_offers()
        if cleaned_count > 0:
            self.client_logic.add_message(f"Cleaned up {cleaned_count} stale passive DCC offer(s).", "debug", context_name="DCC")
            # DCCPassiveOfferManager already logs details


    def _ensure_dir_exists(self, dir_path: str):
        abs_dir_path = os.path.abspath(dir_path)
        if not os.path.exists(abs_dir_path):
            try:
                os.makedirs(abs_dir_path, exist_ok=True)
                self.dcc_event_logger.info(f"Created directory: {abs_dir_path}")
            except OSError as e:
                self.dcc_event_logger.error(f"Could not create directory '{abs_dir_path}': {e}")
                # Potentially disable DCC or parts of it if essential dirs can't be made
                self.client_logic.add_message(f"Error: DCC directory '{abs_dir_path}' cannot be created. DCC may not function.", "error", context_name="Status")


    def _generate_transfer_id(self) -> str:
        return str(uuid.uuid4())

    def _get_listening_socket(self) -> Optional[Tuple[socket.socket, int]]:
        """Finds an available port in the configured range and returns a listening socket."""
        port_start = self.dcc_config.get("port_range_start", 1024)
        port_end = self.dcc_config.get("port_range_end", 65535)

        if port_start > port_end:
            self.dcc_event_logger.warning(f"Invalid port range: start ({port_start}) > end ({port_end}). Using default range 1024-65535.")
            port_start = 1024
            port_end = 65535

        ports_to_try = list(range(port_start, port_end + 1))
        self.dcc_event_logger.info(f"Attempting to find available DCC port in range {port_start}-{port_end}")

        for port in ports_to_try:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("", port))  # Bind to all interfaces on the current port
                s.listen(1)  # Listen for one incoming connection for this transfer
                self.dcc_event_logger.info(f"Successfully bound DCC listening socket to port {port}.")
                return s, port
            except socket.error as e:
                if e.errno == 98:  # EADDRINUSE
                    self.dcc_event_logger.debug(f"Port {port} already in use, trying next.")
                else:
                    self.dcc_event_logger.warning(f"Could not bind to port {port}: {e}. Trying next.")
                if s:  # Ensure socket is closed if bind failed after creation
                    s.close()
            except Exception as ex:
                self.dcc_event_logger.error(f"Unexpected error trying port {port}: {ex}")
                if s:
                    s.close()

        # If we get here, all ports in the range failed
        self.dcc_event_logger.error(f"Could not find an available DCC listening port in range {port_start}-{port_end}.")
        self.client_logic.add_message(f"Error: No available DCC ports in range {port_start}-{port_end}.", "error", context_name="DCC")
        return None

    def _get_local_ip_for_ctcp(self) -> str:
        """Attempts to determine a suitable local IP address for CTCP messages."""
        configured_ip = self.dcc_config.get("advertised_ip")
        if configured_ip and isinstance(configured_ip, str) and configured_ip.strip():
            # Validate the configured IP format
            try:
                socket.inet_aton(configured_ip) # Basic validation
                self.dcc_event_logger.info(f"Using configured DCC advertised IP: {configured_ip}")
                return configured_ip
            except socket.error:
                self.dcc_event_logger.warning(f"Configured dcc_advertised_ip '{configured_ip}' is invalid. Falling back to auto-detection.")

        # Existing auto-detection logic:
        try:
            temp_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            temp_s.settimeout(0.5)
            temp_s.connect(("8.8.8.8", 80))
            local_ip = temp_s.getsockname()[0]
            temp_s.close()
            self.dcc_event_logger.debug(f"Auto-detected DCC IP via external connect: {local_ip}")
            return local_ip
        except socket.error:
            self.dcc_event_logger.warning("Could not determine local IP for DCC CTCP using external connect. Trying hostname.")
            try:
                local_ip = socket.gethostbyname(socket.gethostname())
                self.dcc_event_logger.debug(f"Auto-detected DCC IP via gethostname: {local_ip}")
                return local_ip
            except socket.gaierror:
                self.dcc_event_logger.warning("Could not determine local IP via gethostname. Falling back to '127.0.0.1'.")
                return "127.0.0.1"

    # _execute_send method is removed as its logic is now in DCCSendManager._execute_send_operation

    def initiate_sends(self, peer_nick: str, local_filepaths: List[str], passive: bool = False) -> Dict[str, Any]:
        """
        Initiates DCC SEND for one or more files. Delegates to DCCSendManager.
        """
        if not self.dcc_config.get("enabled"): # Keep basic enabled check here
            return {"success": False, "error": "DCC is disabled.", "transfers_started": [], "files_queued": [], "errors": []}
        return self.send_manager.initiate_sends(peer_nick, local_filepaths, passive)

    def _process_next_in_send_queue(self, peer_nick: str):
        """Checks the send queue for a peer and starts the next transfer. Delegates to DCCSendManager."""
        self.send_manager.process_next_in_queue(peer_nick)

    def handle_incoming_dcc_ctcp(self, nick: str, userhost: str, ctcp_payload: str):
        """Handles a parsed DCC CTCP command from a peer by delegating to DCCCTCPHandler."""
        if not self.dcc_config.get("enabled"):
            self.dcc_event_logger.info(f"DCC disabled, ignoring incoming DCC CTCP from {nick}: {ctcp_payload}")
            return

        self.dcc_event_logger.debug(f"Received raw CTCP from {nick} ({userhost}): {ctcp_payload}")
        parsed_dcc = parse_dcc_ctcp(ctcp_payload)
        if not parsed_dcc:
            self.dcc_event_logger.warning(f"Could not parse DCC CTCP from {nick}: {ctcp_payload}")
            self.client_logic.add_message(f"Received malformed DCC request from {nick}.", "error", context_name="Status")
            return

        # Log before delegating to the handler, so we know manager received it
        self.dcc_event_logger.info(f"DCCManager: Delegating DCC Command '{parsed_dcc.get('dcc_command')}' from {nick} to CTCP Handler. Data: {parsed_dcc}")
        self.ctcp_handler.process_ctcp(nick, userhost, parsed_dcc)


    def accept_incoming_resume_offer(
        self,
        nick: str,
        resume_filename: str,
        peer_ip_address: str,
        resume_peer_port: int,
        resume_position_offered_by_peer: int,
        total_filesize: int, # This was determined from our prior transfer record
        local_file_path_to_check: str # This is the validated path from our prior record
    ) -> None:
        """
        Handles the logic for accepting a DCC RESUME offer from a peer.
        This method is called by DCCCTCPHandler after initial parsing and validation.
        """
        self.dcc_event_logger.info(f"DCCManager: Processing acceptance of RESUME offer for '{resume_filename}' from {nick}.")

        actual_resume_offset = 0
        can_resume_this_offer = False

        if os.path.exists(local_file_path_to_check):
            current_local_size = os.path.getsize(local_file_path_to_check)
            if current_local_size == resume_position_offered_by_peer:
                actual_resume_offset = current_local_size
                can_resume_this_offer = True
                self.dcc_event_logger.info(f"Local file '{local_file_path_to_check}' size {current_local_size} matches peer's offered RESUME offset {resume_position_offered_by_peer}. Will accept.")
            else:
                self.dcc_event_logger.warning(f"DCC RESUME from {nick}: local file '{local_file_path_to_check}' size {current_local_size} mismatches peer's offered offset {resume_position_offered_by_peer}. Cannot accept this RESUME offer as is.")
        elif resume_position_offered_by_peer == 0:
             can_resume_this_offer = True
             actual_resume_offset = 0
             self.dcc_event_logger.info(f"DCC RESUME from {nick} for '{resume_filename}' is from offset 0. Local file missing/re-downloading to '{local_file_path_to_check}'.")

        if can_resume_this_offer:
            ctcp_accept_msg = format_dcc_accept_ctcp(resume_filename, "0", 0, actual_resume_offset, token=None)
            if not ctcp_accept_msg:
                self.dcc_event_logger.error(f"Failed to format DCC ACCEPT for RESUME from {nick}.")
                return

            self.client_logic.send_ctcp_privmsg(nick, ctcp_accept_msg)
            new_transfer_id = self._generate_transfer_id()

            recv_resume_args = {
                "transfer_id": new_transfer_id,
                "peer_nick": nick,
                "filename": resume_filename,
                "filesize": total_filesize,
                "local_filepath": local_file_path_to_check,
                "dcc_manager_ref": self,
                "connect_to_ip": peer_ip_address,
                "connect_to_port": resume_peer_port,
                "resume_offset": actual_resume_offset,
                "peer_ip": peer_ip_address, # Store peer's IP for the transfer object
                "dcc_event_logger": self.dcc_event_logger
            }

            new_recv_transfer = DCCReceiveTransfer(**recv_resume_args)
            with self._lock:
                self.transfers[new_transfer_id] = new_recv_transfer

            new_recv_transfer._report_status(DCCTransferStatus.CONNECTING, f"Accepted RESUME from {nick}. Connecting to resume download.")
            new_recv_transfer.start_transfer_thread()
            self.client_logic.add_message(f"Accepted DCC RESUME from {nick} for '{resume_filename}'. Resuming download from offset {actual_resume_offset}.", "system", context_name="DCC")
        else:
            self.dcc_event_logger.info(f"Cannot accept DCC RESUME from {nick} for '{resume_filename}' under current conditions (offset mismatch or file issue).")
            self.client_logic.add_message(f"Could not accept DCC RESUME from {nick} for '{resume_filename}' (offset/file mismatch).", "warning", context_name="DCC")


    def accept_incoming_send_offer(self, peer_nick: str, original_filename: str, ip_str: str, port: int, filesize: int) -> Dict[str, Any]:
        """
        Called by command handler when user accepts a DCC SEND offer.
        Initiates a DCCReceiveTransfer (Active DCC RECV for Phase 1).
        """
        self.dcc_event_logger.info(f"Attempting to accept ACTIVE DCC SEND offer from {peer_nick} for '{original_filename}' ({ip_str}:{port}, {filesize} bytes).")
        if not self.dcc_config.get("enabled"):
            return {"success": False, "error": "DCC is disabled."}

        validation_result = validate_download_path(
            original_filename,
            self.dcc_config["download_dir"],
            self.dcc_config["blocked_extensions"],
            self.dcc_config["max_file_size"],
            filesize
        )

        if not validation_result["success"]:
            return {"success": False, "error": validation_result["error"], "sanitized_filename": validation_result.get("sanitized_filename")}

        safe_local_path = validation_result["safe_path"]
        sanitized_filename_for_log = validation_result["sanitized_filename"]

        transfer_id = self._generate_transfer_id()
        recv_transfer = DCCReceiveTransfer(
            transfer_id=transfer_id,
            peer_nick=peer_nick,
            filename=original_filename, # Original filename for display/logging
            filesize=filesize,
            local_filepath=safe_local_path, # Where to save the file
            dcc_manager_ref=self,
            connect_to_ip=ip_str, # For active DCC RECV, we connect
            connect_to_port=port,
            peer_ip=ip_str, # Store sender's IP
            dcc_event_logger=self.dcc_event_logger # Pass the logger
        )

        with self._lock:
            self.transfers[transfer_id] = recv_transfer
            self.dcc_event_logger.debug(f"Created DCCReceiveTransfer (active) with ID {transfer_id} for '{original_filename}' from {peer_nick}.")

        recv_transfer._report_status(DCCTransferStatus.QUEUED) # Or CONNECTING if thread starts immediately
        recv_transfer.start_transfer_thread()

        self.event_manager.dispatch_event("DCC_TRANSFER_QUEUED", {
            "transfer_id": transfer_id, "type": "RECEIVE", "nick": peer_nick,
            "filename": original_filename, "size": filesize
        })
        self.client_logic.add_message(f"DCC RECV for '{original_filename}' from {peer_nick} accepted. Connecting to {ip_str}:{port}. Saving to '{sanitized_filename_for_log}'.", "system", context_name="DCC")
        return {"success": True, "transfer_id": transfer_id}

    def initiate_passive_receive(self, peer_nick: str, offered_filename: str, offered_filesize: int, offer_token: str, peer_ip_str_from_offer: Optional[str]) -> Dict[str, Any]:
        """
        Called when the local user wants to accept a PASSIVE DCC SEND offer they received.
        This client will listen, and send an ACCEPT CTCP to the peer, who will then connect.
        `peer_ip_str_from_offer` is retrieved from the stored passive offer.
        """
        self.dcc_event_logger.info(f"Attempting to initiate PASSIVE DCC RECV for '{offered_filename}' from {peer_nick} (token: {offer_token}, size: {offered_filesize}, peer_ip: {peer_ip_str_from_offer}).")
        if not self.dcc_config.get("enabled"):
            return {"success": False, "error": "DCC is disabled."}

        validation_result = validate_download_path(
            offered_filename,
            self.dcc_config["download_dir"],
            self.dcc_config["blocked_extensions"],
            self.dcc_config["max_file_size"],
            offered_filesize
        )

        if not validation_result["success"]:
            return {"success": False, "error": validation_result["error"], "sanitized_filename": validation_result.get("sanitized_filename")}

        safe_local_path = validation_result["safe_path"]
        sanitized_filename_for_log = validation_result["sanitized_filename"]

        socket_info = self._get_listening_socket()
        if not socket_info:
            return {"success": False, "error": "Could not create listening socket for passive DCC RECV."}

        listening_socket, local_listening_port = socket_info
        local_ip_for_ctcp = self._get_local_ip_for_ctcp()

        ctcp_accept_message = format_dcc_accept_ctcp(
            filename=offered_filename,
            ip_str=local_ip_for_ctcp,
            port=local_listening_port,
            position=0,
            token=offer_token
        )

        if not ctcp_accept_message:
            listening_socket.close()
            self.dcc_event_logger.error(f"Failed to format passive DCC ACCEPT CTCP for '{offered_filename}' to {peer_nick}.")
            return {"success": False, "error": "Failed to format passive DCC ACCEPT CTCP message."}
        self.dcc_event_logger.debug(f"Passive RECV for '{offered_filename}' from {peer_nick}. Sent ACCEPT CTCP: {ctcp_accept_message.strip()}")

        transfer_id = self._generate_transfer_id()
        recv_transfer_args = {
            "transfer_id":transfer_id,
            "peer_nick":peer_nick,
            "filename":offered_filename,
            "filesize":offered_filesize,
            "local_filepath":safe_local_path,
            "dcc_manager_ref":self,
            "server_socket_for_passive_recv":listening_socket,
            "peer_ip": peer_ip_str_from_offer, # Use the IP from the stored offer
            "dcc_event_logger": self.dcc_event_logger
        }
        # We need to ensure that ip_str from the passive offer is stored in pending_passive_offers
        # This was added in the change for line 443.

        recv_transfer = DCCReceiveTransfer(**recv_transfer_args)

        with self._lock:
            self.transfers[transfer_id] = recv_transfer
            self.dcc_event_logger.debug(f"Created DCCReceiveTransfer (passive setup) with ID {transfer_id} for '{offered_filename}' from {peer_nick}.")

        # Send the CTCP ACCEPT to the peer, inviting them to connect to us
        self.client_logic.send_ctcp_privmsg(peer_nick, ctcp_accept_message)

        recv_transfer._report_status(DCCTransferStatus.NEGOTIATING, f"Sent passive ACCEPT. Waiting for {peer_nick} to connect to {local_ip_for_ctcp}:{local_listening_port}.")
        recv_transfer.start_transfer_thread() # This thread will now block on listening_socket.accept()

        self.event_manager.dispatch_event("DCC_TRANSFER_QUEUED", {
            "transfer_id": transfer_id, "type": "RECEIVE", "nick": peer_nick,
            "filename": offered_filename, "size": offered_filesize, "is_passive_setup": True
        })
        self.client_logic.add_message(
            f"Passive DCC RECV for '{offered_filename}' from {peer_nick} initiated. "
            f"Listening on {local_ip_for_ctcp}:{local_listening_port}. Waiting for peer connection.",
            "system", context_name="DCC"
        )
        return {"success": True, "transfer_id": transfer_id}

    def accept_passive_offer_by_token(self, calling_nick_for_logging: str, offered_filename_by_user: str, offer_token: str) -> Dict[str, Any]:
        """
        Accepts a stored passive DCC SEND offer based on a token.
        Called by the /dcc get command. Delegates to DCCPassiveOfferManager.
        """
        self.dcc_event_logger.info(f"User {calling_nick_for_logging} attempting to accept passive offer with token: {offer_token} for filename: {offered_filename_by_user}")

        pending_offer_details = self.passive_offer_manager.get_offer(offer_token)

        if not pending_offer_details:
            self.dcc_event_logger.warning(f"Passive offer with token '{offer_token}' not found or expired for user {calling_nick_for_logging}.")
            return {"success": False, "error": f"Passive offer with token '{offer_token}' not found or expired."}

        original_sender_nick = pending_offer_details["nick"]
        original_offered_filename = pending_offer_details["filename"]
        original_filesize = pending_offer_details["filesize"]
        original_peer_ip_str = pending_offer_details.get("ip_str") # Get the stored IP

        if offered_filename_by_user.lower() != original_offered_filename.lower():
             self.dcc_event_logger.warning(f"Filename mismatch for passive offer token {offer_token}. User: '{offered_filename_by_user}', Offer: '{original_offered_filename}'. Proceeding by token match.")

        if not self.passive_offer_manager.remove_offer(offer_token):
            self.dcc_event_logger.warning(f"Failed to remove passive offer token {offer_token} during acceptance by {calling_nick_for_logging}, it might have been removed by cleanup.")
            # Continue if details were fetched, but log this anomaly.

        return self.initiate_passive_receive(
            peer_nick=original_sender_nick,
            offered_filename=original_offered_filename,
            offered_filesize=original_filesize,
            offer_token=offer_token,
            peer_ip_str_from_offer=original_peer_ip_str # Pass the IP
        )

    def update_transfer_status(self, transfer_id: str, status: DCCTransferStatus, error_message: Optional[str]):
        with self._lock:
            transfer = self.transfers.get(transfer_id)
        if transfer:
            old_status = transfer.status
            transfer.status = status
            transfer.error_message = error_message
            self.dcc_event_logger.info(f"Transfer {transfer_id} ('{transfer.original_filename}') status updated: {old_status.name} -> {status.name}. Error: {error_message}")

            event_name = ""
            if status == DCCTransferStatus.COMPLETED: event_name = "DCC_TRANSFER_COMPLETE"
            elif status in [DCCTransferStatus.FAILED, DCCTransferStatus.TIMED_OUT]: event_name = "DCC_TRANSFER_ERROR"
            elif status == DCCTransferStatus.CANCELLED: event_name = "DCC_TRANSFER_CANCELLED"
            elif status == DCCTransferStatus.TRANSFERRING: event_name = "DCC_TRANSFER_START" # First time it hits transferring
            # Add more specific events if needed, e.g. DCC_TRANSFER_CONNECTING

            if event_name:
                event_data = {
                    "transfer_id": transfer.transfer_id,
                    "type": transfer.transfer_type.name,
                    "nick": transfer.peer_nick,
                    "filename": transfer.original_filename,
                    "local_path": transfer.local_filepath,
                    "size": transfer.filesize # Add size to error/complete events too
                }
                if error_message:
                    event_data["error_message"] = error_message
                self.event_manager.dispatch_event(event_name, event_data)

            # Clean up completed or failed transfers from active list?
            # Or keep them for a while for /dcc list. For now, keep.
            if status in [DCCTransferStatus.COMPLETED, DCCTransferStatus.FAILED, DCCTransferStatus.CANCELLED, DCCTransferStatus.TIMED_OUT]:
                logger.info(f"Transfer {transfer_id} ('{transfer.original_filename}') reached final state: {status.name}.")
                # If it's a send transfer, try to process the next in queue for that peer
                if isinstance(transfer, DCCSendTransfer):
                    self.dcc_event_logger.debug(f"Send transfer {transfer_id} for {transfer.peer_nick} ended. Checking send queue.")
                    self._process_next_in_send_queue(transfer.peer_nick)
                # Consider removing from self.transfers after a delay or if list gets too long.

            self.client_logic.add_message(
                f"DCC {transfer.transfer_type.name} '{transfer.original_filename}' with {transfer.peer_nick}: {status.name}"
                f"{f' ({error_message})' if error_message else ''}",
                "error" if status in [DCCTransferStatus.FAILED, DCCTransferStatus.TIMED_OUT] else "system",
                context_name="DCC"
            )
        else:
            self.dcc_event_logger.warning(f"update_transfer_status called for unknown transfer_id: {transfer_id}")

    def update_transfer_progress(self, transfer_id: str, bytes_transferred: int, total_size: int, rate_bps: float, eta_seconds: Optional[float]):
        # This can be very frequent, so logging might be too verbose for INFO/DEBUG unless specifically needed.
        # For now, no dcc_event_logger call here. Can be added if debugging specific progress issues.
        with self._lock:
            transfer = self.transfers.get(transfer_id)
        if transfer:
            transfer.bytes_transferred = bytes_transferred
            # filesize might differ from total_size if sender sends more/less than advertised
            # but for progress, use the initial filesize as total.
            transfer.current_rate_bps = rate_bps
            transfer.estimated_eta_seconds = eta_seconds

            self.event_manager.dispatch_event("DCC_TRANSFER_PROGRESS", {
                "transfer_id": transfer.transfer_id,
                "type": transfer.transfer_type.name,
                "bytes_transferred": bytes_transferred,
                "total_size": transfer.filesize, # Use original advertised filesize for consistency
                "rate_bps": rate_bps,
                "eta_seconds": eta_seconds
            })
            # UI update for progress is typically handled by the UI subscribing to DCC_TRANSFER_PROGRESS
            # No direct add_message here to avoid flooding, unless it's a very infrequent update.
        else:
            # This could also be verbose if it happens often for a short period after a transfer is removed.
            # logger.warning(f"update_transfer_progress called for unknown transfer_id: {transfer_id}")
            pass


    def get_transfer_statuses(self) -> List[str]:
        """Returns a list of formatted strings representing current transfer statuses."""
        status_lines = []
        with self._lock:
            # List active/completed/failed transfers
            if self.transfers:
                status_lines.append("--- Active/Recent Transfers ---")
                # Sort by start time or add time? For now, just iterate.
                # Consider sorting by a 'last_updated' timestamp if available on DCCTransfer objects.
                sorted_transfers = sorted(self.transfers.items(), key=lambda item: getattr(item[1], 'start_time', 0) or getattr(item[1], 'queue_time', 0), reverse=True)

                for tid, t in sorted_transfers:
                    progress_percent = (t.bytes_transferred / t.filesize * 100) if t.filesize > 0 else 0
                    size_str = f"{t.bytes_transferred / (1024*1024):.2f}MB / {t.filesize / (1024*1024):.2f}MB"
                    rate_str = f"{t.current_rate_bps / 1024:.1f} KB/s" if t.current_rate_bps is not None and t.current_rate_bps > 0 else ""
                    eta_str = f"ETA: {int(t.estimated_eta_seconds // 60)}m{int(t.estimated_eta_seconds % 60)}s" if t.estimated_eta_seconds is not None else ""

                    checksum_info = ""
                    if hasattr(t, 'checksum_status') and t.checksum_status and t.checksum_status not in ["Pending", "NotChecked", None]:
                        checksum_info = f" Checksum: {t.checksum_status}"
                        if hasattr(t, 'checksum_algorithm') and t.checksum_algorithm:
                            checksum_info += f" ({t.checksum_algorithm})"

                    line = (f"ID: {tid[:8]} [{t.transfer_type.name}] {t.peer_nick} - '{t.original_filename}' "
                            f"({size_str}, {progress_percent:.1f}%) Status: {t.status.name}{checksum_info} {rate_str} {eta_str}")
                    if t.error_message:
                        line += f" Error: {t.error_message}"
                    status_lines.append(line)

            # List pending passive offers using the new manager
            passive_offer_status_lines = self.passive_offer_manager.get_status_lines()
            if passive_offer_status_lines: # Will include its own header if offers exist
                status_lines.extend(passive_offer_status_lines)

            if not status_lines: # If both active transfers and passive offers were empty
                return ["No active DCC transfers or pending passive offers."]

            # The previously duplicated/commented out code from line 683 to 702 has been removed by this diff.
            # The loop for self.transfers is already handled correctly above.
            # The final return statement's indentation is also corrected.
        return status_lines # This line is now correctly indented.

    def cancel_transfer(self, transfer_id: str) -> bool:
        with self._lock:
            transfer = self.transfers.get(transfer_id)

        if transfer:
            self.dcc_event_logger.info(f"User requested cancellation of transfer {transfer_id} ('{transfer.original_filename}') to/from {transfer.peer_nick}.")
            transfer.stop_transfer(DCCTransferStatus.CANCELLED, "User cancelled.")
            # The stop_transfer method itself calls _report_status, which dispatches event
            return True
        self.dcc_event_logger.warning(f"Cannot cancel: Transfer ID {transfer_id} not found in active transfers.")
        return False

    def cancel_pending_passive_offer(self, token_prefix: str) -> bool:
        """Cancels a pending passive DCC SEND offer by delegating to DCCPassiveOfferManager."""
        actual_token_cancelled, cancelled_offer_details = self.passive_offer_manager.cancel_offer_by_prefix(token_prefix)

        if cancelled_offer_details and actual_token_cancelled:
            peer_nick = cancelled_offer_details.get('nick', 'UnknownNick')
            filename = cancelled_offer_details.get('filename', 'UnknownFile')
            self.client_logic.add_message(
                f"Cancelled pending passive DCC SEND offer from {peer_nick} for '{filename}' (token: {actual_token_cancelled[:8]}...).",
                "system", context_name="DCC"
            )
            return True
        return False

    def cleanup_old_transfers(self):
        # Placeholder for future: remove very old completed/failed transfers
        pass

    def send_dcc_checksum_info(self, transfer_id: str, peer_nick: str, filename: str, algorithm: str, checksum: str):
        """Called by DCCSendTransfer to send checksum info to the peer."""
        if not self.dcc_config.get("checksum_verify", False) or self.dcc_config.get("checksum_algorithm", "none") == "none":
            return # Checksums not enabled

        self.dcc_event_logger.info(f"Sending DCCCHECKSUM for transfer {transfer_id}, file '{filename}', algo {algorithm}, checksum '{checksum[:10]}...' to {peer_nick}")

        # Using transfer_id as the identifier for the peer to match
        ctcp_checksum_msg = format_dcc_checksum_ctcp(filename, algorithm, checksum, transfer_id)

        if ctcp_checksum_msg:
            self.client_logic.send_ctcp_privmsg(peer_nick, ctcp_checksum_msg)
            self.client_logic.add_message(f"Sent checksum ({algorithm}) for '{filename}' to {peer_nick}.", "debug", context_name="DCC")
        else:
            self.dcc_event_logger.error(f"Failed to format DCCCHECKSUM message for transfer {transfer_id}.")

    def update_transfer_checksum_result(self, transfer_id: str, checksum_status: str):
        """Called by DCCTransfer when checksum comparison is done."""
        with self._lock:
            transfer = self.transfers.get(transfer_id)

        if transfer:
            self.dcc_event_logger.info(f"Checksum status for transfer {transfer_id} ('{transfer.original_filename}'): {checksum_status}. Expected: '{transfer.expected_checksum}', Calculated: '{transfer.calculated_checksum}' (Algo: {transfer.checksum_algorithm})")
            transfer.checksum_status = checksum_status # Ensure it's updated on the object if not already

            # Notify UI
            ui_message = f"DCC: Checksum for '{transfer.original_filename}' with {transfer.peer_nick}: {checksum_status}."
            color_key = "system"
            if checksum_status == "Mismatch":
                color_key = "error"
            elif checksum_status == "Match":
                color_key = "info" # Or a success color

            self.client_logic.add_message(ui_message, color_key, context_name="DCC")

            # Dispatch event
            self.event_manager.dispatch_event("DCC_TRANSFER_CHECKSUM_VALIDATED", {
                "transfer_id": transfer_id,
                "type": transfer.transfer_type.name,
                "nick": transfer.peer_nick,
                "filename": transfer.original_filename,
                "checksum_status": checksum_status,
                "expected_checksum": transfer.expected_checksum,
                "calculated_checksum": transfer.calculated_checksum,
                "algorithm_used": transfer.checksum_algorithm
            })
        else:
            self.dcc_event_logger.warning(f"update_transfer_checksum_result called for unknown transfer_id: {transfer_id}")

    def attempt_user_resume(self, identifier: str) -> Dict[str, Any]:
        """
        Attempts to resume a previously failed/cancelled outgoing DCC SEND transfer
        based on a user-provided identifier (transfer ID prefix or filename).
        """
        if not self.dcc_config.get("enabled"):
            return {"success": False, "error": "DCC is disabled."}
        if not self.dcc_config.get("resume_enabled"):
            return {"success": False, "error": "DCC resume is disabled in configuration."}

        self.dcc_event_logger.info(f"User attempt to resume transfer with identifier: '{identifier}'")

        resumable_transfer: Optional[DCCSendTransfer] = None

        with self._lock:
            # Try to find by transfer ID prefix first
            possible_matches_by_id = []
            for tid, transfer in self.transfers.items():
                if tid.startswith(identifier) and isinstance(transfer, DCCSendTransfer):
                    possible_matches_by_id.append(transfer)

            if len(possible_matches_by_id) == 1:
                resumable_transfer = possible_matches_by_id[0]
            elif len(possible_matches_by_id) > 1:
                self.dcc_event_logger.warning(f"Ambiguous identifier '{identifier}' for resume (multiple ID prefix matches).")
                return {"success": False, "error": f"Ambiguous transfer ID prefix '{identifier}'. Be more specific."}

            # If not found by ID prefix, try by filename (case-insensitive)
            if not resumable_transfer:
                possible_matches_by_filename = []
                for transfer in self.transfers.values():
                    if (isinstance(transfer, DCCSendTransfer) and
                        transfer.original_filename.lower() == identifier.lower()):
                        possible_matches_by_filename.append(transfer)

                if len(possible_matches_by_filename) == 1:
                    resumable_transfer = possible_matches_by_filename[0]
                elif len(possible_matches_by_filename) > 1:
                    self.dcc_event_logger.warning(f"Ambiguous identifier '{identifier}' for resume (multiple filename matches).")
                    # Consider listing them or asking for peer_nick if implementing more complex resume UI
                    return {"success": False, "error": f"Ambiguous filename '{identifier}'. Multiple transfers match. Try ID prefix."}

        if not resumable_transfer:
            self.dcc_event_logger.warning(f"No resumable SEND transfer found matching identifier '{identifier}'.")
            return {"success": False, "error": f"No SEND transfer found matching '{identifier}'."}

        # Check if the found transfer is in a state that allows resuming
        if resumable_transfer.status not in [DCCTransferStatus.FAILED, DCCTransferStatus.CANCELLED, DCCTransferStatus.TIMED_OUT]:
            self.dcc_event_logger.info(f"Transfer '{resumable_transfer.transfer_id}' ('{resumable_transfer.original_filename}') is not in a resumable state (current state: {resumable_transfer.status.name}).")
            return {"success": False, "error": f"Transfer '{resumable_transfer.original_filename}' is not in a failed/cancelled state ({resumable_transfer.status.name})."}

        if not (resumable_transfer.bytes_transferred > 0 and resumable_transfer.bytes_transferred < resumable_transfer.filesize):
            self.dcc_event_logger.info(f"Transfer '{resumable_transfer.transfer_id}' ('{resumable_transfer.original_filename}') has no partial progress ({resumable_transfer.bytes_transferred}/{resumable_transfer.filesize}). Cannot resume.")
            return {"success": False, "error": f"Transfer '{resumable_transfer.original_filename}' has no partial progress to resume from."}

        # At this point, we have a valid, resumable DCCSendTransfer.
        # Call _execute_send, which contains the logic to offer DCC RESUME CTCP.
        self.dcc_event_logger.info(f"Re-initiating send for '{resumable_transfer.original_filename}' to {resumable_transfer.peer_nick} (will offer resume from {resumable_transfer.bytes_transferred}).")

        # Note: _execute_send will create a *new* transfer object internally for the resume attempt.
        # The old 'resumable_transfer' object will remain in self.transfers unless explicitly removed by _execute_send's resume logic.
        # The current _execute_send logic for resume does not remove the old transfer object explicitly.
        # This means `self.transfers` might accumulate multiple attempts for the same logical file if resume is tried multiple times.
        # This might be desired for history, or might need cleanup later.

        return self.send_manager._execute_send_operation(
            peer_nick=resumable_transfer.peer_nick,
            local_filepath=resumable_transfer.local_filepath,
            original_filename=resumable_transfer.original_filename,
            filesize=resumable_transfer.filesize,
            passive=False # User-initiated resume is for active sends for now
        )
