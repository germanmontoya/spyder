import asyncio
import logging
import signal
import socket

import asyncssh

from spyder.plugins.remoteclient.api.protocol import KernelConnectionInfo, DeleteKernel, KernelInfo, KernelsList, SSHClientOptions
from spyder.plugins.remoteclient.api.jupyterhub import JupyterHubAPI
from spyder.plugins.remoteclient.api.ssh import SpyderSSHClient
from spyder.plugins.remoteclient.utils.installation import get_installer_command


class SpyderRemoteClient:
    """Class to manage a remote server and its kernels."""

    _extra_options = ['platform', 'id']

    API_TOKEN = "GiJ96ujfLpPsq7oatW1IJuER01FbZsgyCM0xH6oMZXDAV6zUZsFy3xQBZakSBo6P"
    START_SERVER_COMMAND = "/${HOME}/.local/bin/micromamba run -n spyder-remote spyder-remote-server --juptyerhub"
    CHECK_SERVER_COMMAND = "/${HOME}/.local/bin/micromamba run -n spyder-remote spyder-remote-server -h"

    def __init__(self, conf_id, options: SSHClientOptions, _plugin=None):
        self._config_id = conf_id
        self.options = options
        self._plugin = _plugin

        self.ssh_connection: asyncssh.SSHClientConnection = None
        self.remote_server_process: asyncssh.SSHClientProcess = None
        self.port_forwarder: asyncssh.SSHListener = None
        self.server_port: int = None
        self.local_port: int = None

        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}({self.config_id})")

    async def close(self):
        """Closes the remote server and the SSH connection."""
        await self.close_port_forwarder()
        await self.stop_remote_server()
        await self.close_ssh_connection()

    @property
    def client_factory(self):
        """Return the client factory."""
        if self._plugin is None:
            return lambda: asyncssh.SSHClient()

        return lambda: SpyderSSHClient(self)

    @property
    def server_is_running(self):
        """Check if the remote server is running."""
        return self.remote_server_process is not None

    @property
    def ssh_is_connected(self):
        """Check if SSH connection is open."""
        return self.ssh_connection is not None

    @property
    def port_is_forwarded(self):
        """Check if local port is forwarded."""
        return self.port_forwarder is not None

    @property
    def config_id(self):
        """Return the configuration ID"""
        return self._config_id

    @property
    def server_url(self):
        """Return server URL

        Get the server URL with the fowarded local port that
        is used to connect to the spyder-remote-server.

        Returns
        -------
        str
            JupyterHub server URL

        Raises
        ------
        ValueError
            If the local port is not set.
        """
        if not self.local_port:
            raise ValueError("Local port is not set")
        return f"http://127.0.0.1:{self.local_port}"

    # -- Connection and server management
    async def connect_and_install_remote_server(self) -> bool:
        """Connect to the remote server and install the server."""
        if await self.create_new_connection():
            return await self.install_remote_server()

        return False

    async def connect_and_start_server(self) -> bool:
        """Connect to the remote server and start the server."""
        if await self.create_new_connection():
            return await self.start_remote_server()

        return False

    async def start_remote_server(self):
        """Start remote server."""
        if not self.ssh_is_connected:
            self._logger.error("SSH connection is not open")
            return False

        if self.server_is_running:
            await self.stop_remote_server()

        try:
            self.remote_server_process = (
                await self.ssh_connection.create_process(
                    self.START_SERVER_COMMAND,
                    stderr=asyncssh.STDOUT,
                )
            )
        except (OSError, asyncssh.Error, ValueError) as e:
            self._logger.error(f"Error starting remote server: {e}")
            self.remote_server_process = None
            return False

        self.server_port = await self.__extract_server_port()

        return await self.forward_local_port()

    async def check_server_installed(self) -> bool:
        """Check if remote server is installed."""
        if not self.ssh_is_connected:
            self._logger.error("SSH connection is not open")
            return False

        try:
            await self.ssh_connection.run(self.CHECK_SERVER_COMMAND, check=True)
        except asyncssh.ProcessError as err:
            self._logger.error(f"spyder-remote-server is not installed: {err.stderr}")
            return False
        except asyncssh.TimeoutError:
            self._logger.error("Checking if spyder-remote-server is installed timed out")
            return False

        return True

    async def install_remote_server(self):
        """Install remote server."""
        if not self.ssh_is_connected:
            self._logger.error("SSH connection is not open")
            return False

        command = get_installer_command(self.options['platform'])
        if not command:
            self._logger.error(f"Cannot install spyder-remote-server on {self.options['platform']} automatically. Please install it manually.")
            return False

        try:
            await self.ssh_connection.run(command, check=True)
        except asyncssh.ProcessError as err:
            self._logger.error(f"Instalation script failed: {err.stderr}")
            return False
        except asyncssh.TimeoutError:
            self._logger.error("Instalation script timed out")
            return False

        return True

    async def create_new_connection(self) -> bool:
        """Creates a new SSH connection to the remote server machine.

        Args
        ----
        options: dict[str, str]
            The options to use for the SSH connection.

        Returns
        -------
        bool
            True if the connection was successful, False otherwise.
        """
        if self.ssh_is_connected:
            await self.close_ssh_connection()

        conect_kwargs = {k: v for k, v in self.options.items() if k not in self._extra_options}
        try:
            self.ssh_connection = await asyncssh.connect(**conect_kwargs,
                                                         client_factory=self.client_factory)
        except (OSError, asyncssh.Error) as e:
            self._logger.error(f"Failed to open ssh connection: {e}")
            return False

        return True

    async def __extract_server_port(self) -> int | None:
        """Extract server port from server stdout.

        Returns
        -------
        int | None
            The server port if found, None otherwise.

        Raises
        ------
        ValueError
            If the server port is not found in the server stdout.
        """
        if not self.remote_server_process:
            return None

        port = None
        while port is None:
            line = await self.remote_server_process.stdout.readline()
            if not line:
                break
            if 'JupyterHub is now running at' in line:
                port = int(line.splitlines()[0].split(':')[-1].split('/')[0])
                break

        if not port:
            raise ValueError("Failed to extract port from server stdout")
        
        return port

    async def forward_local_port(self):
        """Forward local port."""
        if not self.server_port:
            self._logger.error("Server port is not set")
            return False

        if not self.ssh_is_connected:
            self._logger.error("SSH connection is not open")
            return False
        
        if self.port_forwarder:
            await self.close_port_forwarder()

        local_port = self.get_free_port()

        self.port_forwarder = await self.ssh_connection.forward_local_port(
            '',
            local_port,
            self.options['host'],
            self.server_port,
        )

        self.local_port = local_port

        self._logger.debug(f"Forwarding local port {local_port} to remote port {self.server_port}")

        return True

    async def close_port_forwarder(self):
        """Close port forwarder."""
        if self.port_is_forwarded:
            self.port_forwarder.close()
            await self.port_forwarder.wait_closed()
            self.port_forwarder = None
            self._logger.debug(f"Port forwarder closed for host {self.options['host']} with local port {self.local_port}")

    async def stop_remote_server(self):
        """Close remote server."""
        if self.server_is_running and not self.remote_server_process.is_closing():
            # bug in jupyterhub, need to send SIGINT twice
            self.remote_server_process.send_signal(signal.SIGINT)
            await asyncio.sleep(3)
            self.remote_server_process.send_signal(signal.SIGINT)
            self.remote_server_process.terminate()
            self.remote_server_process.close()
            await self.remote_server_process.wait_closed()
            self.remote_server_process = None
            self._logger.debug(f"Remote server process closed for {self.options['host']}")

    async def close_ssh_connection(self):
        """Close SSH connection."""
        if self.ssh_is_connected:
            self.ssh_connection.close()
            await self.ssh_connection.wait_closed()
            self.ssh_connection = None
            self._logger.debug(f"SSH connection closed for {self.options['host']}")

    # --- Kernel Management
    async def lauch_kernel_ensure_server(self, options: SSHClientOptions) -> KernelConnectionInfo:
        """Launch a new kernel ensuring the remote server is running.

        Parameters
        ----------
        options : SSHClientOptions
            The options to use for the SSH connection.

        Returns
        -------
        KernelConnectionInfo
            The kernel connection information.
        """
        if self.ssh_is_connected and not self.server_is_running:
            self.start_remote_server()
        elif not self.ssh_is_connected and not self.connect_and_start_server(options):
            self._logger.error("Cannot launch kernel, remote server is not running")
            return

        return await self.start_new_kernel()

    async def start_new_kernel(self) -> KernelConnectionInfo:
        """Start new kernel."""
        async with JupyterHubAPI(self.server_url, api_token=self.API_TOKEN) as hub:
            response = await hub.execute_post_service("spyder-service", "kernel")
        self._logger.info(f'spyder-service-kernel-post: {response}')
        return response

    async def get_kernels(self) -> KernelsList:
        """Get opened kernels."""
        async with JupyterHubAPI(self.server_url, api_token=self.API_TOKEN) as hub:
            response = await hub.execute_get_service("spyder-service", "kernel")
        self._logger.warning(f'spyder-service-kernel-get: {response}')
        return response

    async def get_kernel_info(self, kernel_key) -> KernelInfo:
        """Get kernel info."""
        async with JupyterHubAPI(self.server_url, api_token=self.API_TOKEN) as hub:
            response = await hub.execute_get_service("spyder-service", f"kernel/{kernel_key}")
        self._logger.info(f'spyder-service-kernel-get: {response}')
        return response

    async def terminate_kernel(self, kernel_key) -> DeleteKernel:
        """Terminate opened kernel."""
        async with JupyterHubAPI(self.server_url, api_token=self.API_TOKEN) as hub:
            response = await hub.execute_delete_service("spyder-service", f"kernel/{kernel_key}")
        self._logger.info(f'spyder-service-kernel-delete: {response}')
        return response

    @staticmethod
    def get_free_port():
        """Request a free port from the OS."""
        with socket.socket() as s:
            s.bind(('', 0))
            return s.getsockname()[1]
