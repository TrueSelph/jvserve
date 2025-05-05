"""Module for registering CLI plugins for jaseci."""

import logging
import os
import time
from contextlib import asynccontextmanager
from pickle import load
from typing import AsyncIterator, Optional

from dotenv import load_dotenv
from jac_cloud.jaseci.security import authenticator
from jaclang.cli.cmdreg import cmd_registry
from jaclang.runtimelib.machine import JacMachineState, hookimpl
from uvicorn import run as _run

from jvserve.lib.agent_interface import AgentInterface
from jvserve.lib.agent_pulse import AgentPulse
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
            workers: Optional[int] = None,
        ) -> None:
            """Launch the jac application."""
            from jac_cloud import FastAPI
            from jaclang import JacFeature as Jac

            base, mod = os.path.split(filename)
            base = base if base else "./"
            mod = mod[:-4]

            FastAPI.enable()
            mach = JacMachineState(base)

            # set up logging
            JVLogger.setup_logging(level=loglevel)
            logger = logging.getLogger(__name__)

            if filename.endswith(".jac"):
                start_time = time.time()
                Jac.jac_import(
                    mach=mach,
                    target=mod,
                    base_path=base,
                    override_name="__main__",
                )
                logger.info(f"Loading took {time.time() - start_time} seconds")
            elif filename.endswith(".jir"):
                with open(filename, "rb") as f:
                    Jac.attach_program(mach, load(f))
                    Jac.jac_import(
                        mach=mach,
                        target=mod,
                        base_path=base,
                        override_name="__main__",
                    )
            else:
                mach.exec_ctx.close()
                raise ValueError("Not a valid file!\nOnly supports `.jac` and `.jir`")

            AgentInterface.HOST = host
            AgentInterface.PORT = port

            # set up lifespan events
            async def on_startup() -> None:
                # Perform initialization actions here
                logger.info("JIVAS is starting up...")

            async def on_shutdown() -> None:
                # Perform initialization actions here
                logger.info("JIVAS is shutting down...")
                AgentPulse.stop()
                mach.exec_ctx.close()

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
            FastAPI.start(host=host, port=port, lifespan="on", workers=workers)

        @cmd_registry.register
        def jvfileserve(
            directory: str, host: str = "0.0.0.0", port: int = 9000
        ) -> None:
            """Launch the file server."""
            # load FastAPI
            from fastapi import FastAPI
            from fastapi.middleware.cors import CORSMiddleware
            from fastapi.staticfiles import StaticFiles

            if directory:
                os.environ["JIVAS_FILES_ROOT_PATH"] = directory

            if not os.path.exists(directory):
                os.makedirs(directory)

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

            app.mount(
                "/files",
                StaticFiles(
                    directory=os.environ.get("JIVAS_FILES_ROOT_PATH", ".files")
                ),
                name="files",
            )

            app.mount(
                "/files",
                StaticFiles(
                    directory=os.environ.get("JIVAS_FILES_ROOT_PATH", ".files")
                ),
                name="files",
            )

            # run the app
            _run(app, host=host, port=port)
