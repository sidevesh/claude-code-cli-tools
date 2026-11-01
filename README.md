# claude-code-cli-tools

Three small CLI tools for [Claude Code](https://claude.com/claude-code) and Herdr:

- **`claude-remote-launch`** — open a new terminal window running Claude Code in a directory, skipping the workspace trust prompt, with the session auto-named so it's identifiable in the Remote Control session list.
- **`claude-profile-move`** — list, compare and move projects between Claude Code config profiles.
- **`herdr-launch`** — launch a mixed-agent Herdr session whose Claude panes independently retain their original profile, standard names, and Remote Control across native session restore.

All three are dependency-free Python 3, and work on Linux and macOS.

> Tested against **Claude Code 2.1.228**. These tools read and write Claude Code's own config layout (`~/.claude.json`, `<config>/projects/`) and depend on flags like `claude -n` and `claude --remote-control`, so a future release could move things underneath them. If something misbehaves after an upgrade, check the layout before assuming a bug here.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/sidevesh/claude-code-cli-tools/main/install.sh | sh
```

Always installs the latest released tag. Run it again to upgrade — it detects an existing install and asks first.

Three payload files and two Herdr entry-point symlinks land on your system, and nothing is written to your working directory (downloads are staged in a temp dir that is removed on exit):

```
~/.local/bin/claude-remote-launch
~/.local/bin/claude-profile-move
~/.local/bin/herdr-launch -> ~/.local/bin/claude-remote-launch
~/.local/lib/claude-code-cli-tools/claude_profile_lib.py
~/.local/lib/claude-code-cli-tools/herdr-bin/claude -> ~/.local/bin/claude-remote-launch
```

The shared module is deliberately **not** in `bin/` — that directory goes on your `PATH` and should contain nothing but commands. Each script locates the module relative to its own resolved path, so both the installed layout and a git checkout work.

| Environment variable | |
|---|---|
| `PREFIX` | install root (default `~/.local`) |
| `BINDIR` / `LIBDIR` | override either directory individually |
| `CLAUDE_CLI_TOOLS_ASSUME_YES=1` | upgrade without prompting (for scripted installs) |
| `CLAUDE_CLI_TOOLS_REF` | install a specific tag or branch instead of the latest |

When piping, put variables on the **`sh` side** — on the left they apply to `curl`, not to the script:

```sh
curl -fsSL https://raw.githubusercontent.com/sidevesh/claude-code-cli-tools/main/install.sh | PREFIX=/opt/tools sh
```

Requires `python3` and either `curl` or `wget`. Note macOS has not bundled Python since 12.3 — `/usr/bin/python3` is a stub that prompts to install the Xcode Command Line Tools. Any Mac with Xcode CLT or Homebrew already has it.

### Updating

```sh
claude-remote-launch --update --check     # is there a newer release?
claude-remote-launch --update             # install it (prompts first)
claude-remote-launch --update -y          # no prompt
```

All three commands accept `--update`, and any one updates **all three payload files together** — they share a module, so a half-updated install would be a broken one. Updates are staged fully before anything is replaced, and each file is swapped in atomically, so an interrupted download leaves your existing install untouched. Version numbers are single-sourced from the shared module, and future releases can register migrations there if an update ever needs to change on-disk state.

Running from a git checkout, `--update` refuses and tells you to `git pull` instead.

## `claude-remote-launch`

```sh
claude-remote-launch                          # current directory
claude-remote-launch --profiles               # list config profiles
claude-remote-launch --list                   # list known projects
claude-remote-launch --shell-init             # per-profile aliases, for eval
claude-remote-launch ~/Projects/foo
claude-remote-launch ~/Projects/foo --model opus
claude-remote-launch --profile NAME ~/Projects/foo
claude-remote-launch --here ~/Projects/foo     # use the current terminal/pane
claude-remote-launch --here --continue-if-available ~/Projects/foo
claude-remote-launch --dry-run ~/Projects/foo   # print the plan, change nothing
```

| Option | |
|---|---|
| `--profiles` | list config profiles and exit |
| `--list` | list known projects by last-active, and exit |
| `--shell-init` | print per-profile alias definitions for `eval`, then exit |
| `--profile NAME` | which config profile to use (else `$CLAUDE_CONFIG_DIR`, else default) |
| `--terminal NAME` | force a terminal emulator (else `$CLAUDE_TERMINAL`, else auto-detect) |
| `--here` | run Claude in the current terminal instead of opening a new terminal window |
| `--continue-if-available` | continue the latest conversation for this project and profile when one exists; otherwise start a new session |
| `-n`, `--name NAME` | override the session name |
| `--no-name` | don't name the session |
| `--dry-run` | print what would run, change nothing |
| `--update [--check]` | update all tools; `--check` only reports |
| `-v`, `--version` | print version and exit |

Unrecognised arguments are passed straight through to `claude`.

`--here` is useful in tmux panes, SSH sessions, IDE terminals and scripts that
already control terminal placement. It performs the same profile selection,
workspace trust, session naming and Remote Control setup as the normal launcher,
then replaces itself with Claude in the current terminal.

`--continue-if-available` differs from passing `--continue` directly: it checks
for an existing transcript belonging to the exact directory in the selected
profile first. With no matching conversation it starts a new named session
instead of exiting with `No conversation found to continue`.

### Skipping the trust prompt

Claude Code has **no interactive `--trust` flag** — the "Do you trust the files in this folder?" dialog is only auto-skipped in non-interactive mode (`-p`, or when stdout isn't a TTY), which is no use for an interactive window. The supported route is the per-project key in the config:

```
<config>/.claude.json → projects["/abs/path"].hasTrustDialogAccepted = true
```

That is all this tool sets. It deliberately **does not** use `--dangerously-skip-permissions`, which would suppress the dialog but also disable every tool-permission check for the session. Trust and permissions are separate concerns and only the first one is being pre-answered here.

**Know what you are pre-answering.** Workspace trust gates the things a project directory can make run *without* going through the tool-permission prompt — hooks, `statusLine`, `subagentStatusLine`, `fileSuggestion` commands, and plugin monitors, all configurable from a project's own `.claude/settings.json`. Untrusted, those are skipped entirely.

So pre-trusting a directory is only as safe as the directory. Point this at your own projects. Do **not** point it at a repo you have just cloned and not read, because a hostile `.claude/settings.json` in that repo can define a hook that executes on your machine, and the trust prompt is precisely the checkpoint that would have stopped it.

The config is rewritten atomically (`mkstemp` + `os.replace`, mode `0600`), because Claude Code rewrites this file continuously and a non-atomic write can be clobbered by — or clobber — a live session.

### Session naming

Sessions are auto-named `<dirname> (<user>@<host>)` via `claude -n`, which sets the name in the prompt box, the `/resume` picker, the terminal title, and the Remote Control session list. Without it, sessions fall back to auto-generated names like `Websites [2f4a9f]`, which are hard to tell apart when several machines are connected.

Your own `-n`/`--name` (ours, or passed straight through after the directory) always wins over the auto-name.

Every session is also launched with `--remote-control`. If nothing named the session (no `-n`/`--name`, no `--no-name`), the same generated `<dirname> (<user>@<host>)` is passed as `--remote-control`'s argument too. Otherwise `--remote-control` is passed bare and Claude Code's own defaults take over — your own name if you gave one, or its hostname-prefixed auto name.

### Terminal support

Auto-detected, first match wins: `kgx`, `ptyxis`, `gnome-terminal`, `tilix`, `konsole`, `alacritty`, `kitty`, `wezterm`, `foot`, `xfce4-terminal`, `xterm`. On macOS it drives **iTerm2** if installed, otherwise **Terminal.app**, via `osascript`.

Force one with `--terminal` or `$CLAUDE_TERMINAL`.

## `herdr-launch`

Herdr normally restores Claude panes with `claude --resume <session-id>`, which
does not retain a non-default `CLAUDE_CONFIG_DIR`, session naming, or Remote
Control arguments. `herdr-launch` preserves all of them:

```sh
herdr-launch --session projects --claude-fallback-profile NAME
```

This starts or attaches to the Herdr named session `projects`. The session may
contain Claude, OpenCode, Codex, ordinary shells, or any other process; only
Claude commands are redirected. The launcher prepends a private `claude` shim
to that Herdr server's `PATH` and then replaces itself with
`herdr --session projects`. When Herdr starts or restores Claude in a pane, the
shim routes its native command through `claude-remote-launch --here`. OpenCode,
Codex, normal terminals, normal `claude` commands, and Herdr sessions launched
another way are unaffected.

The selected Claude profile is only the fallback for a new session or a resume
whose transcript cannot yet be found. For normal restore, the shim searches all
discovered profiles for `<config>/projects/**/<session-id>.jsonl` and uses the
owning config directory. Different panes in the same Herdr session can therefore
resume different Claude accounts. If copied profile data makes a session id
ambiguous, the pane cwd and the project registration in each profile's
`.claude.json` disambiguate it when possible. If a fully copied session is still
ambiguous, that Claude pane pauses with an interactive profile chooser. The
chooser reminds you to remove the duplicate afterward; if you leave both copies
in place, it asks again on every Herdr start rather than silently choosing the
wrong account.

Start a new pane under the account you want once, for example with a generated
`claude-<name>` alias or `claude-remote-launch --profile NAME --here`. After
Claude creates its transcript, future Herdr restores infer that profile automatically.
Install Herdr's Claude integration in every profile so each account reports its
session identity. The first start after a full server stop or machine reboot
must use `herdr-launch` so the new server inherits its private shim environment;
ordinary Herdr clients may attach afterward while that server remains alive.

| Option | |
|---|---|
| `--session NAME` | Herdr named session to launch or attach (required) |
| `--claude-fallback-profile NAME` | Claude profile for new/unidentified sessions (else `$CLAUDE_CONFIG_DIR`, else the default profile) |
| `--dry-run` | print the Herdr, fallback Claude profile, binary and shim that would be used |
| `--update [--check]` | update all tools; `--check` only reports |
| `-v`, `--version` | print version and exit |

## `claude-profile-move`

```sh
claude-profile-move --profiles                # list config profiles
claude-profile-move --list                    # all projects, by last-active
claude-profile-move --dupes                   # projects present in >1 profile
claude-profile-move ~/Projects/foo --to NAME
claude-profile-move ~/Projects/foo --to NAME --copy
claude-profile-move ~/Projects/foo --to NAME --dry-run
```

| Option | |
|---|---|
| `--profiles` | list config profiles and exit |
| `--list` | list all projects by last-active, and exit |
| `--dupes` | list projects present in more than one profile, and exit |
| `--to NAME` | target profile (required to move) |
| `--from NAME` | source profile (default: whichever profile has it) |
| `--copy` | copy instead of move |
| `--prefer-newer` | on collision, keep the more recently active copy |
| `--force` | on collision, overwrite the target |
| `--dry-run` | print the plan, change nothing |
| `--update [--check]` | update all tools; `--check` only reports |
| `-v`, `--version` | print version and exit |

A project is **two** things, and both are moved together:

1. `<config>/projects/<slug>/` — transcripts and memory
2. `<config>/.claude.json → projects["/abs/path"]` — trust and per-project settings

Moving only the directory leaves the project half-registered, which is the main reason this tool exists.

On a collision the move is refused unless you pass `--prefer-newer` or `--force`, and the displaced copy is always renamed to `.bak` rather than deleted.

## Profiles

A profile is a naming convention, not a registry:

| Profile | Config dir |
|---|---|
| `default` | `~/.claude` |
| `<name>` | `~/.claude-<name>` |

So if you run a second profile with an alias like

```sh
alias claude-NAME='CLAUDE_CONFIG_DIR=~/.claude-NAME claude'
```

it is automatically the corresponding named profile — nothing to configure.
Profiles are discovered by scanning for `~/.claude-*` directories. Either tool
will list what it found:

```
$ claude-profile-move --profiles
PROFILE  PROJECTS  CONFIG DIR
default  6         /home/you/.claude
NAME     8         /home/you/.claude-NAME
```

### Shell integration

Add one line to your `~/.bashrc` or `~/.zshrc`:

```sh
eval "$(claude-remote-launch --shell-init)"
```

That defines `claude-<name>` and `claude-remote-launch-<name>` for every profile found:

```
$ claude-remote-launch --shell-init
alias claude-NAME='env CLAUDE_CONFIG_DIR=/home/you/.claude-NAME claude'
alias claude-remote-launch-NAME='env CLAUDE_CONFIG_DIR=/home/you/.claude-NAME claude-remote-launch'
```

Create a profile directory and the aliases appear in your next shell — no shell code to maintain. The generated aliases use `env VAR=... cmd` rather than a `VAR=... cmd` prefix, so they work in fish as well as bash and zsh.

Note that Claude Code stores the default profile's config at `~/.claude.json` (beside the directory, not inside it), but a non-default profile's at `$CLAUDE_CONFIG_DIR/.claude.json`. Both tools handle that asymmetry.

## How "last active" is determined

From the timestamps embedded in the `*.jsonl` transcripts, **not** file mtime.

mtime is rewritten by copies, backups and cleanup passes, and routinely overstates activity — during development one transcript showed an mtime three days newer than its real last message. The tools read the tail of each transcript (they run to several MB), take the newest embedded timestamp, and fall back to mtime only when no timestamp is found, marking such rows `(mtime)`.

Transcripts also record the session's `cwd`, which is the only reliable way to recover a project's real path: the directory slug is lossy, since every run of non-alphanumeric characters collapses to `-`, so a dot or a genuine hyphen in a path cannot be reversed.

## Why Python rather than shell

The tools have to edit `~/.claude.json` — tens of kilobytes of JSON — without corrupting it. That rules out hand-rolled `sed`/`awk`, and `jq` is not installed by default on macOS or on most Linux distributions. `perl` with core `JSON::PP` would work and is bundled with macOS, but needs careful `utf8` handling to avoid mangling non-ASCII paths.

Python 3 also sidesteps a pile of GNU/BSD divergence (`realpath -m`, `stat`, `sed -i`, `pgrep -a`) and provides `socket.gethostname()` without needing a `hostname` binary, which minimal installs omit.

## Licence

MIT
