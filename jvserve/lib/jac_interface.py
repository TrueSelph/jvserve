""" JacInterface: A connection and context state provider for Jac Runtime."""

import os
import time
import asyncio
import logging
import requests
import threading
import traceback
from typing import Optional, Tuple
from jac_cloud.plugin.jaseci import JacPlugin
from jaclang.runtimelib.machine import JacMachine
from jac_cloud.core.archetype import (  # type: ignore
    AnchorState,
    NodeAnchor,
    Permission,
    Root,
    WalkerArchetype,
)
from jac_cloud.core.context import (
    JASECI_CONTEXT,
    SUPER_ROOT,
    SUPER_ROOT_ID,
    JaseciContext,
)

class JacInterface:
    """Thread-safe connection and context state provider for Jac Runtime with auto-authentication."""
    def __init__(self, host: str = "localhost", port: int = 8000):
        """Initialize JacInterface with host and port."""
        self.host = host
        self.port = port
        self.root_id = ""
        self.token = ""
        self.expiration = None
        self._lock = threading.RLock()  # Thread-safe lock
        self.logger = logging.getLogger(__name__)

    def update(self, root_id: str, token: str, expiration: float) -> None:
        """Thread-safe state update"""
        with self._lock:
            self.root_id = root_id
            self.token = token
            self.expiration = expiration

    def reset(self) -> None:
        """Thread-safe state reset"""
        with self._lock:
            self.root_id = ""
            self.token = ""
            self.expiration = None

    def is_valid(self) -> bool:
        """Thread-safe validity check"""
        with self._lock:
            return bool(
                self.token 
                and self.expiration 
                and self.expiration > time.time()
            )

    def get_state(self) -> Tuple[str, str, Optional[float]]:
        """Get current state with auto-authentication"""
        if not self.is_valid():
            self._authenticate()
        with self._lock:
            return (self.root_id, self.token, self.expiration)
    
    def get_context(self) -> Optional[JaseciContext]:
        """Get Jaseci context with proper thread safety"""
        state = self.get_state()
        if not state or self.is_valid() is False:
            self.logger.error("Failed to get valid state for Jaseci context")
            return None
        
        root_id = state[0]
        try:
            entry_node = NodeAnchor.ref(f"n:root:{root_id}")
            ctx = JaseciContext.create(None, entry_node)
            
            if not isinstance(system_root := ctx.mem.find_by_id(SUPER_ROOT), NodeAnchor):
                system_root = NodeAnchor(
                    archetype=object.__new__(Root),
                    id=SUPER_ROOT_ID,
                    access=Permission(),
                    state=AnchorState(connected=True),
                    persistent=True,
                    edges=[],
                )
                system_root.archetype.__jac__ = system_root
                NodeAnchor.Collection.insert_one(system_root.serialize())
                system_root.sync_hash()
                ctx.mem.set(system_root.id, system_root)

            ctx.system_root = system_root
            ctx.root = system_root
            ctx.entry_node = ctx.root

            if _ctx := JASECI_CONTEXT.get(None):
                _ctx.close()
            JASECI_CONTEXT.set(ctx)
            
            return ctx
        except Exception as e:
            self.logger.error(f"Failed to create JaseciContext: {e}\n{traceback.format_exc()}")
            return None

    def spawn_walker(
        self,
        walker_name: str | None,
        module_name: str,
        attributes: dict = {},
    ) -> Optional[WalkerArchetype]:
        """Spawn walker with proper context handling and thread safety"""
        
        if not all([walker_name, module_name]):
            self.logger.error("Missing required parameters for spawning walker")
            return None
        
        ctx = self.get_context()
        if not ctx:
            return None
        
        try:
            if module_name not in JacMachine.list_modules():
                self.logger.error(f"Module {module_name} not loaded")
                return None
            
            entry_node = ctx.entry_node.archetype
            walker_obj = JacMachine.spawn_walker(walker_name, attributes, module_name)
            return JacPlugin.spawn(walker_obj, entry_node)
        except Exception as e:
            self.logger.error(f"Error spawning walker: {e}\n{traceback.format_exc()}")
            return None
        finally:
            if ctx:
                ctx.close()
                if JASECI_CONTEXT.get(None) == ctx:
                    JASECI_CONTEXT.set(None)
        
    def _authenticate(self) -> None:
        """Thread-safe authentication with retry logic and improved error handling"""
        user = os.environ.get("JIVAS_USER")
        password = os.environ.get("JIVAS_PASSWORD")
        if not user or not password:
            self.logger.error("Missing JIVAS_USER or JIVAS_PASSWORD")
            return

        login_url = f"http://{self.host}:{self.port}/user/login"
        register_url = f"http://{self.host}:{self.port}/user/register"

        with self._lock:
            try:
                # Try login first
                response = requests.post(
                    login_url, 
                    json={"email": user, "password": password},
                    timeout=15
                )
                self.logger.info(f"Login response status: {response.status_code}")
                if response.status_code == 200:
                    self._process_auth_response(response.json())
                    return

                # Register if login fails
                reg_response = requests.post(
                    register_url, 
                    json={"email": user, "password": password},
                    timeout=15
                )
                self.logger.info(f"Register response status: {reg_response.status_code}")
                if reg_response.status_code == 201:
                    # Retry login after registration
                    login_response = requests.post(
                        login_url, 
                        json={"email": user, "password": password},
                        timeout=15
                    )
                    self.logger.info(f"Retry login response status: {login_response.status_code}")
                    if login_response.status_code == 200:
                        self._process_auth_response(login_response.json())
            except requests.exceptions.RequestException as e:
                self.logger.error(f"Network error during authentication: {e}")
            except Exception as e:
                self.logger.error(f"Authentication failed: {e}\n{traceback.format_exc()}")
                self.reset()

    def _process_auth_response(self, response_data: dict) -> None:
        """Process authentication response and update state"""
        user_data = response_data.get("user", {})
        root_id = user_data.get("root_id", "")
        token = response_data.get("token", "")
        expiration = user_data.get("expiration")
        
        if not all([root_id, token, expiration]):
            self.logger.error("Invalid authentication response")
            return
            
        self.update(root_id, token, expiration)

    # Async versions of methods
    async def _authenticate_async(self) -> None:
        """Asynchronous wrapper for authentication"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._authenticate)

    async def get_context_async(self) -> Optional[JaseciContext]:
        """Asynchronous wrapper for context creation"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_context)

    async def spawn_walker_async(
        self,
        walker_name: str,
        module_name: str,
        attributes: dict,
    ) -> Optional[WalkerArchetype]:
        """Asynchronous wrapper for walker spawning"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, 
            self.spawn_walker, 
            walker_name, 
            module_name,
            attributes
        )