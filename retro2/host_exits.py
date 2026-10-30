#!/usr/bin/env python3
"""Trusted host exits for subprocesses; adapters only consume structured results."""
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
import config


class HostExitError(RuntimeError):
    pass


# Explicitly inherit only locale/terminal basics plus the model endpoint knobs.
# PATH: selected executable; HOME: isolated credential lookup; LANG/LC_*/TERM:
# deterministic child text; model credential-file variables only.
CHILD_ENVIRONMENT = ("PATH", "HOME", "LANG", "TERM")
MODEL_ENVIRONMENT = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CREDENTIAL_FILE", "CODEX_CREDENTIAL_FILE")
SECRET_DIRECTORIES = (".ssh", ".aws", ".gnupg", ".config/gh", ".docker", ".kube", ".codex", ".claude")
CHILD_SENTINEL = "R2_CHILD_STARTED\n"


def _mask_directories(home):
    """Directories that must stay hidden from the model after any parent bind."""
    covered = []
    for secret in SECRET_DIRECTORIES:
        _remember_mask(covered, home / secret)
    _remember_mask(covered, config.RETRO2_HOME)
    return covered


def _remember_mask(covered, path):
    path = Path(path).resolve()
    if path == Path("/") or not path.is_dir():
        return
    if any(path == item or path.is_relative_to(item) for item in covered):
        return
    covered.append(path)


def _strictly_inside(path, masks):
    path = Path(path).resolve()
    return any(path != mask and path.is_relative_to(mask) for mask in masks)


def _private_auth_dir(prefix, visible):
    """A /tmp directory that is not inside a path the model is allowed to read."""
    dest_dir = Path("/tmp") / (prefix + secrets.token_hex(8))
    if any(dest_dir.is_relative_to(path) or path.is_relative_to(dest_dir) for path in visible):
        raise HostExitError("credential mount overlaps a model-visible root")
    return dest_dir


def _added_directories(argv):
    found = []
    index = 0
    while index < len(argv):
        if argv[index] == "--add-dir" and index + 1 < len(argv):
            found.append(Path(argv[index + 1]).resolve())
            index += 2
            continue
        index += 1
    return found


def restricted_environment(source=None):
    source = os.environ if source is None else source
    env = {key: source[key] for key in CHILD_ENVIRONMENT if source.get(key)}
    env.setdefault("PATH", os.defpath)
    env.setdefault("HOME", str(Path.home()))
    env.update({key: value for key, value in source.items() if key.startswith("LC_")})
    env.update({key: source[key] for key in MODEL_ENVIRONMENT if source.get(key)})
    return env


def model_backend_environment(source=None):
    """Pass the selected retro backend through detached queue/worker boundaries."""
    source = os.environ if source is None else source
    env = dict(source)
    if "RETRO2_MODEL_BACKEND" in source:
        env["RETRO2_MODEL_BACKEND"] = source["RETRO2_MODEL_BACKEND"]
    return env


def run_restricted(argv, writable_root, *, cwd=None, timeout=300, env=None,
                   allow_network=False, readonly_roots=(), credential_paths=(),
                   git_metadata_roots=()):
    """Run an untrusted child with only its worktree writable.

    bubblewrap makes the host filesystem read-only, then re-binds the isolated
    worktree read/write.  This protects host receipts, the formal subject tree,
    and Git's common directory from candidate and test children.  If bubblewrap
    is absent, callers must stop rather than call an unconfined child.
    """
    bubblewrap = shutil.which("bwrap")
    writable_root = Path(writable_root).resolve()
    if not bubblewrap:
        raise HostExitError("platform_isolation_unavailable")
    if not writable_root.is_dir():
        raise HostExitError("restricted writable root missing")
    roots = [Path(path).resolve() for path in readonly_roots]
    git_roots = [Path(path).resolve() for path in git_metadata_roots]
    if any(not (path.is_dir() or path.is_file()) for path in roots):
        raise HostExitError("restricted read-only root missing")
    git_root = config.RETRO2_HOME / ".git"
    if any(not path.is_relative_to(git_root) or not (path.is_dir() or path.is_file()) for path in git_roots):
        raise HostExitError("restricted git metadata is invalid")
    command = [bubblewrap, "--die-with-parent", "--new-session", "--unshare-all"]
    if allow_network:
        command.append("--share-net")
    # tmpfs must come before bindings: otherwise a test root under /tmp is hidden.
    # Full root remains read-only pending a separately-scoped minimal-root design;
    # known credential homes are hidden before any model child receives network.
    # and a false-green test can succeed without starting its child.
    command += ["--ro-bind", "/", "/", "--tmpfs", "/tmp"]
    child_env = restricted_environment(env)
    home = Path(child_env["HOME"]).resolve()
    credentials = [Path(path).resolve() for path in credential_paths]
    # A parent bind (the project repo, when it is $HOME or contains the harness)
    # covers every earlier mount underneath it. Masks are therefore applied
    # again after those binds. Refusing such repos would break a normal tree
    # nested in $HOME and a checkout whose root is $HOME; remasking keeps both
    # the project and the allowlisted harness files readable.
    masks = _mask_directories(home)
    # A host worktree may point into the configured harness Git directory. Keep that tree
    # tree masked, exposing only a read-only scaffold and the named metadata.
    with tempfile.TemporaryDirectory(prefix="retro2-git-view-") as temporary:
        scaffold = None
        if git_roots:
            scaffold = Path(temporary) / "retro2-home"
            for root in git_roots:
                target = scaffold / root.relative_to(config.RETRO2_HOME)
                (target if root.is_dir() else target.parent).mkdir(parents=True, exist_ok=True)
                if root.is_file():
                    target.touch()
        for root in roots:
            command += ["--ro-bind", str(root), str(root)]
        command += ["--bind", str(writable_root), str(writable_root)]
        for mask in masks:
            command += ["--tmpfs", str(mask)]
        if scaffold is not None:
            command += ["--ro-bind", str(scaffold), str(config.RETRO2_HOME.resolve())]
            for root in git_roots:
                command += ["--ro-bind", str(root), str(root)]
        for root in roots:
            if _strictly_inside(root, masks):
                command += ["--ro-bind", str(root), str(root)]
        if _strictly_inside(writable_root, masks):
            command += ["--bind", str(writable_root), str(writable_root)]
        # Claude reads $CLAUDE_CONFIG_DIR/.credentials.json. Mount that file
        # outside every directory the model can read. --add-dir accepts
        # directories only, so CLAUDE.md and settings.json stay visible by
        # adding the harness root after that root has been masked.
        visible = (
            *roots,
            writable_root,
            Path(cwd or writable_root).resolve(),
            *_added_directories(argv),
        )
        for credential in credentials:
            if not credential.is_file():
                continue
            # Codex reads $CODEX_HOME/auth.json. The directory must exist before
            # the process starts. Leaving the file at ~/.codex/auth.json keeps it
            # on a path the read-only Codex sandbox can still read.
            if credential.is_relative_to(home / ".codex"):
                dest_dir = _private_auth_dir("retro2-codex-auth-", visible)
                command += ["--dir", str(dest_dir), "--ro-bind", str(credential), str(dest_dir / "auth.json")]
                child_env["CODEX_HOME"] = str(dest_dir)
                continue
            if not credential.is_relative_to(config.RETRO2_HOME):
                continue
            dest_dir = _private_auth_dir("retro2-claude-auth-", visible)
            command += ["--dir", str(dest_dir), "--ro-bind", str(credential), str(dest_dir / ".credentials.json")]
            child_env["CLAUDE_CONFIG_DIR"] = str(dest_dir)
            child_env["CLAUDE_CREDENTIAL_FILE"] = str(dest_dir / ".credentials.json")
        if (home / ".netrc").exists():
            command += ["--ro-bind", "/dev/null", str(home / ".netrc")]
        command += ["--proc", "/proc", "--dev", "/dev",
                    "--chdir", str(Path(cwd or writable_root).resolve()), "--", "/bin/sh", "-c",
                    'printf "R2_CHILD_STARTED\\n"; exec "$@"', "r2-child", *[str(item) for item in argv]]
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False,
                                    env=child_env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as error:
            result = subprocess.CompletedProcess(command, 124, error.stdout or "", error.stderr or "timeout")
    result.child_started = result.stdout.startswith(CHILD_SENTINEL)
    result.stdout = result.stdout[len(CHILD_SENTINEL):] if result.child_started else result.stdout
    result.infrastructure_error = None if result.child_started else "child_start_failed"
    return result


SYSTEM_DIRECTORIES = ("/usr", "/lib", "/lib64", "/bin", "/sbin")
SYSTEM_FILES = (
    "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd",
    "/etc/group", "/etc/localtime", "/etc/ssl", "/etc/ca-certificates",
)
HARNESS_READ_NAMES = ("CLAUDE.md", "settings.json", "hooks", "scripts/logs")


def _contains(parent, child):
    parent, child = Path(parent).resolve(), Path(child).resolve()
    return child == parent or child.is_relative_to(parent)


def project_mounts(project_root, home, harness_root):
    """Repo paths that can be mounted without exposing HOME or the harness.

    A normal project nested in $HOME, or a project outside $HOME, is mounted
    whole. If the project is $HOME, contains $HOME, or contains the harness
    or a credential directory, the project root is not mounted. Only subtrees
    that contain none of those directories are mounted. Allowlisted harness
    files are mounted separately.
    """
    home_r = Path(home).resolve()
    harness_r = Path(harness_root).resolve()
    sensitive = []
    for name in SECRET_DIRECTORIES:
        _remember_mask(sensitive, home_r / name)
    _remember_mask(sensitive, harness_r)

    def walk(path):
        path = Path(path).resolve()
        if path == home_r:
            return
        if any(_contains(item, path) for item in sensitive):
            return
        blocked = list(sensitive) + [home_r]
        if any(item.is_relative_to(path) for item in blocked):
            if path.is_dir():
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    yield from walk(child)
            return
        yield path

    return list(walk(Path(project_root).resolve()))


def _ro_bind(command, source):
    source = Path(source)
    if not source.exists():
        return
    # Bind the path programs look up, even when it is a symlink.
    command.extend(["--ro-bind", str(source.resolve()), str(source)])


def run_allowlisted(argv, writable_root, *, cwd, timeout=300, env=None,
                    allow_network=False, project_root, harness_root,
                    harness_reads=(), diagnostics=None, credential_paths=()):
    """Run a diagnosis child on an empty root with an explicit mount list.

    Nothing under $HOME, RETRO2_HOME, or the project is visible unless this
    function mounts it. Credential files are mounted only under a random /tmp
    directory named by CLAUDE_CONFIG_DIR or CODEX_HOME.
    """
    bubblewrap = shutil.which("bwrap")
    writable_root = Path(writable_root).resolve()
    if not bubblewrap:
        raise HostExitError("platform_isolation_unavailable")
    if not writable_root.is_dir():
        raise HostExitError("restricted writable root missing")
    child_env = restricted_environment(env)
    home = Path(child_env["HOME"]).resolve()
    harness_root = Path(harness_root).resolve()
    project_root = Path(project_root).resolve()
    command = [bubblewrap, "--die-with-parent", "--new-session", "--unshare-all"]
    if allow_network:
        command.append("--share-net")
    command += ["--tmpfs", "/"]
    for directory in SYSTEM_DIRECTORIES:
        if Path(directory).is_dir():
            command += ["--ro-bind", directory, directory]
    for filename in SYSTEM_FILES:
        _ro_bind(command, filename)
    command += ["--tmpfs", "/tmp"]
    for path in project_mounts(project_root, home, harness_root):
        command += ["--ro-bind", str(path), str(path)]
    for path in harness_reads:
        path = Path(path)
        if not path.exists():
            raise HostExitError("allowlisted harness path missing")
        command += ["--ro-bind", str(path.resolve()), str(path)]
    if diagnostics is not None and Path(diagnostics).exists():
        command += ["--ro-bind", str(Path(diagnostics).resolve()), str(diagnostics)]
    executable = Path(argv[0]) if argv else None
    if executable is not None and executable.is_file():
        command += ["--ro-bind", str(executable.resolve()), str(executable)]
    visible = [writable_root, project_root, harness_root, *_added_directories(argv)]
    for credential in credential_paths:
        credential = Path(credential).resolve()
        if not credential.is_file():
            continue
        if credential.is_relative_to(home / ".codex"):
            dest_dir = _private_auth_dir("retro2-codex-auth-", visible)
            command += ["--dir", str(dest_dir), "--ro-bind", str(credential), str(dest_dir / "auth.json")]
            child_env["CODEX_HOME"] = str(dest_dir)
            continue
        if credential.is_relative_to(Path(config.RETRO2_HOME).resolve()) or credential.name == ".credentials.json":
            dest_dir = _private_auth_dir("retro2-claude-auth-", visible)
            command += ["--dir", str(dest_dir), "--ro-bind", str(credential), str(dest_dir / ".credentials.json")]
            child_env["CLAUDE_CONFIG_DIR"] = str(dest_dir)
            child_env["CLAUDE_CREDENTIAL_FILE"] = str(dest_dir / ".credentials.json")
    command += ["--bind", str(writable_root), str(writable_root), "--proc", "/proc", "--dev", "/dev",
                "--chdir", str(Path(cwd).resolve()), "--", "/bin/sh", "-c",
                'printf "R2_CHILD_STARTED\\n"; exec "$@"', "r2-child", *[str(item) for item in argv]]
    child_env["RETRO2_DIAGNOSIS_SCRATCH"] = str(writable_root)
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False,
                                env=child_env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        result = subprocess.CompletedProcess(command, 124, error.stdout or "", error.stderr or "timeout")
    result.child_started = result.stdout.startswith(CHILD_SENTINEL)
    result.stdout = result.stdout[len(CHILD_SENTINEL):] if result.child_started else result.stdout
    result.infrastructure_error = None if result.child_started else "child_start_failed"
    return result


def run_apply_tests(commands, subject_repo, writable_root, *, timeout=300,
                    allow_network=False):
    """Run formal apply qualification/canary tests with the subject read-only.

    The disposable directory is the only writable bind.  The explicit readonly
    subject bind remains necessary when it lives beneath the private /tmpfs.
    """
    records = []
    subject = Path(subject_repo).resolve()
    for argv in commands:
        result = run_restricted(argv, writable_root, cwd=subject, timeout=timeout,
                                readonly_roots=(subject,), allow_network=allow_network)
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
