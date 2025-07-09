"""Module for registering CLI plugins for jaseci."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pickle import load
from typing import AsyncIterator, Optional

from dotenv import load_dotenv
from fastapi.responses import FileResponse, Response, StreamingResponse
from jac_cloud.jaseci.main import FastAPI
from jac_cloud.jaseci.security import authenticator
from jac_cloud.plugin.jaseci import NodeAnchor
from jaclang import JacMachine as Jac
from jaclang.cli.cmdreg import cmd_registry
from jaclang.runtimelib.machine import hookimpl
from uvicorn import run as _run

from jvserve.lib.agent_interface import AgentInterface
from jvserve.lib.agent_pulse import AgentPulse
from jvserve.lib.file_interface import (
    DEFAULT_FILES_ROOT,
    FILE_INTERFACE,
    file_interface,
)
from jvserve.lib.jvlogger import JVLogger

load_dotenv(".env")


class JacCmd:
    """Jac CLI."""

    @staticmethod
    @hookimpl
    def create_cmd() -> None:
        """Create Jac CLI cmds."""

        @cmd_registry.register
        def jvserve(
            filename: str,
            host: str = "0.0.0.0",
            port: int = 8000,
            loglevel: str = "INFO",
        ) -> None:
            """Launch the jac application."""

            AgentInterface.HOST = host
            AgentInterface.PORT = port

            # set up logging
            JVLogger.setup_logging(level=loglevel)
            logger = logging.getLogger(__name__)

            base, mod = os.path.split(filename)
            base = base if base else "./"
            mod = mod[:-4]

            FastAPI.enable()
            if filename.endswith(".jac"):
                Jac.jac_import(target=mod, base_path=base, override_name="__main__")
            elif filename.endswith(".jir"):
                with open(filename, "rb") as f:
                    Jac.attach_program(load(f))
                    Jac.jac_import(target=mod, base_path=base, override_name="__main__")
            else:
                raise ValueError("Not a valid file!\nOnly supports `.jac` and `.jir`")

            # Define post-startup function to run AFTER server is ready
            async def post_startup() -> None:
                """Function to execute after server is fully operational"""
                # Minimal delay allows server to start listening
                await asyncio.sleep(0.01)
                await AgentInterface.init_agents()

            # set up lifespan events
            async def on_startup() -> None:
                # Perform initialization actions here
                logger.info("JIVAS is starting up...")
                asyncio.create_task(post_startup())

            async def on_shutdown() -> None:
                # Perform initialization actions here
                logger.info("JIVAS is shutting down...")
                AgentPulse.stop()

            app_lifespan = FastAPI.get().router.lifespan_context

            @asynccontextmanager
            async def lifespan_wrapper(app: FastAPI) -> AsyncIterator[Optional[str]]:
                await on_startup()
                async with app_lifespan(app) as maybe_state:
                    yield maybe_state
                await on_shutdown()

            FastAPI.get().router.lifespan_context = lifespan_wrapper

            # Setup custom routes
            FastAPI.get().add_api_route(
                "/interact", endpoint=AgentInterface.interact, methods=["POST"]
            )
            FastAPI.get().add_api_route(
                "/webhook/{key}",
                endpoint=AgentInterface.webhook_exec,
                methods=["GET", "POST"],
            )
            FastAPI.get().add_api_route(
                "/action/walker",
                endpoint=AgentInterface.action_walker_exec,
                methods=["POST"],
                dependencies=authenticator,
            )

            # run the app
            FastAPI.start(host=host, port=port)

        @cmd_registry.register
        def jvfileserve(
            directory: str, host: str = "0.0.0.0", port: int = 9000
        ) -> None:
            """Launch the file server for local files."""
            # load FastAPI
            from fastapi import FastAPI
            from fastapi.middleware.cors import CORSMiddleware
            from fastapi.staticfiles import StaticFiles

            # Setup custom routes
            app = FastAPI()

            # Add CORS middleware
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

            if not os.path.exists(directory):
                os.makedirs(directory)

            # Set the environment variable for the file root path
            os.environ["JIVAS_FILES_ROOT_PATH"] = directory

            # Mount the static files directory
            app.mount(
                "/files",
                StaticFiles(directory=directory),
                name="files",
            )

            # run the app
            _run(app, host=host, port=port)

        @cmd_registry.register
        def jvproxyserve(
            directory: str, host: str = "0.0.0.0", port: int = 9000
        ) -> None:
            """Launch the file proxy server for remote files."""
            # load FastAPI
            from fastapi import FastAPI
            from fastapi.middleware.cors import CORSMiddleware

            # Setup custom routes
            app = FastAPI()

            # Add CORS middleware
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

            # Add proxy routes only if using S3
            if FILE_INTERFACE == "s3":

                @app.get("/files/{file_path:path}", response_model=None)
                async def serve_file(
                    file_path: str,
                ) -> FileResponse | StreamingResponse | Response:
                    descriptor_path = os.environ["JIVAS_DESCRIPTOR_ROOT_PATH"]
                    if descriptor_path and descriptor_path in file_path:
                        return Response(status_code=403)

                    return serve_proxied_file(file_path)

            @app.get("/f/{file_id:path}", response_model=None)
            async def get_proxied_file(
                file_id: str,
            ) -> FileResponse | StreamingResponse | Response:
                from bson import ObjectId
                from fastapi import HTTPException

                params = file_id.split("/")
                object_id = params[0]

                # mongo db collection
                collection = NodeAnchor.Collection.get_collection("url_proxies")
                file_details = collection.find_one({"_id": ObjectId(object_id)})
                descriptor_path = os.environ["JIVAS_DESCRIPTOR_ROOT_PATH"]

                if file_details:
                    if descriptor_path and descriptor_path in file_details["path"]:
                        return Response(status_code=403)

                    return serve_proxied_file(file_details["path"])

                raise HTTPException(status_code=404, detail="File not found")

            # run the app
            _run(app, host=host, port=port)


def serve_proxied_file(file_path: str) -> FileResponse | StreamingResponse:
    """Serve a proxied file from a remote or local URL."""
    import mimetypes

    import requests
    from fastapi import HTTPException

    if FILE_INTERFACE == "local":
        return FileResponse(path=os.path.join(DEFAULT_FILES_ROOT, file_path))

    file_url = file_interface.get_file_url(file_path)

    if file_url and ("localhost" in file_url or "127.0.0.1" in file_url):
        # prevent recusive calls when env vars are not detected
        raise HTTPException(status_code=500, detail="Environment not set up correctly")

    if not file_url:
        raise HTTPException(status_code=404, detail="File not found")

    file_extension = os.path.splitext(file_path)[1].lower()

    # List of extensions to serve directly
    direct_serve_extensions = [
        ".pdf",
        ".html",
        ".txt",
        ".js",
        ".css",
        ".json",
        ".xml",
        ".svg",
        ".csv",
        ".ico",
    ]

    if file_extension in direct_serve_extensions:
        file_response = requests.get(file_url)
        file_response.raise_for_status()  # Raise HTTPError for bad responses (4XX or 5XX)

        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        return StreamingResponse(iter([file_response.content]), media_type=mime_type)

    file_response = requests.get(file_url, stream=True)
    file_response.raise_for_status()

    return StreamingResponse(
        file_response.iter_content(chunk_size=1024),
        media_type="application/octet-stream",
    )
