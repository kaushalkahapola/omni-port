"""MCP server wrapper for Java microservice integration."""

import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import httpx

logger = logging.getLogger(__name__)

# Java microservice configuration
JAVA_SERVICE_PORT = 8080
JAVA_SERVICE_URL = f"http://localhost:{JAVA_SERVICE_PORT}"
JAVA_SERVICE_JAR = Path(__file__).parent.parent.parent / "java-microservice" / "target" / "omniport-java-service-1.0-SNAPSHOT.jar"


class JavaMicroserviceClient:
    """HTTP client for Java microservice."""

    def __init__(self, base_url: str = JAVA_SERVICE_URL, timeout: float = 30.0):
        self.base_url = base_url
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    async def _ensure_running(self) -> bool:
        """Check if service is running, retry a few times."""
        for attempt in range(5):
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    resp = await client.get(f"{self.base_url}/api/gumtree/health")
                    return resp.status_code == 200
            except (httpx.ConnectError, httpx.TimeoutException):
                if attempt < 4:
                    await asyncio.sleep(1)
                else:
                    return False
        return False

    async def gumtree_diff(
        self, repo_path: str, file_path: str, old_content: str
    ) -> dict[str, Any]:
        """Compute GumTree diff."""
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        try:
            resp = await self._client.post(
                f"{self.base_url}/api/gumtree/diff",
                json={"repo_path": repo_path, "file_path": file_path, "old_content": old_content},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            return {"status": "error", "message": str(e)}

    async def javaparser_resolve(
        self, repo_path: str, file_path: str, symbols_to_resolve: list[str]
    ) -> dict[str, Any]:
        """Resolve symbols using JavaParser."""
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        try:
            resp = await self._client.post(
                f"{self.base_url}/api/javaparser/resolve",
                json={
                    "repo_path": repo_path,
                    "file_path": file_path,
                    "symbols_to_resolve": symbols_to_resolve,
                },
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            return {"status": "error", "message": str(e)}

    async def japicmp_compare(
        self, old_jar_path: str, new_jar_path: str
    ) -> dict[str, Any]:
        """Compare JARs using japicmp."""
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")

        try:
            resp = await self._client.post(
                f"{self.base_url}/api/japicmp/compare",
                json={"old_jar_path": old_jar_path, "new_jar_path": new_jar_path},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            return {"status": "error", "message": str(e)}


class JavaMicroserviceManager:
    """Manages Java microservice lifecycle."""

    def __init__(self, jar_path: Optional[Path] = None):
        self.jar_path = jar_path or JAVA_SERVICE_JAR
        self.process: Optional[subprocess.Popen] = None
        self.client = JavaMicroserviceClient()

    def start(self) -> bool:
        """Start the Java microservice."""
        if not self.jar_path.exists():
            logger.error(f"JAR not found at {self.jar_path}. Run: mvn clean package")
            return False

        try:
            logger.info(f"Starting Java microservice from {self.jar_path}")
            self.process = subprocess.Popen(
                ["java", "-jar", str(self.jar_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Wait for service to be ready
            for attempt in range(30):
                try:
                    import httpx
                    resp = httpx.get(f"{JAVA_SERVICE_URL}/api/gumtree/health", timeout=2)
                    if resp.status_code == 200:
                        logger.info("Java microservice is ready")
                        return True
                except Exception:
                    pass
                time.sleep(0.5)

            logger.error("Java microservice failed to start within timeout")
            self.stop()
            return False

        except Exception as e:
            logger.error(f"Failed to start Java microservice: {e}")
            return False

    def stop(self):
        """Stop the Java microservice."""
        if self.process:
            logger.info("Stopping Java microservice")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    async def get_client(self) -> JavaMicroserviceClient:
        """Get an initialized client (ensures service is running)."""
        if not self.process or self.process.poll() is not None:
            # Process not running, try to start it
            if not self.start():
                raise RuntimeError("Failed to start Java microservice")

        return self.client


# Global manager instance
_manager: Optional[JavaMicroserviceManager] = None


def get_manager() -> JavaMicroserviceManager:
    """Get or create the global manager."""
    global _manager
    if _manager is None:
        _manager = JavaMicroserviceManager()
    return _manager


async def gumtree_diff(
    repo_path: str, file_path: str, old_content: str
) -> dict[str, Any]:
    """Async wrapper for GumTree diff."""
    manager = get_manager()
    async with await manager.get_client() as client:
        return await client.gumtree_diff(repo_path, file_path, old_content)


async def javaparser_resolve(
    repo_path: str, file_path: str, symbols_to_resolve: list[str]
) -> dict[str, Any]:
    """Async wrapper for JavaParser symbol resolution."""
    manager = get_manager()
    async with await manager.get_client() as client:
        return await client.javaparser_resolve(repo_path, file_path, symbols_to_resolve)


async def japicmp_compare(
    old_jar_path: str, new_jar_path: str
) -> dict[str, Any]:
    """Async wrapper for japicmp JAR comparison."""
    manager = get_manager()
    async with await manager.get_client() as client:
        return await client.japicmp_compare(old_jar_path, new_jar_path)


def ensure_service_running():
    """Synchronously ensure service is running (for non-async contexts)."""
    manager = get_manager()
    if not manager.process or manager.process.poll() is not None:
        if not manager.start():
            raise RuntimeError("Failed to start Java microservice")
