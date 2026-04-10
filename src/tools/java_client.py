import json
import queue
import subprocess
import os
import threading
from typing import Dict, Any, Optional


_RESPONSE_TIMEOUT_SECONDS = 30.0


class JavaMicroserviceClient:
    def __init__(self):
        jar_path = os.path.join(
            os.path.dirname(__file__),
            "../../java-microservice/target/omniport-java-service-1.0-SNAPSHOT.jar",
        )
        jar_path = os.path.abspath(jar_path)

        self.process = subprocess.Popen(
            ["java", "-jar", jar_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Consume the startup banner so it doesn't pollute JSON reads.
        self._read_line(timeout=10.0)

    def _read_line(self, timeout: float = _RESPONSE_TIMEOUT_SECONDS) -> Optional[str]:
        """
        Reads one line from the microservice stdout with a wall-clock timeout.
        Returns None on timeout or if the process has exited.
        """
        result: queue.Queue[Optional[str]] = queue.Queue()

        def _reader():
            try:
                line = self.process.stdout.readline()
                result.put(line if line else None)
            except Exception:
                result.put(None)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            return result.get(timeout=timeout)
        except queue.Empty:
            return None

    def _is_alive(self) -> bool:
        return self.process.poll() is None

    def send_request(self, command: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_alive():
            return {"status": "error", "message": "Java microservice process has exited"}

        req = {"command": command}
        req.update(payload)

        try:
            self.process.stdin.write(json.dumps(req) + "\n")
            self.process.stdin.flush()
        except BrokenPipeError:
            return {"status": "error", "message": "Broken pipe to Java microservice"}

        resp_str = self._read_line()
        if resp_str is None:
            return {"status": "error", "message": "Timeout or EOF reading from Java microservice"}

        try:
            return json.loads(resp_str)
        except json.JSONDecodeError as e:
            return {"status": "error", "message": f"Invalid JSON from service: {e}"}

    def close(self):
        if self.process:
            try:
                self.process.stdin.write("exit\n")
                self.process.stdin.flush()
            except Exception:
                pass
            self.process.terminate()
            self.process.wait()


# Module-level singleton — created on first use.
_client: Optional[JavaMicroserviceClient] = None


def get_java_client() -> JavaMicroserviceClient:
    global _client
    if _client is None:
        _client = JavaMicroserviceClient()
    return _client


def close_java_client():
    global _client
    if _client:
        _client.close()
        _client = None
