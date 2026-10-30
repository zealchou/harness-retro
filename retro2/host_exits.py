#!/usr/bin/env python3
"""Trusted host exits for subprocesses; adapters only consume structured results."""
import base64
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
import config


class HostExitError(RuntimeError):
    pass


# Child environment is an allow-list. Agent sockets are not on it:
# SSH_AUTH_SOCK, SSH_AGENT_PID, GPG_AGENT_INFO, DBUS_SESSION_BUS_ADDRESS.
# PATH: selected executable. HOME: path the CLI expects, not a mounted tree.
# LANG/LC_*/TERM: child text. Model endpoint knobs only, then overwritten
# with the relocated credential paths when those files are mounted.
CHILD_ENVIRONMENT = ("PATH", "HOME", "LANG", "TERM")
MODEL_ENVIRONMENT = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CREDENTIAL_FILE",
)
CODEX_BACKEND_DISABLED = (
    "Codex backend is disabled until the host-side credential broker ships; "
    "see docs/sandbox-allowlist.md#follow-up-credential-broker"
)
SECRET_DIRECTORIES = (".ssh", ".aws", ".gnupg", ".config/gh", ".docker", ".kube", ".codex", ".claude")
CHILD_SENTINEL = "R2_CHILD_STARTED\n"
# Mount sources are checked immediately before spawn. A later stage re-checks
# because an earlier stage can plant a symlink in the worktree. The check and
# bubblewrap's mount are not atomic: this assumes no concurrent mutation of
# those paths in between.
HARNESS_READ_NAMES = ("CLAUDE.md", "settings.json", "hooks", "scripts/logs")
NAMED_HARNESS_FILES = {Path(name) for name in HARNESS_READ_NAMES}
SYSTEM_FILES = (
    "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd",
    "/etc/group", "/etc/localtime", "/etc/ssl", "/etc/ca-certificates",
)
SANDBOX_PROFILES = {"diagnosis", "candidate", "apply"}


def _absolute(path):
    """Absolute path with ``.`` and ``..`` removed. Symlinks are not followed."""
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    parts = []
    for part in path.parts:
        if part == "/":
            parts = []
            continue
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return Path("/").joinpath(*parts)


def _inside(child, parent):
    child, parent = _absolute(child), _absolute(parent)
    if child == parent:
        return False
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_no_symlink(path):
    """lstat every component. Refuse a symlink. Return the path, not a resolved target."""
    path = _absolute(path)
    if path == Path("/"):
        raise HostExitError("refusing to mount /")
    current = Path("/")
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise HostExitError(f"allowlisted path missing: {path}") from error
        if stat.S_ISLNK(mode):
            raise HostExitError(f"allowlisted path is a symlink: {current}")
    return current


def _named_harness_file(path, home, harness):
    path = _absolute(path)
    for root in (_absolute(home / ".claude"), _absolute(harness)):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if relative in NAMED_HARNESS_FILES:
            return True
    return False


def _forbidden_mount(path, home, harness, *, named_ok=False):
    """True when `path` is, contains, or sits inside a credential root.

    ``$HOME`` itself and any path that contains ``$HOME`` are refused.
    ``~/.ssh``, ``~/.claude``, ``~/.codex``, and ``RETRO2_HOME`` are refused
    as a tree, including their contents. The named harness files are the
    exception when ``named_ok`` is set.
    """
    path = _absolute(path)
    home = _absolute(home)
    if path == home or _inside(home, path):
        return True
    trees = [home / ".ssh", home / ".claude", home / ".codex", _absolute(harness)]
    for tree in trees:
        tree = _absolute(tree)
        if path == tree or _inside(path, tree) or _inside(tree, path):
            if named_ok and _named_harness_file(path, home, harness):
                return False
            return True
    return False


def _private_auth_dir(prefix, visible, namespace=None):
    """A /tmp directory that is not inside a path the model is allowed to read."""
    dest_dir = Path("/tmp") / (prefix + (namespace or secrets.token_hex(8)))
    visible = [_absolute(path) for path in visible]
    if any(dest_dir == path or _inside(dest_dir, path) or _inside(path, dest_dir) for path in visible):
        raise HostExitError("credential mount overlaps a model-visible root")
    return dest_dir


def _added_directories(argv):
    found = []
    index = 0
    while index < len(argv):
        if argv[index] == "--add-dir" and index + 1 < len(argv):
            found.append(_absolute(argv[index + 1]))
            index += 2
            continue
        index += 1
    return found


def restricted_environment(source=None):
    source = os.environ if source is None else source
    env = {key: source[key] for key in CHILD_ENVIRONMENT if source.get(key)}
    env.setdefault("PATH", os.defpath)
    env.setdefault("HOME", str(Path.home()))
    env.update({key: value for key, value in source.items() if key.startswith("LC_") and source.get(key)})
    env.update({key: source[key] for key in MODEL_ENVIRONMENT if source.get(key)})
    for banned in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO", "DBUS_SESSION_BUS_ADDRESS"):
        env.pop(banned, None)
    return env


def model_backend_environment(source=None):
    """Pass the selected retro backend through detached queue/worker boundaries."""
    source = os.environ if source is None else source
    env = dict(source)
    if "RETRO2_MODEL_BACKEND" in source:
        env["RETRO2_MODEL_BACKEND"] = source["RETRO2_MODEL_BACKEND"]
    return env


def _mount_system(command):
    usr = Path("/usr")
    if usr.is_dir() and not usr.is_symlink():
        command += ["--ro-bind", "/usr", "/usr"]
    for link in ("/bin", "/sbin", "/lib", "/lib64"):
        path = Path(link)
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            # Recreate the symlink. Do not ro-bind its target as the link path.
            command += ["--symlink", os.readlink(path), link]
        elif path.is_dir():
            command += ["--ro-bind", link, link]
    for filename in SYSTEM_FILES:
        path = Path(filename)
        if not path.exists() and not path.is_symlink():
            continue
        if any(current.is_symlink() for current in path.parents if current != Path("/")):
            continue
        if path.is_symlink():
            command += ["--symlink", os.readlink(path), str(path)]
            continue
        try:
            checked = _assert_no_symlink(path)
        except HostExitError:
            continue
        command += ["--ro-bind", str(checked), str(checked)]


def _npm_package_dirs(resolved):
    """The npm package that contains `resolved`, plus sibling platform packages."""
    parts = resolved.parts
    if "node_modules" not in parts:
        return []
    index = len(parts) - 1 - parts[::-1].index("node_modules")
    rest = parts[index + 1:]
    if not rest:
        return []
    if rest[0].startswith("@") and len(rest) >= 2:
        package_root = Path(*parts[: index + 3])
        sibling_parent = package_root.parent
    else:
        package_root = Path(*parts[: index + 2])
        sibling_parent = package_root.parent
    if not package_root.is_dir():
        return []
    mounts = [package_root]
    prefix = package_root.name + "-"
    try:
        children = list(sibling_parent.iterdir())
    except OSError:
        children = []
    for child in children:
        if child.is_dir() and not child.is_symlink() and child != package_root and child.name.startswith(prefix):
            mounts.append(child)
    return mounts


def _under_home(path, home):
    path, home = _absolute(path), _absolute(home)
    return path == home or _inside(path, home)


def _runtime_mounts(argv, home):
    """Mount the CLI install directory. Never mount ``$HOME`` or ``~/.nvm``.

    Claude is an npm package. The sandbox follows the bin symlink once to find
    that package directory, then mounts the directory only when every
    component is a real path outside ``$HOME`` and ``~/.nvm``. A ``node``
    binary outside ``/usr`` and outside ``$HOME`` is mounted too. The child
    executes the real file inside the package mount.
    """
    argv = [str(item) for item in argv]
    if not argv:
        return [], argv
    executable = Path(argv[0])
    if not executable.is_file():
        return [], argv
    resolved = executable.resolve()
    if any(part == ".nvm" for part in resolved.parts) or _under_home(resolved, home / ".nvm"):
        raise HostExitError("refusing to mount a CLI install under HOME or .nvm")
    mounts = []
    package_dirs = _npm_package_dirs(resolved)
    if package_dirs:
        for directory in package_dirs:
            if _under_home(directory, home):
                raise HostExitError("refusing to mount a CLI install under HOME or .nvm")
            mounts.append(_assert_no_symlink(directory))
        argv[0] = str(resolved)
    elif not _inside(resolved, Path("/usr")) and resolved != Path("/usr"):
        # A single file. Mounting it does not mount $HOME even if the file lives there.
        mounts.append(_assert_no_symlink(resolved))
    text = ""
    try:
        text = resolved.read_text(encoding="utf-8", errors="ignore")[:80]
    except OSError:
        text = ""
    needs_node = text.startswith("#!") and "node" in text
    # Claude's package is a native executable. Still mount node when it is
    # outside $HOME so an npm-installed Claude that shells out to node can start.
    node = shutil.which("node")
    if node and (needs_node or package_dirs):
        node_path = Path(node).resolve()
        if _under_home(node_path, home):
            if needs_node:
                raise HostExitError("refusing to mount node from HOME or .nvm")
        elif not (_inside(node_path, Path("/usr")) or node_path == Path("/usr")):
            mounts.append(_assert_no_symlink(node_path))
    return mounts, argv


def project_mounts(project_root, home, harness_root):
    """Repo paths that can be mounted without exposing HOME or the harness.

    The project directory is mounted whole when it is not ``$HOME`` and does
    not contain ``$HOME``, ``~/.ssh``, ``~/.claude``, ``~/.codex``, or
    ``RETRO2_HOME``. Otherwise only subtrees that contain none of those
    directories are mounted. A symlink encountered while walking is refused.
    Named harness files are mounted separately.
    """
    home = _absolute(home)
    harness = _absolute(harness_root)
    secret_roots = [_absolute(home / ".ssh"), _absolute(home / ".claude"), _absolute(home / ".codex"), harness]

    def walk(path):
        path = _assert_no_symlink(path)
        # $HOME and credential trees are not mounted, and their children are not
        # walked. Named harness files are mounted separately.
        if path == home or any(path == root or _inside(path, root) for root in secret_roots):
            return
        if _inside(home, path) or any(_inside(root, path) for root in secret_roots):
            if path.is_dir():
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    if child.is_symlink():
                        raise HostExitError(f"allowlisted path is a symlink: {child}")
                    yield from walk(child)
            return
        yield path

    return list(walk(_absolute(project_root)))


def _ro_bind(command, path, home, harness, *, named_ok=False):
    checked = _assert_no_symlink(path)
    if _forbidden_mount(checked, home, harness, named_ok=named_ok):
        raise HostExitError(f"refusing to mount credential or home path: {checked}")
    command += ["--ro-bind", str(checked), str(checked)]
    return checked


def build_allowlisted_argv(argv, writable_root, *, cwd, env=None,
                           allow_network=False, profile="diagnosis",
                           project_root=None, harness_root=None,
                           harness_reads=(), diagnostics=None, credential_paths=(),
                           subject_root=None, runtime_argv=None, credential_namespace=None):
    """Run a child on an empty root. ``profile`` is diagnosis, candidate, or apply.

    Diagnosis mounts the project read-only, the named harness files, diagnostics,
    one writable scratch directory, and one relocated credential file.
    Candidate mounts the worktree read-write, the CLI install, and that credential.
    Apply mounts the subject read-only and the scratch directory read-write, with
    network disabled and no credential file. Nothing else from ``$HOME`` is mounted.

    Re-checked on every call, including each stage spawn. Assumes no concurrent
    mutation of the mount sources between this check and bubblewrap's mount.
    """
    if profile not in SANDBOX_PROFILES:
        raise HostExitError(f"sandbox profile is not allow-listed: {profile}")
    if profile == "apply":
        allow_network = False
    bubblewrap = shutil.which("bwrap")
    if not bubblewrap:
        raise HostExitError("platform_isolation_unavailable")
    child_env = restricted_environment(env)
    home = _absolute(child_env["HOME"])
    harness = _absolute(harness_root if harness_root is not None else config.RETRO2_HOME)
    writable = _assert_no_symlink(writable_root)
    if not writable.is_dir():
        raise HostExitError("restricted writable root missing")
    # The scratch or worktree may live inside $HOME. It may not be $HOME,
    # contain $HOME, or be a credential directory or the harness root.
    if writable == home or _inside(home, writable) or any(
        writable == _absolute(tree) or _inside(writable, _absolute(tree)) or _inside(_absolute(tree), writable)
        for tree in (home / ".ssh", home / ".claude", home / ".codex", harness)
    ):
        raise HostExitError(f"refusing to mount credential or home path: {writable}")
    work = _assert_no_symlink(cwd)
    command = [bubblewrap, "--die-with-parent", "--new-session", "--unshare-all", "--clearenv"]
    if allow_network:
        command.append("--share-net")
    command += ["--tmpfs", "/"]
    _mount_system(command)
    # Host /tmp is not mounted. The fresh tmpfs hides host auth files that
    # were left under /tmp, and holds the relocated credential directory.
    command += ["--tmpfs", "/tmp"]
    if profile == "diagnosis":
        if project_root is None:
            raise HostExitError("diagnosis project root missing")
        for path in project_mounts(project_root, home, harness):
            _ro_bind(command, path, home, harness)
        for path in harness_reads:
            if not Path(path).exists() and not Path(path).is_symlink():
                continue
            _ro_bind(command, path, home, harness, named_ok=True)
        if diagnostics is not None and (Path(diagnostics).exists() or Path(diagnostics).is_symlink()):
            _ro_bind(command, diagnostics, home, harness)
    elif profile == "apply":
        if subject_root is None:
            raise HostExitError("apply subject root missing")
        subject = _assert_no_symlink(subject_root)
        if subject != writable:
            _ro_bind(command, subject, home, harness)
    executed = [str(item) for item in argv]
    mount_argv = runtime_argv if runtime_argv is not None else argv
    runtime_mounts, rewritten = _runtime_mounts(mount_argv, home)
    if runtime_argv is None:
        executed = rewritten
    for path in runtime_mounts:
        command += ["--ro-bind", str(path), str(path)]
    visible = [writable, work, harness, *_added_directories(argv)]
    if project_root is not None:
        visible.append(_absolute(project_root))
    for credential in credential_paths:
        if profile == "apply":
            break
        credential = _absolute(credential)
        if _inside(credential, home / ".codex") or credential == _absolute(home / ".codex" / "auth.json"):
            raise HostExitError(CODEX_BACKEND_DISABLED)
        if not Path(credential).exists() and not Path(credential).is_symlink():
            continue
        credential = _assert_no_symlink(credential)
        if not credential.is_file():
            continue
        if credential.name == ".credentials.json":
            dest_dir = _private_auth_dir("retro2-claude-auth-", visible, credential_namespace)
            command += ["--dir", str(dest_dir), "--ro-bind", str(credential), str(dest_dir / ".credentials.json")]
            child_env["CLAUDE_CONFIG_DIR"] = str(dest_dir)
            child_env["CLAUDE_CREDENTIAL_FILE"] = str(dest_dir / ".credentials.json")
    if profile != "apply":
        child_env["RETRO2_DIAGNOSIS_SCRATCH"] = str(writable)
    for key, value in child_env.items():
        command += ["--setenv", key, value]
    command += ["--bind", str(writable), str(writable), "--proc", "/proc", "--dev", "/dev",
                "--chdir", str(work), "--", "/bin/sh", "-c",
                'printf "R2_CHILD_STARTED\\n"; exec "$@"', "r2-child", *executed]
    return command


def run_allowlisted(argv, writable_root, *, cwd, timeout=300, env=None,
                    allow_network=False, profile="diagnosis",
                    project_root=None, harness_root=None,
                    harness_reads=(), diagnostics=None, credential_paths=(),
                    subject_root=None, runtime_argv=None, credential_namespace=None):
    """Spawn the command from `build_allowlisted_argv` and record that argv."""
    command = build_allowlisted_argv(
        argv, writable_root, cwd=cwd, env=env, allow_network=allow_network, profile=profile,
        project_root=project_root, harness_root=harness_root, harness_reads=harness_reads,
        diagnostics=diagnostics, credential_paths=credential_paths, subject_root=subject_root,
        runtime_argv=runtime_argv, credential_namespace=credential_namespace,
    )
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout, check=False,
            env=restricted_environment(env), stdin=subprocess.DEVNULL, close_fds=True, pass_fds=(),
        )
    except subprocess.TimeoutExpired as error:
        result = subprocess.CompletedProcess(command, 124, error.stdout or "", error.stderr or "timeout")
    result.child_started = result.stdout.startswith(CHILD_SENTINEL)
    result.stdout = result.stdout[len(CHILD_SENTINEL):] if result.child_started else result.stdout
    result.infrastructure_error = None if result.child_started else "child_start_failed"
    result.sandbox_argv = list(command)
    return result


def run_apply_tests(commands, subject_repo, writable_root, *, timeout=300,
                    allow_network=False):
    """Run apply tests on the empty-root allow-list.

    The subject is read-only. The scratch directory is the only writable bind.
    Network stays disabled. ``allow_network`` is accepted and ignored so older
    callers cannot turn it on.
    """
    del allow_network
    records = []
    subject = _absolute(subject_repo)
    for argv in commands:
        result = run_allowlisted(
            argv, writable_root, cwd=subject, timeout=timeout, profile="apply",
            subject_root=subject, allow_network=False,
        )
        records.append({"argv": argv, "exit_code": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr,
                        "infrastructure_error": getattr(result, "infrastructure_error", None)})
        if result.returncode or getattr(result, "infrastructure_error", None):
            break
    return {"passed": len(records) == len(commands) and all(item["exit_code"] == 0 for item in records),
            "commands": commands, "results": records}

def validation_receipt(*, subject_repo, baseline_commit, preimages, patch_sha256,
                       postimages, test_commands, result, approval_ref=None,
                       test_version_sha256=None, run_id=None, candidate_id=None):
    """Create a host-only immutable-by-hash record for one mechanical result."""
    binding = {
        "schema_version": 1,
        "subject_repo": str(Path(subject_repo).resolve()),
        "baseline_commit": baseline_commit,
        "preimages": dict(sorted(preimages.items())),
        "patch_sha256": patch_sha256,
        "postimages": dict(sorted(postimages.items())),
        "test_commands": test_commands,
        "test_version_sha256": test_version_sha256 or hashlib.sha256(
            json.dumps(test_commands, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "result": result,
        "approval_ref": approval_ref,
        "approval_binding": {"run_id": run_id, "candidate_id": candidate_id,
                              "patch_sha256": patch_sha256} if approval_ref else None,
        "isolation": "bubblewrap: known credential homes masked; candidate network is model-endpoint residual risk",
    }
    binding["binding_sha256"] = hashlib.sha256(
        json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return binding


def _run(argv, *, input_text=None, check=True, env=None, timeout=None):
    result = subprocess.run(
        argv, input=input_text, text=True, capture_output=True, env=env, timeout=timeout
    )
    if check and result.returncode:
        raise HostExitError(result.stderr.strip() or result.stdout.strip() or "host command failed")
    return result


class DetachedProcessHost:
    """The sole detached-process exit used by queue and worker."""

    def __init__(self, spawn=None):
        self._spawn = spawn or os.posix_spawn

    def spawn_detached(self, argv, env=None):
        argv = [str(item) for item in argv]
        if not argv:
            raise HostExitError("detached process argv is empty")
        devnull = os.open(os.devnull, os.O_RDWR)
        try:
            actions = [
                (os.POSIX_SPAWN_DUP2, devnull, descriptor)
                for descriptor in (0, 1, 2)
            ]
            return self._spawn(
                argv[0], argv, os.environ if env is None else env,
                file_actions=actions, setsid=True,
            )
        finally:
            os.close(devnull)


class GitWorktreeHost:
    """The only Git subprocess owner used by candidate generation."""

    def baseline(self, repo):
        return _run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()

    def prepare(self, repo, worktree, baseline):
        _run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), baseline])
        return {"status": "READY", "worktree": str(worktree)}

    def inspect(self, worktree):
        raw = _run(["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"]).stdout
        entries = [item for item in raw.split("\0") if item]
        paths = []
        index = 0
        while index < len(entries):
            paths.append(entries[index][3:])
            if entries[index][:2].strip().startswith(("R", "C")) and index + 1 < len(entries):
                index += 1
                paths.append(entries[index])
            index += 1
        paths = sorted(set(paths))
        tracked = frozenset(
            item for item in _run(["git", "-C", str(worktree), "ls-files", "-z"]).stdout.split("\0") if item
        )
        untracked = [path for path in paths if path not in tracked]
        if untracked:
            _run(["git", "-C", str(worktree), "add", "-N", "--", *untracked])
        patch = _run(["git", "-C", str(worktree), "diff", "--binary", "--no-ext-diff"]).stdout.encode()
        changed_lines = 0
        for line in _run(["git", "-C", str(worktree), "diff", "--numstat"]).stdout.splitlines():
            added, removed, _path = line.split("\t", 2)
            changed_lines += 200 if "-" in (added, removed) else int(added) + int(removed)
        return {
            "status": "INSPECTED",
            "changed_paths": paths,
            "changed_lines": changed_lines,
            "patch_base64": base64.b64encode(patch).decode(),
        }

    def remove(self, repo, worktree):
        result = _run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
            check=False,
        )
        return {"status": "REMOVED" if result.returncode == 0 else "REMOVE_FAILED"}
