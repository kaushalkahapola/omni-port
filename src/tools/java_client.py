import json
import subprocess
import os
import time
from typing import Dict, Any, Optional

class JavaMicroserviceClient:
    def __init__(self):
        jar_path = os.path.join(
            os.path.dirname(__file__), 
            "../../java-microservice/target/omniport-java-service-1.0-SNAPSHOT.jar"
        )
        jar_path = os.path.abspath(jar_path)
        
        self.process = subprocess.Popen(
            ["java", "-jar", jar_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        # Skip the startup message
        startup_msg = self.process.stdout.readline()

    def send_request(self, command: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = {"command": command}
        req.update(payload)
        
        req_str = json.dumps(req) + "\n"
        self.process.stdin.write(req_str)
        self.process.stdin.flush()
        
        resp_str = self.process.stdout.readline()
        if not resp_str:
            return {"status": "error", "message": "Microservice crashed or closed stdout"}
            
        try:
            return json.loads(resp_str)
        except json.JSONDecodeError as e:
            return {"status": "error", "message": f"Invalid JSON from service: {e}"}

    def close(self):
        if self.process:
            self.process.stdin.write("exit\n")
            self.process.stdin.flush()
            self.process.terminate()
            self.process.wait()

# Global instance
_client = None

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
