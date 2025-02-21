"""Test case for the Jac CLI commands."""

import os
import subprocess
import unittest
from contextlib import suppress
from time import sleep
from typing import Optional
import httpx


class JVServeCliTest(unittest.TestCase):
    """Test the Jac CLI commands."""

    def setUp(self) -> None:
        """Setup the test environment."""
        self.host = "http://0.0.0.0:8000"
        self.server_process: Optional[subprocess.Popen] = None

    def run_jvserve(self, filename: str, wait: int = 5) -> None:
        """Run jvserve in a subprocess."""
        # Ensure any process running on port 8000 is terminated
        subprocess.run(["fuser", "-k", "8000/tcp"], capture_output=True, text=True)

        # Create a temporary .jac file for testing
        with open(filename, "w") as f:
            f.write("with entry {print('Test Execution');}")

        # Launch `jvserve`
        self.server_process = subprocess.Popen(
            ["jac", "jvserve", filename, "--port", "8000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        sleep(wait)  # Allow some time for the server to start

        # Validate the server is running
        self.check_server()

    def stop_server(self) -> None:
        """Stop the running server."""
        if self.server_process:
            self.server_process.kill()

    def check_server(self) -> None:
        """Ensure the server is responding."""
        with suppress(Exception):
            res = httpx.get(f"{self.host}/healthz")
            res.raise_for_status()
            self.assertEqual(res.status_code, 200)

    def test_jvserve_runs(self) -> None:
        """Ensure `jac jvserve` runs successfully."""
        try:
            self.run_jvserve("test.jac")
            # Check if server started successfully
            res = httpx.get(f"{self.host}/docs")
            self.assertEqual(res.status_code, 200)
        finally:
            self.stop_server()

    def test_action_walker_requires_auth(self) -> None:
        """Ensure /action/walker requires authentication."""
        try:
            self.run_jvserve("test.jac")
            res = httpx.post(f"{self.host}/action/walker", json={})
            self.assertEqual(res.status_code, 403)  # Should be Not Authenticated / Forbidden
        finally:
            self.stop_server()

    def test_jvfileserve_runs(self) -> None:
        """Ensure `jac jvfileserve` runs successfully."""
        directory = "test_files"
        os.makedirs(directory, exist_ok=True)

        # Add file to the directory
        with open(f"{directory}/test.txt", "w") as f:
            f.write("Hello, World!")
        
        env = os.environ.copy()
        env["COVERAGE_PROCESS_START"] = ".coveragerc"  # Make sure .coveragerc exists
        env["PYTHONPATH"] = os.getcwd()

        try:
            server_process = subprocess.Popen(
                ["jac", "jvfileserve", directory, "--port", "9000"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            sleep(5)  # Give the server time to start

            res = httpx.get("http://0.0.0.0:9000/files/test.txt")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.text, "Hello, World!")

        finally:
            server_process.kill()

            # Clean up the directory
            os.remove(f"{directory}/test.txt")
            os.rmdir(directory )


    def tearDown(self) -> None:
        """Cleanup after each test."""
        self.stop_server()
        with suppress(FileNotFoundError):
            os.remove("test.jac")

if __name__ == "__main__":
    unittest.main()
