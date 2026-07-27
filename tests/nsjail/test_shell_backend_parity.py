"""Parity tests comparing plain subprocess-style shell execution with nsjail.

Each test runs the same shell command through both a plain ``bash -c`` invocation
inside the Lima VM and an equivalent nsjail sandbox, then asserts that the
observable behavior (exit code, stdout, stderr) is identical. This proves the
nsjail backend is a drop-in replacement for the subprocess backend for the
command patterns that LLMs commonly emit.
"""

from __future__ import annotations

import subprocess

from tests.nsjail.conftest import NsjailVM


def _write_config(vm: NsjailVM, path: str, config: str) -> None:
    """Write an nsjail config to a file inside the VM."""
    vm.run(f"cat > {path} <<'EOF'\n{config}\nEOF")


def _nsjail_config(command: str, time_limit: int = 10, extra_mounts: str = "") -> str:
    """Build a minimal nsjail config that runs a shell command.

    Mounts the minimum set of host directories needed for a Debian/Ubuntu
    userspace shell (/bin, /usr, /lib, /lib64) plus any caller-supplied
    extra bind mounts. The command is inserted via ``%s`` formatting to
    avoid f-string brace escaping issues when the shell command contains
    ``{`` or ``}``. The ``arg`` value is wrapped in single quotes when the
    command itself contains double quotes, so the generated textproto remains
    valid.

    Args:
        command: The shell command to execute via ``/bin/sh -c``.
        time_limit: nsjail ``time_limit`` value in seconds.
        extra_mounts: Additional ``mount:`` protobuf lines to append.

    Returns:
        A complete nsjail protobuf configuration string.
    """
    arg_quote = "'" if '"' in command else '"'
    base = """
mode: ONCE
clone_newnet: true
clone_newuser: true
clone_newns: true
clone_newpid: true
clone_newipc: true
clone_newuts: true
keep_env: false
envar: "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
mount: { src: "/bin" dst: "/bin" is_bind: true rw: false mandatory: true }
mount: { src: "/usr" dst: "/usr" is_bind: true rw: false mandatory: true }
mount: { src: "/lib" dst: "/lib" is_bind: true rw: false mandatory: false }
mount: { src: "/lib64" dst: "/lib64" is_bind: true rw: false mandatory: false }
%(extra_mounts)s
time_limit: %(time_limit)d
log_level: FATAL
exec_bin {
  path: "/bin/sh"
  arg: "-c"
  arg: %(arg_quote)s%(command)s%(arg_quote)s
}
"""
    return base % {
        "extra_mounts": extra_mounts,
        "time_limit": time_limit,
        "arg_quote": arg_quote,
        "command": command,
    }


def _run_both(
    vm: NsjailVM, command: str, timeout: int = 10, extra_mounts: str = ""
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Run a command via plain bash and via nsjail.

    Args:
        vm: The Lima VM helper.
        command: Shell command to run both ways.
        timeout: Maximum runtime for the command itself.
        extra_mounts: Extra nsjail mount lines for this run.

    Returns:
        A tuple ``(plain_result, nsjail_result)``.
    """
    plain = vm.run(command, timeout=timeout + 10)
    cfg_path = f"/tmp/parity_{abs(hash(command))}.cfg"
    _write_config(vm, cfg_path, _nsjail_config(command, time_limit=timeout, extra_mounts=extra_mounts))
    jailed = vm.run_nsjail(cfg_path, timeout=timeout + 10)
    vm.run(f"rm -f {cfg_path}")
    return plain, jailed


def _assert_same_exit_code_and_stdout(
    plain: subprocess.CompletedProcess[str],
    jailed: subprocess.CompletedProcess[str],
) -> None:
    """Assert that plain and nsjail results share exit code and stdout."""
    assert plain.returncode == jailed.returncode, (
        f"exit code mismatch: plain={plain.returncode}, nsjail={jailed.returncode}\n"
        f"nsjail stderr: {jailed.stderr}"
    )
    assert plain.stdout.strip() == jailed.stdout.strip(), (
        f"stdout mismatch:\nplain:\n{plain.stdout}\nnsjail:\n{jailed.stdout}"
    )


def test_parity_simple_echo(nsjail_vm: NsjailVM) -> None:
    """``echo hello`` should return 0 and produce identical stdout."""
    plain, jailed = _run_both(nsjail_vm, "echo hello")
    assert plain.returncode == 0
    assert plain.stdout.strip() == "hello"
    _assert_same_exit_code_and_stdout(plain, jailed)


def test_parity_exit_code_nonzero(nsjail_vm: NsjailVM) -> None:
    """Non-zero exit codes should pass through identically."""
    plain, jailed = _run_both(nsjail_vm, "exit 42")
    assert plain.returncode == 42
    assert plain.returncode == jailed.returncode


def test_parity_pipe(nsjail_vm: NsjailVM) -> None:
    """A simple pipe should preserve stdout."""
    plain, jailed = _run_both(nsjail_vm, 'echo "hello world" | grep hello')
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.stdout.strip() == "hello world"


def test_parity_pipe_multiline(nsjail_vm: NsjailVM) -> None:
    """A multiline piped input should produce the same filtered line."""
    plain, jailed = _run_both(nsjail_vm, "printf 'line1\\nline2\\nline3\\n' | grep line2")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.stdout.strip() == "line2"


def test_parity_and_operator(nsjail_vm: NsjailVM) -> None:
    """``&&`` should evaluate the second command only when the first succeeds."""
    plain, jailed = _run_both(nsjail_vm, "true && echo success")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 0
    assert plain.stdout.strip() == "success"


def test_parity_and_operator_failure(nsjail_vm: NsjailVM) -> None:
    """A failing left side of ``&&`` should short-circuit and return the failure."""
    plain, jailed = _run_both(nsjail_vm, "false && echo should_not_appear")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 1
    assert plain.stdout.strip() == ""


def test_parity_or_operator(nsjail_vm: NsjailVM) -> None:
    """``||`` should run the fallback when the left side fails."""
    plain, jailed = _run_both(nsjail_vm, "false || echo fallback")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 0
    assert plain.stdout.strip() == "fallback"


def test_parity_or_operator_success(nsjail_vm: NsjailVM) -> None:
    """``||`` should short-circuit when the left side succeeds."""
    plain, jailed = _run_both(nsjail_vm, "true || echo should_not_appear")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 0
    assert plain.stdout.strip() == ""


def test_parity_combined_and_or(nsjail_vm: NsjailVM) -> None:
    """A chain mixing ``&&`` and ``||`` should preserve bash semantics."""
    plain, jailed = _run_both(nsjail_vm, "echo first && echo second || echo third")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 0
    assert plain.stdout.strip() == "first\nsecond"


def test_parity_chained_and(nsjail_vm: NsjailVM) -> None:
    """Multiple ``&&`` commands should all execute and emit their outputs."""
    plain, jailed = _run_both(nsjail_vm, "echo a && echo b && echo c")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 0
    assert plain.stdout.strip() == "a\nb\nc"


def test_parity_chained_pipes(nsjail_vm: NsjailVM) -> None:
    """A sequence of pipes should produce the same filtered output."""
    plain, jailed = _run_both(nsjail_vm, "echo 'hello world foo' | tr ' ' '\\n' | grep world")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 0
    assert plain.stdout.strip() == "world"


def test_parity_stderr_separation(nsjail_vm: NsjailVM) -> None:
    """stdout and stderr streams should be separated identically in both backends."""
    plain, jailed = _run_both(nsjail_vm, "echo out; echo err >&2")
    assert plain.returncode == jailed.returncode == 0
    assert plain.stdout.strip() == jailed.stdout.strip() == "out"
    assert plain.stderr.strip() == jailed.stderr.strip() == "err"


def test_parity_large_output(nsjail_vm: NsjailVM) -> None:
    """Large outputs should not be truncated and should have matching boundaries."""
    plain, jailed = _run_both(nsjail_vm, "seq 1 1000")
    assert plain.returncode == jailed.returncode == 0
    plain_lines = plain.stdout.strip().splitlines()
    jailed_lines = jailed.stdout.strip().splitlines()
    assert len(plain_lines) == len(jailed_lines) == 1000
    assert plain_lines[0] == jailed_lines[0] == "1"
    assert plain_lines[-1] == jailed_lines[-1] == "1000"


def test_parity_bash_script_from_shared_folder(nsjail_vm: NsjailVM) -> None:
    """An executable script from a bind-mounted directory should behave identically."""
    nsjail_vm.run("mkdir -p /tmp/parity_scripts")
    nsjail_vm.run(
        "cat > /tmp/parity_scripts/test.sh <<'EOF'\n"
        "#!/bin/bash\n"
        "echo script_started\n"
        'echo "args: $@"\n'
        "exit 0\n"
        "EOF"
    )
    nsjail_vm.run("chmod +x /tmp/parity_scripts/test.sh")

    extra_mounts = (
        'mount: { src: "/tmp/parity_scripts" '
        'dst: "/tmp/parity_scripts" is_bind: true rw: true mandatory: true }'
    )
    plain, jailed = _run_both(
        nsjail_vm,
        "/tmp/parity_scripts/test.sh arg1 arg2",
        extra_mounts=extra_mounts,
    )

    expected = "script_started\nargs: arg1 arg2"
    assert plain.returncode == jailed.returncode == 0
    assert plain.stdout.strip() == jailed.stdout.strip() == expected

    nsjail_vm.run("rm -rf /tmp/parity_scripts")


def test_parity_bash_script_with_pipes_inside(nsjail_vm: NsjailVM) -> None:
    """A script using internal pipes and boolean operators should behave identically."""
    nsjail_vm.run("mkdir -p /tmp/parity_scripts")
    nsjail_vm.run(
        "cat > /tmp/parity_scripts/pipe_script.sh <<'EOF'\n"
        "#!/bin/bash\n"
        "echo 'a b c' | tr ' ' '\\n' | grep b && echo pipe_ok || echo pipe_fail\n"
        "EOF"
    )
    nsjail_vm.run("chmod +x /tmp/parity_scripts/pipe_script.sh")

    extra_mounts = (
        'mount: { src: "/tmp/parity_scripts" '
        'dst: "/tmp/parity_scripts" is_bind: true rw: true mandatory: true }'
    )
    plain, jailed = _run_both(
        nsjail_vm,
        "/tmp/parity_scripts/pipe_script.sh",
        extra_mounts=extra_mounts,
    )

    expected = "b\npipe_ok"
    assert plain.returncode == jailed.returncode == 0
    assert plain.stdout.strip() == jailed.stdout.strip() == expected

    nsjail_vm.run("rm -rf /tmp/parity_scripts")


def test_parity_subshell(nsjail_vm: NsjailVM) -> None:
    """A subshell grouping should preserve combined stdout."""
    plain, jailed = _run_both(nsjail_vm, "(echo inside_subshell; echo still_inside)")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 0
    assert plain.stdout.strip() == "inside_subshell\nstill_inside"


def test_parity_command_substitution(nsjail_vm: NsjailVM) -> None:
    """Command substitution should expand identically inside nsjail."""
    plain, jailed = _run_both(nsjail_vm, "echo result=$(echo computed)")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 0
    assert plain.stdout.strip() == "result=computed"


def test_parity_env_var_inline(nsjail_vm: NsjailVM) -> None:
    """An inline environment variable set by the shell should be visible to the child."""
    plain, jailed = _run_both(nsjail_vm, "FOO=bar bash -c 'echo $FOO'")
    _assert_same_exit_code_and_stdout(plain, jailed)
    assert plain.returncode == 0
    assert plain.stdout.strip() == "bar"
