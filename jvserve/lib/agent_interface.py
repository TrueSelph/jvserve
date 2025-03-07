"""Agent Interface class and methods for interaction with Jivas."""

import json
import logging
import os
import string
import time
import traceback
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote

import aiohttp
import requests
from fastapi import Form, Request, UploadFile
from fastapi.responses import JSONResponse
from jac_cloud.core.architype import AnchorState, Permission, Root
from jac_cloud.core.context import (
    JASECI_CONTEXT,
    SUPER_ROOT,
    SUPER_ROOT_ID,
    ExecutionContext,
    JaseciContext,
)
from jac_cloud.core.memory import MongoDB
from jac_cloud.plugin.jaseci import NodeAnchor
from jaclang.plugin.feature import JacFeature as _Jac
from jaclang.runtimelib.machine import JacMachine
from pydantic import BaseModel


class AgentInterface:
    """Agent Interface for Jivas."""

    HOST = "localhost"
    PORT = 8000
    ROOT_ID = ""
    TOKEN = ""
    EXPIRATION = ""
    LOGGER = logging.getLogger(__name__)

    @staticmethod
    def spawn_walker(
        walker_name: str, module_name: str, attributes: dict
    ) -> _Jac.Walker:
        """Spawn any walker by name, located in module"""
        return JacMachine.get().spawn_walker(walker_name, attributes, module_name)

    @staticmethod
    async def webhook_exec(key: str, request: Request) -> JSONResponse:
        """
        Execute a walker by name within context
        The key combines the walker name, module name and agent_id in an encoded string
        """
        params = {}
        response = JSONResponse(status_code=200, content="200 OK")

        # Capture query parameters dynamically

        if query_params := request.query_params:
            params = query_params

        # Capture JSON body dynamically
        if request.method == "POST":
            try:
                params = await request.json()

            except Exception as e:
                AgentInterface.LOGGER.warning(
                    f"Missing or invalid JSON served via webhook call: {e}"
                )

        # decode the arguments
        args = AgentInterface.decrypt_webhook_key(key=key)

        if args:
            agent_id = args.get("agent_id")
            module_root = args.get("module_root")
            walker = args.get("walker")

            if not agent_id or not walker or not module_root:
                AgentInterface.LOGGER.error("malformed webhook key")
                return response
        else:
            AgentInterface.LOGGER.error("malformed webhook key")
            return response

        ctx = await AgentInterface.load_context_async()
        if ctx:
            # compose full module_path
            module = f"{module_root}.{walker}"
            try:
                response = _Jac.spawn_call(
                    ctx.entry_node.architype,
                    AgentInterface.spawn_walker(
                        walker_name=walker,
                        attributes={
                            "agent_id": agent_id,
                            "params": params,
                            "reporting": False,
                        },
                        module_name=module,
                    ),
                ).response

                if response:
                    if isinstance(response, str):
                        response = json.loads(response)
                    response = JSONResponse(
                        status_code=200, content=response, media_type="application/json"
                    )

            except Exception as e:
                AgentInterface.EXPIRATION = ""
                AgentInterface.LOGGER.error(
                    f"an exception occurred: {e}, {traceback.format_exc()}"
                )
        else:
            AgentInterface.LOGGER.error(f"unable to execute {walker}")

        ctx.close()

        return response

    @staticmethod
    async def action_walker_exec(
        request: Request,
        agent_id: str = Form(...),  # noqa: B008
        module_root: str = Form(...),  # noqa: B008
        walker: str = Form(...),  # noqa: B008
        args: Optional[str] = Form(None),  # noqa: B008
        attachments: Optional[list[UploadFile]] = None,
    ) -> JSONResponse:
        """Execute a named walker exposed by an action within context; capable of handling JSON or file data depending on request"""

        response = JSONResponse(status_code=500, content="unable to complete request")

        try:

            # add agent id as a standard
            attributes: Dict[str, Any] = {"agent_id": agent_id}

            # add any other args
            if args:
                attributes.update(json.loads(args))

            # Processing files if any were uploaded
            if attachments:
                attributes["files"] = []
                for file in attachments:
                    attributes["files"].append(
                        {
                            "name": file.filename,
                            "type": file.content_type,
                            "content": await file.read(),
                        }
                    )

            if not agent_id or not walker or not module_root:
                AgentInterface.LOGGER.error("missing parameters")
                return JSONResponse(
                    status_code=401, content="missing required parameters"
                )

        except Exception as e:
            AgentInterface.LOGGER.error(
                f"an exception occurred: {e}, {traceback.format_exc()}"
            )
            return JSONResponse(status_code=500, content="internal server error")

        ctx = await AgentInterface.load_context_async()
        if ctx:
            # compose full module_path
            module = f"{module_root}.{walker}"

            try:
                response = _Jac.spawn_call(
                    ctx.entry_node.architype,
                    AgentInterface.spawn_walker(
                        walker_name=walker,
                        attributes=attributes,
                        module_name=module,
                    ),
                ).response

            except Exception as e:
                AgentInterface.EXPIRATION = ""
                AgentInterface.LOGGER.error(
                    f"an exception occurred: {e}, {traceback.format_exc()}"
                )
        else:
            AgentInterface.LOGGER.error(f"unable to execute {walker}")

        ctx.close()

        return response

    class InteractPayload(BaseModel):
        """Payload for interacting with the agent."""

        agent_id: str
        utterance: str
        session_id: str
        tts: bool
        verbose: bool

    @staticmethod
    def interact(payload: InteractPayload) -> dict:
        """Interact with the agent."""

        response = None
        ctx = AgentInterface.load_context()
        session_id = payload.session_id if payload.session_id else ""

        if not ctx:
            return {}

        AgentInterface.LOGGER.debug(
            f"attempting to interact with agent {payload.agent_id} with user root {ctx.root}..."
        )

        try:
            response = _Jac.spawn_call(
                ctx.entry_node.architype,
                AgentInterface.spawn_walker(
                    walker_name="interact",
                    attributes={
                        "agent_id": payload.agent_id,
                        "utterance": payload.utterance,
                        "session_id": session_id,
                        "tts": payload.tts,
                        "verbose": payload.verbose,
                        "reporting": False,
                    },
                    module_name="agent.action.interact",
                ),
            ).response
        except Exception as e:
            AgentInterface.EXPIRATION = ""
            AgentInterface.LOGGER.error(
                f"an exception occurred: {e}, {traceback.format_exc()}"
            )

        ctx.close()
        return response if response else {}

    @staticmethod
    def pulse(action_label: str, agent_id: str = "") -> dict:
        """Interact with the agent."""

        response = None
        ctx = AgentInterface.load_context()

        if not ctx:
            return {}

        # let's do some cleanup on the way schedule passes params; it includes in the value the param=
        # we need to take this out if it exists..
        action_label = action_label.replace("action_label=", "")
        agent_id = agent_id.replace("agent_id=", "")

        # TODO : raise error in the event agent id is invalid
        AgentInterface.LOGGER.debug(
            f"attempting to interact with agent {agent_id} with user root {ctx.root}..."
        )

        try:
            response = _Jac.spawn_call(
                ctx.entry_node.architype,
                AgentInterface.spawn_walker(
                    walker_name="pulse",
                    attributes={
                        "action_label": action_label,
                        "agent_id": agent_id,
                        "reporting": True,
                    },
                    module_name="agent.action.pulse",
                ),
            ).response
        except Exception as e:
            AgentInterface.EXPIRATION = ""
            AgentInterface.LOGGER.error(
                f"an exception occurred: {e}, {traceback.format_exc()}"
            )

        ctx.close()
        return response if response else {}

    @staticmethod
    def api_pulse(action_label: str, agent_id: str) -> dict:
        """Interact with the agent pulse using API"""

        host = AgentInterface.HOST
        port = AgentInterface.PORT
        ctx = AgentInterface.get_user_context()

        if not ctx:
            return {}

        # let's do some cleanup on the way schedule passes params; it includes in the value the param=
        # we need to take this out if it exists..
        action_label = action_label.replace("action_label=", "")
        agent_id = agent_id.replace("agent_id=", "")

        endpoint = f"http://{host}:{port}/walker/pulse"

        if AgentInterface.TOKEN:

            try:
                headers = {}
                json = {"action_label": action_label, "agent_id": agent_id}
                headers["Authorization"] = "Bearer " + AgentInterface.TOKEN

                # call interact
                response = requests.post(endpoint, json=json, headers=headers)

                if response.status_code == 200:
                    result = response.json()
                    return result.get("reports", {})

                if response.status_code == 401:
                    AgentInterface.EXPIRATION = ""
                    return {}

            except Exception as e:
                AgentInterface.EXPIRATION = ""
                AgentInterface.LOGGER.error(
                    f"an exception occurred: {e}, {traceback.format_exc()}"
                )

        return {}

    @staticmethod
    def api_interact(payload: InteractPayload) -> dict:
        """Interact with the agent using API"""

        host = AgentInterface.HOST
        port = AgentInterface.PORT
        ctx = AgentInterface.get_user_context()
        session_id = payload.session_id if payload.session_id else ""

        if not ctx:
            return {}

        endpoint = f"http://{host}:{port}/walker/interact"

        if ctx["token"]:

            try:
                headers = {}
                json = {
                    "agent_id": payload.agent_id,
                    "utterance": payload.utterance,
                    "session_id": session_id,
                }
                headers["Authorization"] = "Bearer " + AgentInterface.TOKEN

                # call interact
                response = requests.post(endpoint, json=json, headers=headers)

                if response.status_code == 200:
                    result = response.json()
                    return result["reports"]

                if response.status_code == 401:
                    AgentInterface.EXPIRATION = ""
                    return {}

            except Exception as e:
                AgentInterface.EXPIRATION = ""
                AgentInterface.LOGGER.error(
                    f"an exception occurred: {e}, {traceback.format_exc()}"
                )

        return {}

    @staticmethod
    def load_context(entry: NodeAnchor | None = None) -> Optional[ExecutionContext]:
        """Load the execution context synchronously."""
        AgentInterface.get_user_context()
        return AgentInterface.get_jaseci_context(entry, AgentInterface.ROOT_ID)

    @staticmethod
    async def load_context_async(
        entry: NodeAnchor | None = None,
    ) -> Optional[ExecutionContext]:
        """Load the execution context asynchronously."""
        ctx = await AgentInterface.get_user_context_async()
        if ctx:
            AgentInterface.ROOT_ID = ctx["root_id"]
            AgentInterface.TOKEN = ctx["token"]
            AgentInterface.EXPIRATION = ctx["expiration"]
        return AgentInterface.get_jaseci_context(entry, AgentInterface.ROOT_ID)

    @staticmethod
    def get_jaseci_context(entry: NodeAnchor | None, root_id: str) -> ExecutionContext:
        """Build the execution context for the agent."""

        try:
            ctx = JaseciContext()
            ctx.base = ExecutionContext.get()
        except Exception as e:
            AgentInterface.LOGGER.error(
                f"an exception occurred: {e}, {traceback.format_exc()}"
            )
            return None

        ctx.mem = MongoDB()
        ctx.reports = []
        ctx.status = 200

        # load the user root graph
        user_root = NodeAnchor.ref(f"n:root:{root_id}")

        if not isinstance(system_root := ctx.mem.find_by_id(SUPER_ROOT), NodeAnchor):
            system_root = NodeAnchor(
                architype=object.__new__(Root),
                id=SUPER_ROOT_ID,
                access=Permission(),
                state=AnchorState(connected=True),
                persistent=True,
                edges=[],
            )
            system_root.architype.__jac__ = system_root
            NodeAnchor.Collection.insert_one(system_root.serialize())
            system_root.sync_hash()
            ctx.mem.set(system_root.id, system_root)

        ctx.system_root = system_root
        ctx.root = user_root if user_root else system_root
        ctx.entry_node = entry if entry else ctx.root

        if _ctx := JASECI_CONTEXT.get(None):
            _ctx.close()
        JASECI_CONTEXT.set(ctx)

        return ctx

    @staticmethod
    def get_user_context() -> Optional[dict]:
        """Set graph context for JIVAS if user is not logged in; attempt registration if login fails."""
        ctx: dict = {}
        host = AgentInterface.HOST
        port = AgentInterface.PORT

        # if user context still active, return it
        now = int(time.time())
        if (
            AgentInterface.EXPIRATION
            and AgentInterface.EXPIRATION.isdigit()
            and int(AgentInterface.EXPIRATION) > now
        ):
            return {
                "root_id": AgentInterface.ROOT_ID,
                "token": AgentInterface.TOKEN,
                "expiration": AgentInterface.EXPIRATION,
            }

        user = os.environ.get("JIVAS_USER")
        password = os.environ.get("JIVAS_PASSWORD")
        if not user or not password:
            AgentInterface.LOGGER.error(
                "JIVAS_USER and or JIVAS_PASSWORD environment variable is not set."
            )
            return ctx

        login_url = f"http://{host}:{port}/user/login"
        register_url = f"http://{host}:{port}/user/register"

        try:
            # Attempt to log in
            response = requests.post(
                login_url, json={"email": user, "password": password}
            )

            if response.status_code == 200:
                # Login successful, set the ROOT_ID
                ctx["root_id"] = AgentInterface.ROOT_ID = response.json()["user"][
                    "root_id"
                ]
                ctx["token"] = AgentInterface.TOKEN = response.json()["token"]
                ctx["expiration"] = AgentInterface.EXPIRATION = response.json()["user"][
                    "expiration"
                ]

            else:
                AgentInterface.LOGGER.info(
                    f"Login failed with status code {response.status_code}, attempting registration..."
                )

                # Attempt to register the user
                register_response = requests.post(
                    register_url, json={"email": user, "password": password}
                )

                if register_response.status_code == 201:
                    # Registration successful, now log in again
                    AgentInterface.LOGGER.info(
                        f"Registration successful for user {user}, attempting login again..."
                    )

                    # Re-attempt login after successful registration
                    login_response = requests.post(
                        login_url, json={"email": user, "password": password}
                    )

                    if login_response.status_code == 200:
                        AgentInterface.LOGGER.info(
                            f"Login successful after registration, ROOT_ID ({ctx['root_id']}) set for user {user}."
                        )
                    else:
                        AgentInterface.LOGGER.error(
                            f"Login failed after registration with status code {login_response.status_code}."
                        )
                else:
                    AgentInterface.LOGGER.error(
                        f"Registration failed with status code {register_response.status_code}."
                    )

        except Exception as e:
            AgentInterface.EXPIRATION = ""
            AgentInterface.LOGGER.error(
                f"an exception occurred: {e}, {traceback.format_exc()}"
            )

        return ctx

    @staticmethod
    async def get_user_context_async() -> Optional[dict]:
        """Set graph context for JIVAS if user is not logged in; attempt registration if login fails."""
        ctx: dict = {}
        host = AgentInterface.HOST
        port = AgentInterface.PORT

        # if user context still active, return it
        now = int(time.time())
        if (
            AgentInterface.EXPIRATION
            and AgentInterface.EXPIRATION.isdigit()
            and int(AgentInterface.EXPIRATION) > now
        ):
            return {
                "root_id": AgentInterface.ROOT_ID,
                "token": AgentInterface.TOKEN,
                "expiration": AgentInterface.EXPIRATION,
            }

        user = os.environ.get("JIVAS_USER")
        password = os.environ.get("JIVAS_PASSWORD")
        if not user or not password:
            AgentInterface.LOGGER.error(
                "JIVAS_USER and or JIVAS_PASSWORD environment variable is not set."
            )
            return ctx

        login_url = f"http://{host}:{port}/user/login"
        register_url = f"http://{host}:{port}/user/register"

        async with aiohttp.ClientSession() as session:
            try:
                # Attempt to log in
                async with session.post(
                    login_url, json={"email": user, "password": password}
                ) as response:
                    if response.status == 200:
                        # Login successful, set the ROOT_ID
                        data = await response.json()
                        ctx["root_id"] = AgentInterface.ROOT_ID = data["user"][
                            "root_id"
                        ]
                        ctx["token"] = AgentInterface.TOKEN = data["token"]
                        ctx["expiration"] = AgentInterface.EXPIRATION = data["user"][
                            "expiration"
                        ]
                    else:
                        AgentInterface.LOGGER.info(
                            f"Login failed with status code {response.status}, attempting registration..."
                        )

                        # Attempt to register the user
                        async with session.post(
                            register_url, json={"email": user, "password": password}
                        ) as register_response:
                            if register_response.status == 201:
                                AgentInterface.LOGGER.info(
                                    f"Registration successful for user {user}, attempting login again..."
                                )

                                # Re-attempt login after successful registration
                                async with session.post(
                                    login_url,
                                    json={"email": user, "password": password},
                                ) as login_response:
                                    if login_response.status == 200:
                                        data = await login_response.json()
                                        root_id = data["user"]["root_id"]
                                        ctx["root_id"] = root_id
                                        ctx["token"] = data["token"]
                                        ctx["expiration"] = data["user"]["expiration"]
                                        AgentInterface.LOGGER.info(
                                            f"Login successful after registration, ROOT_ID ({ctx['root_id']}) set for user {user}."
                                        )
                                    else:
                                        AgentInterface.LOGGER.error(
                                            f"Login failed after registration with status code {login_response.status}."
                                        )
                            else:
                                AgentInterface.LOGGER.error(
                                    f"Registration failed with status code {register_response.status}."
                                )

            except Exception as e:
                AgentInterface.EXPIRATION = ""
                AgentInterface.LOGGER.error(
                    f"an exception occurred: {e}, {traceback.format_exc()}"
                )

        return ctx

    @staticmethod
    def generate_cipher_alphabet() -> tuple[str, str]:
        """Generate a cipher alphabet for encryption."""
        # TODO: make this more secure
        secret_key = os.environ.get("JIVAS_WEBHOOK_SECRET_KEY", "ABCDEFGHIJK")
        secret_key = secret_key.lower() + secret_key.upper()
        seen = set()
        key_unique = "".join(
            seen.add(c) or c for c in secret_key if c not in seen and c.isalpha()  # type: ignore
        )
        remaining = "".join(
            c
            for c in string.ascii_lowercase + string.ascii_uppercase
            if c not in seen and c.isalpha()
        )
        return key_unique, remaining

    @staticmethod
    def encrypt_webhook_key(agent_id: str, module_root: str, walker: str) -> str:
        """Encrypt the webhook key."""
        lower_cipher_alphabet, upper_cipher_alphabet = (
            AgentInterface.generate_cipher_alphabet()
        )
        table = str.maketrans(
            string.ascii_lowercase + string.ascii_uppercase,
            lower_cipher_alphabet + upper_cipher_alphabet,
        )
        key_text = json.dumps(
            {"agent_id": agent_id, "module_root": module_root, "walker": walker},
            separators=(",", ":"),
        )

        # Translate using the cipher alphabet
        encoded_text = key_text.translate(table)

        # URL encode the translated output
        return quote(encoded_text)

    @staticmethod
    def decrypt_webhook_key(key: str) -> Optional[dict]:
        """Decrypt the webhook key."""
        lower_cipher_alphabet, upper_cipher_alphabet = (
            AgentInterface.generate_cipher_alphabet()
        )
        table = str.maketrans(
            lower_cipher_alphabet + upper_cipher_alphabet,
            string.ascii_lowercase + string.ascii_uppercase,
        )

        # Decode the URL-encoded string
        decoded_text = unquote(key)

        # Translate back using the cipher alphabet
        key_text = decoded_text.translate(table)

        # Convert the JSON string back to a dictionary
        try:
            return json.loads(key_text)
        except Exception as e:
            AgentInterface.LOGGER.error(
                f"an exception occurred: {e}, {traceback.format_exc()}"
            )
            return {}
