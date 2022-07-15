import contextlib
import subprocess
import tempfile
from typing import Optional

from modal_proto import api_pb2


@contextlib.contextmanager
def proxy_tunnel(info: Optional[api_pb2.ProxyInfo]):
    if info is None:
        yield
        return

    with tempfile.NamedTemporaryFile(suffix=".pem") as t:
        f = open(t.name, "w")
        f.write(info.proxy_key)
        f.close()
        cmd = [
            "ssh",
            "-i",
            t.name,  # use pem file
            "-T",  # ignore the tty
            "-n",  # no input
            "-N",  # don't execute a command
            "-L",
            f"{info.remote_port}:{info.remote_addr}:{info.remote_port}",  # tunnel
            f"ubuntu@{info.elastic_ip}",
            "-o",
            "StrictHostKeyChecking=no",  # avoid prompt for host
        ]
        p = subprocess.Popen(cmd)

        import time

        time.sleep(3)
        try:
            yield
        finally:
            p.kill()
