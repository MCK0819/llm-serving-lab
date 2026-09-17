"""Fail-closed SSH setup and joint lifecycle for the pinned GPU experiment."""

import base64
import binascii
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def install_public_key(value: str, directory: Path) -> None:
    fields = value.strip().split()
    if (
        "\n" in value.strip()
        or "\r" in value.strip()
        or len(fields) < 2
        or fields[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"}
    ):
        raise ValueError("LLM_LAB_SSH_PUBLIC_KEY must contain one OpenSSH public key")
    try:
        base64.b64decode(fields[1], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid public key encoding") from exc
    # OpenSSH validates the complete key blob; never accept authorized_keys options.
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as candidate:
        candidate.write(f"{fields[0]} {fields[1]}\n")
        candidate_path = Path(candidate.name)
    try:
        check = subprocess.run(
            ["ssh-keygen", "-lf", str(candidate_path)], capture_output=True, check=False
        )
        if check.returncode:
            raise ValueError("Invalid OpenSSH public key")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        destination = directory / "authorized_keys"
        destination.write_text(candidate_path.read_text())
        destination.chmod(0o600)
    finally:
        candidate_path.unlink()


def vllm_command() -> list[str]:
    revision = "cdbee75f17c01a7cc42f958dc650907174af0554"
    return [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        "Qwen/Qwen3-4B-Instruct-2507",
        "--revision",
        revision,
        "--tokenizer-revision",
        revision,
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--dtype",
        "bfloat16",
        "--max-model-len",
        "4096",
        "--max-num-seqs",
        "1",
    ]


def supervise(commands: list[list[str]], grace_seconds: float = 20) -> int:
    children: list[subprocess.Popen[bytes]] = []
    stopping = 0

    def stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = signum

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for command in commands:
            children.append(subprocess.Popen(command, start_new_session=True))
        while not stopping:
            for child in children:
                code = child.poll()
                if code is not None:
                    return code if code > 0 else 1
            time.sleep(0.1)
        return 128 + stopping
    finally:
        # Signal process groups even when the leader has already exited.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for child in children:
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
            if sig == signal.SIGTERM:
                deadline = time.monotonic() + grace_seconds
                for child in children:
                    try:
                        child.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        pass
        for child in children:
            child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> int:
    install_public_key(os.environ.pop("LLM_LAB_SSH_PUBLIC_KEY", ""), Path("/root/.ssh"))
    Path("/run/sshd").mkdir(mode=0o755, exist_ok=True)
    subprocess.run(["ssh-keygen", "-A"], check=True)
    subprocess.run(["/usr/sbin/sshd", "-t", "-f", "/etc/ssh/sshd_config.lab"], check=True)
    return supervise(
        [["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd_config.lab"], vllm_command()]
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Startup refused: {error}", file=sys.stderr)
        raise SystemExit(1) from None
