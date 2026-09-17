"""GPU-free contracts for the SSH/vLLM container entrypoint."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2] / "deploy" / "start_vllm_ssh.py"


def helper():
    assert HELPER.exists(), "SSH/vLLM startup helper has not been implemented"
    spec = importlib.util.spec_from_file_location("start_vllm_ssh", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "key",
    [
        "",
        "  ",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "ssh-ed25519 invalid!",
        "ssh-ed25519 AAAA\nssh-rsa AAAA",
    ],
)
def test_invalid_key_never_creates_authorization(tmp_path, key):
    with pytest.raises(ValueError):
        helper().install_public_key(key, tmp_path)
    assert not (tmp_path / "authorized_keys").exists()


def test_real_public_key_is_installed_with_private_permissions(tmp_path):
    if os.name != "posix" or not shutil.which("ssh-keygen"):
        pytest.skip("requires Linux and OpenSSH client")
    keyfile = tmp_path / "client"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keyfile)], check=True)
    target = tmp_path / "root-ssh"
    helper().install_public_key(keyfile.with_suffix(".pub").read_text(), target)
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "authorized_keys").stat().st_mode & 0o777 == 0o600
    subprocess.run(
        ["ssh-keygen", "-lf", str(target / "authorized_keys")], check=True, capture_output=True
    )


def test_launch_command_constrains_network_and_model():
    command = helper().vllm_command()
    assert command[:3] == [sys.executable, "-m", "vllm.entrypoints.openai.api_server"]
    args = dict(zip(command[3::2], command[4::2], strict=True))
    assert args == {
        "--model": "Qwen/Qwen3-4B-Instruct-2507",
        "--revision": "cdbee75f17c01a7cc42f958dc650907174af0554",
        "--tokenizer-revision": "cdbee75f17c01a7cc42f958dc650907174af0554",
        "--host": "127.0.0.1",
        "--port": "8000",
        "--dtype": "bfloat16",
        "--max-model-len": "4096",
        "--max-num-seqs": "1",
    }


@pytest.mark.skipif(os.name != "posix", reason="Linux process groups")
@pytest.mark.parametrize("failed_service", [0, 1])
def test_service_exit_stops_peer_and_preserves_failure(tmp_path, failed_service):
    survivor = tmp_path / "survivor.pid"
    long = [
        sys.executable,
        "-c",
        f"import os,time,pathlib; pathlib.Path({str(survivor)!r})"
        ".write_text(str(os.getpid())); time.sleep(60)",
    ]
    short = [sys.executable, "-c", "import time; time.sleep(0.3); raise SystemExit(7)"]
    commands = [long, long]
    commands[failed_service] = short
    assert helper().supervise(commands, grace_seconds=0.5) == 7
    pid = int(survivor.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="Linux signals")
def test_sigterm_stops_both_services(tmp_path):
    helper()
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\nsys.path.insert(0, " + repr(str(HELPER.parent)) + ")\n"
        "from start_vllm_ssh import supervise\n"
        'commands = [[sys.executable,"-c","import time; time.sleep(60)"]] * 2\n'
        "raise SystemExit(supervise(commands, grace_seconds=0.5))\n"
    )
    proc = subprocess.Popen([sys.executable, str(runner)])
    try:
        import time

        time.sleep(0.5)
        proc.terminate()
        assert proc.wait(timeout=5) == 143
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(os.name != "posix", reason="OpenSSH Linux integration")
def test_sshd_effective_policy_disallows_password_and_remote_forwarding(tmp_path):
    if not Path("/usr/sbin/sshd").exists() or not Path("/run/sshd").exists():
        pytest.skip("requires OpenSSH server with /run/sshd prepared")
    config = HELPER.parent / "sshd_config.vllm"
    hostkey = tmp_path / "host"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hostkey)], check=True)
    result = subprocess.run(
        ["/usr/sbin/sshd", "-T", "-f", str(config), "-h", str(hostkey)],
        check=True,
        capture_output=True,
        text=True,
    )
    settings = dict(line.split(" ", 1) for line in result.stdout.splitlines())
    assert settings["authenticationmethods"] == "publickey"
    assert settings["passwordauthentication"] == "no"
    assert settings["kbdinteractiveauthentication"] == "no"
    assert settings["permitrootlogin"] in {"prohibit-password", "without-password"}
    assert settings["allowtcpforwarding"] == "local"
    assert settings["permitopen"] == "127.0.0.1:8000"
    assert settings["gatewayports"] == "no"
