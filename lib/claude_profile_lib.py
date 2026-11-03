"""Shared helpers for claude-remote-launch and claude-profile-move.

Cross-platform (Linux + macOS), Python 3.6+. Kept dependency-free on purpose so
these tools work on a bare system with only python3 available.

This module is the single source of truth for the version number; all commands
report it and are updated together, since they share this file and a
mismatched pair would be a broken install.
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

try:
    from shlex import quote as _shq
except ImportError:  # pragma: no cover
    from pipes import quote as _shq  # type: ignore

#: Single source of truth for the version. All commands report it, and all
#: are updated together since they share this module.
__version__ = "1.3.0"

REPO_SLUG = "sidevesh/claude-code-cli-tools"

#: Files that make up an install, as (repo-relative source, install kind).
#: "bin" -> the executables directory, "lib" -> the shared-module directory.
PAYLOAD = (
    ("bin/claude-remote-launch", "bin"),
    ("bin/claude-profile-move", "bin"),
    ("bin/claude-herdr-resume-hook", "bin"),
    ("lib/claude_profile_lib.py", "lib"),
)

#: Directory name used under <prefix>/lib so the module isn't loose in lib/.
LIB_SUBDIR = "claude-code-cli-tools"

# ---------------------------------------------------------------- profiles

# Profiles are a naming convention, not a fixed list: "default" is ~/.claude and
# any other name X is ~/.claude-X. So a `CLAUDE_CONFIG_DIR=~/.claude-NAME claude`
# alias is automatically the "work" profile with no configuration here.
DEFAULT_PROFILE = "default"
DEFAULT_DIR = os.path.expanduser("~/.claude")
_PREFIX = os.path.expanduser("~/.claude-")


def profile_dir(name):
    if name == DEFAULT_PROFILE:
        return DEFAULT_DIR
    return _PREFIX + name


def discover_profiles():
    """Ordered {name: dir} for profiles that actually exist on disk."""
    out = {}
    if os.path.isdir(DEFAULT_DIR):
        out[DEFAULT_PROFILE] = DEFAULT_DIR
    parent = os.path.dirname(_PREFIX)
    base = os.path.basename(_PREFIX)
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        names = []
    for entry in names:
        if not entry.startswith(base):
            continue
        full = os.path.join(parent, entry)
        if os.path.isdir(full):
            out[entry[len(base):]] = full
    return out


def config_dir(profile=None):
    """Config dir for a named profile, or from CLAUDE_CONFIG_DIR, or the default."""
    if profile:
        d = profile_dir(profile)
        if not os.path.isdir(d):
            known = ", ".join(discover_profiles()) or "none found"
            die("unknown profile %r -> %s does not exist (known: %s)"
                % (profile, d, known))
        return d
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.expanduser(env)
    return DEFAULT_DIR


def config_json_path(cfg_dir):
    """Claude Code stores config at ~/.claude.json for the default dir, but
    inside the config dir when CLAUDE_CONFIG_DIR is set."""
    if os.path.realpath(cfg_dir) == os.path.realpath(DEFAULT_DIR):
        return os.path.expanduser("~/.claude.json")
    return os.path.join(cfg_dir, ".claude.json")


def projects_dir(cfg_dir):
    return os.path.join(cfg_dir, "projects")


def _transcript_cwd(path):
    """Return the cwd recorded near the start of a Claude transcript."""
    try:
        with open(path, "rb") as handle:
            for _ in range(200):
                line = handle.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return resolve_dir(cwd)
    except (OSError, IOError):
        pass
    return None


def _profile_name_for_dir(cfg_dir):
    wanted = os.path.realpath(cfg_dir)
    for name, discovered in discover_profiles().items():
        if os.path.realpath(discovered) == wanted:
            return name
    return cfg_dir


def _project_is_registered(cfg_dir, project_dir):
    """Whether *project_dir* has a projects entry in this profile's JSON."""
    if not project_dir:
        return False
    wanted = os.path.realpath(resolve_dir(project_dir))
    data = load_json(config_json_path(cfg_dir))
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        return False
    for path in projects:
        if isinstance(path, str) and os.path.realpath(resolve_dir(path)) == wanted:
            return True
    return False


def prompt_session_profile(session_id, profile_dirs, input_stream=None,
                           output_stream=None):
    """Ask which profile owns an ambiguous copied Claude session."""
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stderr
    if not getattr(input_stream, "isatty", lambda: False)():
        die("Claude session %s exists in multiple profiles (%s), and no interactive "
            "terminal is available to choose one. Remove the duplicate project/session "
            "from the profile that should not own it, then restart Herdr."
            % (session_id, ", ".join(_profile_name_for_dir(p)
                                     for p in profile_dirs)))

    output_stream.write(
        "\nClaude session %s exists in multiple profiles:\n" % session_id)
    for index, cfg_dir in enumerate(profile_dirs, 1):
        output_stream.write("  %d. %s  (%s)\n" %
                            (index, _profile_name_for_dir(cfg_dir), cfg_dir))
    output_stream.write(
        "\nThis usually means the project or transcript was copied between profiles.\n"
        "Choose the profile to use now. Remove the duplicate from the other profile\n"
        "afterward so this question does not appear on every Herdr start.\n")
    output_stream.flush()

    while True:
        output_stream.write("Profile [1-%d]: " % len(profile_dirs))
        output_stream.flush()
        answer = input_stream.readline()
        if not answer:
            die("profile selection was cancelled")
        try:
            choice = int(answer.strip())
        except ValueError:
            choice = 0
        if 1 <= choice <= len(profile_dirs):
            return profile_dirs[choice - 1]
        output_stream.write("Enter a number from 1 to %d.\n" % len(profile_dirs))


def session_profile_dir(session_id, cwd=None):
    """Find the Claude config directory that owns *session_id*.

    Claude stores a main session as ``<config>/projects/**/<id>.jsonl``.  Herdr
    restores only the id, so locating that transcript recovers the
    CLAUDE_CONFIG_DIR that the original process used.

    A copied profile can contain the same id more than once. Prefer an exact
    transcript cwd match, then the sole profile whose .claude.json registers
    that project. Ask in the pane when a fully copied session is still
    ambiguous rather than silently resuming it with the wrong account.

    Returns ``None`` when no discovered profile contains the session.
    """
    if not session_id or os.path.basename(session_id) != session_id:
        return None
    filename = session_id + ".jsonl"
    candidates = []
    for _name, cfg_dir in discover_profiles().items():
        root = projects_dir(cfg_dir)
        if not os.path.isdir(root):
            continue
        for project_root, _dirs, files in os.walk(root):
            if filename in files:
                path = os.path.join(project_root, filename)
                candidates.append((cfg_dir, path, _transcript_cwd(path)))

    if not candidates:
        return None

    wanted_cwd = resolve_dir(cwd) if cwd else None
    if wanted_cwd:
        cwd_matches = [item for item in candidates
                       if item[2] and os.path.realpath(item[2]) == os.path.realpath(wanted_cwd)]
        if cwd_matches:
            candidates = cwd_matches

    unique_dirs = []
    for cfg_dir, _path, _cwd in candidates:
        if not any(os.path.realpath(cfg_dir) == os.path.realpath(existing)
                   for existing in unique_dirs):
            unique_dirs.append(cfg_dir)
    if len(unique_dirs) == 1:
        return unique_dirs[0]

    project_dir = wanted_cwd
    if not project_dir:
        recorded_cwds = set(item[2] for item in candidates if item[2])
        if len(recorded_cwds) == 1:
            project_dir = next(iter(recorded_cwds))
    registered = [cfg_dir for cfg_dir in unique_dirs
                  if _project_is_registered(cfg_dir, project_dir)]
    if len(registered) == 1:
        return registered[0]

    return prompt_session_profile(session_id, unique_dirs)


# ---------------------------------------------------------------- json io

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        die("%s is not valid JSON (%s) - refusing to touch it" % (path, e))


def save_json_atomic(path, data, mode=0o600):
    """Atomic write. Claude Code rewrites this file constantly, so a partial or
    non-atomic write can be clobbered by (or clobber) a live session."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def install_herdr_resume_hooks():
    """Register the reboot-safe Herdr resume hook in every Claude profile.

    The hook is separate from Herdr's managed hook file, which an integration
    update may replace. Re-running this function removes stale copies of our
    own hook entry before adding the current absolute path.
    """
    bin_dir, _lib_dir = install_root()
    hook_path = os.path.join(bin_dir, "claude-herdr-resume-hook")
    if not os.path.isfile(hook_path):
        die("Herdr resume hook not found at %s; reinstall" % hook_path)
    command = _shq(hook_path)
    installed = []
    marker = "claude-herdr-resume-hook"

    for _name, cfg_dir in discover_profiles().items():
        settings_path = os.path.join(cfg_dir, "settings.json")
        data = load_json(settings_path)
        hooks = data.setdefault("hooks", {})
        entries = hooks.setdefault("SessionStart", [])
        if not isinstance(entries, list):
            die("%s hooks.SessionStart is not a list" % settings_path)

        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict):
                cleaned.append(entry)
                continue
            commands = entry.get("hooks")
            if not isinstance(commands, list):
                cleaned.append(entry)
                continue
            kept = []
            for hook in commands:
                hook_command = hook.get("command", "") if isinstance(hook, dict) else ""
                if marker not in hook_command:
                    kept.append(hook)
            if kept:
                copy = dict(entry)
                copy["hooks"] = kept
                cleaned.append(copy)

        cleaned.append({
            "matcher": "^(startup|resume|clear|compact|fork)$",
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": 10,
            }],
        })
        hooks["SessionStart"] = cleaned
        save_json_atomic(settings_path, data)
        installed.append(settings_path)
    return installed


# ---------------------------------------------------------------- slugs

def path_to_slug(abs_path):
    """Claude Code's project-dir naming: non-alphanumeric runs -> '-'.
    e.g. /home/u/Projects/foo -> -home-u-Projects-foo"""
    return re.sub(r"[^A-Za-z0-9]+", "-", abs_path)


def resolve_dir(p):
    return os.path.abspath(os.path.expanduser(p))


# ---------------------------------------------------------------- activity

_TS_RE = re.compile(rb'"timestamp"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2}T[^"]+)"')


def _parse_iso(s):
    s = s.replace("Z", "+00:00")
    # Python <3.11 can't parse more than 6 fractional digits; trim defensively.
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def last_active(project_path, tail_bytes=262144):
    """Newest activity timestamp for a project dir.

    Prefers timestamps embedded in the .jsonl transcripts over filesystem mtime:
    mtime is rewritten by copies, backups and cleanup passes, so it routinely
    overstates real activity. Falls back to mtime only when no timestamp is found.

    Returns (datetime|None, source_str).
    """
    best = None
    if os.path.isdir(project_path):
        for name in os.listdir(project_path):
            if not name.endswith(".jsonl"):
                continue
            fp = os.path.join(project_path, name)
            try:
                size = os.path.getsize(fp)
                with open(fp, "rb") as f:
                    # Transcripts are append-only, so the tail holds the newest
                    # entries. Reading the whole multi-MB file would be wasteful.
                    if size > tail_bytes:
                        f.seek(size - tail_bytes)
                        f.readline()  # discard partial line
                    chunk = f.read()
                for m in _TS_RE.finditer(chunk):
                    dt = _parse_iso(m.group(1).decode("utf-8", "replace"))
                    if dt and (best is None or dt > best):
                        best = dt
            except (OSError, IOError):
                continue
    if best is not None:
        return best, "transcript"

    newest = None
    for root, _dirs, files in os.walk(project_path):
        for fn in files:
            try:
                mt = os.path.getmtime(os.path.join(root, fn))
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    if newest is None:
        return None, "empty"
    return datetime.fromtimestamp(newest, timezone.utc), "mtime"


def fmt_when(dt):
    if dt is None:
        return "never"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def project_real_path(project_path, slug):
    """Recover the true working directory for a project.

    The slug is lossy ('-' replaces any non-alphanumeric run, so a dot or a real
    hyphen in a path is unrecoverable). The transcripts record the actual `cwd`,
    so read it from there and only guess as a last resort.
    """
    if os.path.isdir(project_path):
        for name in sorted(os.listdir(project_path)):
            if not name.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(project_path, name), "rb") as f:
                    for _ in range(200):
                        line = f.readline()
                        if not line:
                            break
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        cwd = obj.get("cwd")
                        if cwd:
                            return cwd, True
            except (OSError, IOError):
                continue
    return slug.replace("-", "/"), False


def list_projects(cfg_dir):
    """-> {slug: {path, last_active, source, real_path, exact}}"""
    pdir = projects_dir(cfg_dir)
    out = {}
    if not os.path.isdir(pdir):
        return out
    for slug in sorted(os.listdir(pdir)):
        full = os.path.join(pdir, slug)
        if not os.path.isdir(full):
            continue
        dt, src = last_active(full)
        real, exact = project_real_path(full, slug)
        out[slug] = {
            "path": full,
            "last_active": dt,
            "source": src,
            "real_path": real,
            "exact": exact,
        }
    return out


def has_conversation(cfg_dir, abs_path):
    """Whether this profile has a transcript for exactly ``abs_path``.

    Claude's project-directory slug is lossy, so the directory existing is not
    enough: inspect each transcript's recorded cwd to avoid resuming a different
    project whose path happens to collapse to the same slug.
    """
    wanted = os.path.realpath(abs_path)
    project_path = os.path.join(projects_dir(cfg_dir), path_to_slug(wanted))
    if not os.path.isdir(project_path):
        return False
    for name in os.listdir(project_path):
        if not name.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(project_path, name), "rb") as f:
                for _ in range(200):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    cwd = obj.get("cwd")
                    if cwd and os.path.realpath(cwd) == wanted:
                        return True
        except (OSError, IOError):
            continue
    return False


# ---------------------------------------------------------------- trust

def trust_dir(cfg_json, abs_dir):
    """Set hasTrustDialogAccepted for abs_dir. Returns True if newly trusted.

    This is the supported way to skip the workspace trust prompt. Deliberately
    NOT --dangerously-skip-permissions, which would also disable every
    tool-permission check for the session.
    """
    data = load_json(cfg_json)
    entry = data.setdefault("projects", {}).setdefault(abs_dir, {})
    already = entry.get("hasTrustDialogAccepted") is True
    entry.setdefault("allowedTools", [])
    entry.setdefault("mcpServers", {})
    entry.setdefault("enabledMcpjsonServers", [])
    entry.setdefault("disabledMcpjsonServers", [])
    entry["hasTrustDialogAccepted"] = True
    entry["projectOnboardingSeenCount"] = max(
        entry.get("projectOnboardingSeenCount", 0) or 0, 1
    )
    save_json_atomic(cfg_json, data)
    return not already


# ---------------------------------------------------------------- misc

def project_count(cfg_dir):
    """Cheap count - avoids parsing every transcript just to show a number."""
    p = projects_dir(cfg_dir)
    try:
        return sum(1 for e in os.listdir(p) if os.path.isdir(os.path.join(p, e)))
    except OSError:
        return 0


def print_profiles(show_counts=True):
    """Shared --profiles output for both tools."""
    found = discover_profiles()
    if not found:
        print("No profiles found. Expected ~/.claude or ~/.claude-<name>.")
        return 0
    width = max(max(len(n) for n in found), len("PROFILE"))
    if show_counts:
        print("%-*s  %-8s  %s" % (width, "PROFILE", "PROJECTS", "CONFIG DIR"))
        for name, d in found.items():
            print("%-*s  %-8d  %s" % (width, name, project_count(d), d))
    else:
        print("%-*s  %s" % (width, "PROFILE", "CONFIG DIR"))
        for name, d in found.items():
            print("%-*s  %s" % (width, name, d))
    return 0


def print_projects(profile=None):
    """Shared --list output: projects by last-active.

    With `profile`, only that one; otherwise every profile found.
    """
    found = {profile: config_dir(profile)} if profile else discover_profiles()
    if not found:
        print("No profiles found. Expected ~/.claude or ~/.claude-<name>.")
        return 0
    for prof, cfg in found.items():
        projs = list_projects(cfg)
        print("\n=== %s  (%s) ===" % (prof, cfg))
        if not projs:
            print("  (no projects)")
            continue
        rows = sorted(projs.items(),
                      key=lambda kv: (kv[1]["last_active"] is None,
                                      kv[1]["last_active"] or 0),
                      reverse=True)
        for _slug, info in rows:
            mark = "" if info["source"] == "transcript" else "  (%s)" % info["source"]
            print("  %-16s  %s%s" % (fmt_when(info["last_active"]),
                                     info["real_path"], mark))
    print()
    return 0


def print_shell_init(invoked_as="claude-remote-launch"):
    """Emit alias definitions for eval, in the pyenv/direnv/nvm style.

    `env VAR=... cmd` rather than a `VAR=... cmd` prefix, because the prefix
    form is not valid in fish; `env` works in bash, zsh and fish alike.
    """
    print("# Generated by: %s --shell-init" % invoked_as)
    print('# Add to ~/.bashrc or ~/.zshrc:  eval "$(%s --shell-init)"' % invoked_as)
    emitted = 0
    for name, d in discover_profiles().items():
        if name == DEFAULT_PROFILE:
            continue  # plain `claude` already is the default profile
        if not re.match(r"^[A-Za-z0-9_-]+$", name):
            continue  # can't be a shell alias name
        for cmd in ("claude", "claude-remote-launch"):
            alias = "%s-%s" % (cmd, name)
            value = "env CLAUDE_CONFIG_DIR=%s %s" % (_shq(d), cmd)
            print("alias %s=%s" % (alias, _shq(value)))
            emitted += 1
    if not emitted:
        print("# (no non-default profiles found)")
    return 0


# ---------------------------------------------------------------- updating

def _parse_version(s):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", s.strip())
    return tuple(int(g) for g in m.groups()) if m else None


def _fetch(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "claude-code-cli-tools/%s" % __version__,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_version():
    """(version_string, git_ref) of the newest release tag, or (None, None).

    Tags are the source of truth so an install can be pinned and reproduced;
    falls back to the default branch when the repo has no tags yet.
    """
    try:
        tags = json.loads(_fetch(
            "https://api.github.com/repos/%s/tags?per_page=100" % REPO_SLUG
        ).decode("utf-8"))
    except Exception as e:
        warn("could not reach GitHub: %s" % e)
        return None, None
    best, best_name = None, None
    for t in tags:
        v = _parse_version(t.get("name", ""))
        if v and (best is None or v > best):
            best, best_name = v, t["name"]
    if best is None:
        return None, "main"
    return ".".join(str(n) for n in best), best_name


def install_root():
    """Where this install lives: (bin_dir, lib_dir), from THIS module's path.

    Derived from the running files rather than assumed, so a --prefix or BINDIR
    install updates itself in place instead of writing to ~/.local.
    """
    lib_dir = os.path.dirname(os.path.realpath(__file__))
    prefix = os.path.dirname(lib_dir)
    if os.path.basename(lib_dir) == LIB_SUBDIR:
        prefix = os.path.dirname(prefix)          # <prefix>/lib/<subdir>
    bin_dir = os.path.join(prefix, "bin")
    return bin_dir, lib_dir


def is_git_checkout():
    """True when running from a clone, where updating should be `git pull`."""
    lib_dir = os.path.dirname(os.path.realpath(__file__))
    return os.path.isdir(os.path.join(os.path.dirname(lib_dir), ".git"))


#: version-that-introduces-the-change -> callable, e.g. {(1, 1, 0): _migrate_110}.
#: Keep them idempotent: an interrupted update may re-run one.
def install_extras():
    """Install Herdr entry points and register its durable resume hook."""
    bin_dir, lib_dir = install_root()
    launcher = os.path.join(bin_dir, "claude-remote-launch")
    entries = [
        (os.path.join(bin_dir, "herdr-launch"), launcher),
        (os.path.join(lib_dir, "herdr-bin", "claude"), launcher),
    ]
    for link, target in entries:
        os.makedirs(os.path.dirname(link), exist_ok=True)
        tmp = link + ".tmp.%s" % os.getpid()
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(target, tmp)
            os.replace(tmp, link)
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
    install_herdr_resume_hooks()


MIGRATIONS = {}


def apply_migrations(from_v, to_v):
    """Run on-disk migrations for an update from `from_v` to `to_v`.

    Called on the NEWLY installed module, not the running one - see
    _run_new_migrations. A migration therefore ships in the release that needs
    it, rather than having to be predicted by the release before it.
    """
    fv = _parse_version(from_v) or (0, 0, 0)
    tv = _parse_version(to_v) or (0, 0, 0)
    for ver in sorted(MIGRATIONS):
        if fv < ver <= tv:
            print("  running migration for %s" % ".".join(map(str, ver)))
            MIGRATIONS[ver]()


def _run_new_migrations(lib_target, from_v, to_v):
    """Load the just-installed module and run ITS migrations.

    The running process still holds the old code, so calling our own
    apply_migrations here would execute the outgoing version's idea of what
    needs migrating - which cannot know about changes introduced by the release
    being installed. Loading the new file fixes the ordering.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_cclt_new", lib_target)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.apply_migrations(from_v, to_v)
        mod.install_extras()
    except Exception as e:
        warn("could not run migrations for the new version: %s" % e)
        warn("the update itself succeeded; re-run --update if problems appear")


def do_update(check_only=False, assume_yes=False):
    """Update all installed files together to the latest released version."""
    print("installed version: %s" % __version__)

    if is_git_checkout() and not check_only:
        die("running from a git checkout - update with `git pull` instead")

    remote, ref = latest_version()
    if remote is None and ref is None:
        return 1
    if remote is None:
        print("no release tags published yet; nothing to compare against")
        return 0

    print("latest released : %s" % remote)
    cur, new = _parse_version(__version__), _parse_version(remote)
    if cur is not None and new is not None and new <= cur:
        print("already up to date.")
        return 0
    if check_only:
        print("\nan update is available. run without --check to install it.")
        return 0

    bin_dir, lib_dir = install_root()
    print("\nwill update:")
    for rel, kind in PAYLOAD:
        target = os.path.join(bin_dir if kind == "bin" else lib_dir,
                              os.path.basename(rel))
        print("  %s" % target)

    for d in (bin_dir, lib_dir):
        if not os.access(d, os.W_OK):
            die("%s is not writable - re-run with sufficient privileges" % d)

    if not assume_yes and not os.environ.get("CLAUDE_CLI_TOOLS_ASSUME_YES"):
        try:
            resp = input("\nproceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if resp not in ("y", "yes"):
            print("aborted.")
            return 1

    base = "https://raw.githubusercontent.com/%s/%s/" % (REPO_SLUG, ref)
    staged = []
    try:
        for rel, kind in PAYLOAD:
            data = _fetch(base + rel)
            # Guard against a 404 page or a truncated body being written over a
            # working install.
            if len(data) < 200:
                die("suspiciously small download for %s - aborting" % rel)
            if kind == "bin" and not data.startswith(b"#!"):
                die("%s does not look like a script - aborting" % rel)
            if rel.endswith("claude_profile_lib.py") and b"__version__" not in data:
                die("%s does not look like the shared module - aborting" % rel)
            d = bin_dir if kind == "bin" else lib_dir
            target = os.path.join(d, os.path.basename(rel))
            fd, tmp = tempfile.mkstemp(dir=d)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o755 if kind == "bin" else 0o644)
            staged.append((tmp, target))
    except BaseException:
        for tmp, _ in staged:
            if os.path.exists(tmp):
                os.unlink(tmp)
        raise

    # Everything downloaded cleanly; swap them in. os.replace is atomic, and a
    # running script keeps its old inode, so replacing it mid-run is safe.
    lib_target = None
    for tmp, target in staged:
        os.replace(tmp, target)
        print("  updated %s" % target)
        if target.endswith("claude_profile_lib.py"):
            lib_target = target

    if lib_target:
        _run_new_migrations(lib_target, __version__, remote)
    print("\nupdated %s -> %s" % (__version__, remote))
    return 0


def die(msg, code=1):
    sys.stderr.write("error: %s\n" % msg)
    raise SystemExit(code)


def warn(msg):
    sys.stderr.write("warning: %s\n" % msg)
