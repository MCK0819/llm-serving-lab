"""Disposable GPU-free verification, run inside the candidate image with --network none."""

import importlib.metadata
import importlib.util
import json
import pathlib
import socket
import subprocess
import tempfile
import threading
import time

spec = importlib.util.spec_from_file_location("startup", "/opt/llm-lab/start_vllm_ssh.py")
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)
assert not list(pathlib.Path("/etc/ssh").glob("ssh_host_*_key")), "Baked host key"
assert not pathlib.Path("/root/.ssh/authorized_keys").exists(), "Baked authorized key"
with tempfile.TemporaryDirectory() as directory:
    root = pathlib.Path(directory)
    for name in ("client", "host"):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)], check=True
        )
    startup.install_public_key((root / "client.pub").read_text(), pathlib.Path("/root/.ssh"))
    key = (root / "host.pub").read_text().split()
    (root / "known_hosts").write_text(f"[127.0.0.1]:2222 {key[0]} {key[1]}\n")
    sshd = subprocess.Popen(
        [
            "/usr/sbin/sshd",
            "-D",
            "-e",
            "-f",
            "/etc/ssh/sshd_config.lab",
            "-h",
            str(root / "host"),
            "-p",
            "2222",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    tunnel = None
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 8000))
        listener.listen()
        listener.settimeout(15)

        def serve():
            conn, _ = listener.accept()
            with conn:
                conn.recv(1024)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        while True:
            if sshd.poll() is not None:
                raise RuntimeError("sshd exited")
            try:
                with socket.create_connection(("127.0.0.1", 2222), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        tunnel = subprocess.Popen(
            [
                "ssh",
                "-N",
                "-T",
                "-i",
                str(root / "client"),
                "-p",
                "2222",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={root / 'known_hosts'}",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                "127.0.0.1:18000:127.0.0.1:8000",
                "root@127.0.0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        while True:
            if tunnel.poll() is not None:
                raise RuntimeError("SSH tunnel failed: " + tunnel.stderr.read().decode())
            try:
                conn = socket.create_connection(("127.0.0.1", 18000), timeout=1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        with conn:
            conn.settimeout(5)
            conn.sendall(b"GET / HTTP/1.0\r\n\r\n")
            response = b""
            while data := conn.recv(1024):
                response += data
        assert response.endswith(b"\r\n\r\nok"), response
        worker.join(timeout=2)
        print(
            json.dumps(
                {
                    "ssh_tunnel": "passed",
                    "vllm_version": importlib.metadata.version("vllm"),
                    "model_started": False,
                    "baked_keys": False,
                }
            )
        )
    finally:
        listener.close()
        for proc in (tunnel, sshd):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
