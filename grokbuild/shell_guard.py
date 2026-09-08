"""Read-only shell and tool classification."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from grokbuild.payloads import tool_input as payload_tool_input

ALWAYS_READ = frozenset(
    {
        "read_file",
        "request_visual_analysis",
        "get_command_or_subagent_output",
        "wait_commands_or_subagents",
        "search_tool",
        "scheduler_list",
        "todo_write",
        "memory_search",
        "memory_get",
        "web_fetch",
        "ask_user_question",
        "enter_plan_mode",
        "exit_plan_mode",
    }
)

CONDUCTOR_RECON_TOOLS = frozenset({"grep", "list_dir", "search", "web_search"})

READ_TOOLS = ALWAYS_READ | CONDUCTOR_RECON_TOOLS

LIFECYCLE_TOOLS = frozenset({"kill_command_or_subagent"})

SCHEDULER_WRITE_TOOLS = frozenset({"scheduler_create", "scheduler_delete"})

SHELL_INTERPRETERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "csh",
        "tcsh",
        "python",
        "python2",
        "python3",
        "py",
        "node",
        "nodejs",
        "deno",
        "bun",
        "ruby",
        "perl",
        "php",
        "lua",
        "luajit",
        "tclsh",
        "R",
        "Rscript",
        "julia",
        "awk",
        "sed",
        "eval",
        "exec",
        "source",
        "xargs",
        "sudo",
        "doas",
        "env",
        "nice",
        "nohup",
        "timeout",
        "setsid",
        "su",
        "make",
        "cmake",
        "cargo",
        "go",
        "rustc",
        "gcc",
        "g++",
        "clang",
        "npx",
        "yarn",
        "npm",
        "pip",
        "pip3",
        "tee",
        "curl",
        "wget",
        "nc",
        "netcat",
        "socat",
    }
)

RECON_SHELL_COMMANDS = frozenset({"ls", "find", "rg", "grep"})

READONLY_SHELL_COMMANDS = frozenset(
    {
        "ls",
        "cat",
        "cd",
        "echo",
        "head",
        "tail",
        "grep",
        "rg",
        "wc",
        "stat",
        "file",
        "pwd",
        "which",
        "diff",
        "cmp",
        "sort",
        "uniq",
        "tr",
        "cut",
        "paste",
        "comm",
        "join",
        "nl",
        "od",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "printf",
        "strings",
        "date",
        "uname",
        "df",
        "du",
        "free",
        "ps",
        "mount",
        "lsof",
        "uptime",
    }
)

GIT_SUBCOMMAND_WRITE_FLAGS: dict[str, frozenset[str]] = {
    "fsck": frozenset({"--lost-found"}),
    "help": frozenset({"-w", "--web"}),
    "grep": frozenset({"-O", "--open-files-in-pager"}),
    "log": frozenset({"--ext-diff", "--textconv"}),
    "diff": frozenset({"--ext-diff", "--textconv"}),
    "show": frozenset({"--ext-diff", "--textconv"}),
    "whatchanged": frozenset({"--ext-diff", "--textconv"}),
}


READ_GIT_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "grep",
        "ls-files",
        "ls-tree",
        "show-ref",
        "rev-parse",
        "rev-list",
        "blame",
        "shortlog",
        "describe",
        "name-rev",
        "cat-file",
        "count-objects",
        "fsck",
        "whatchanged",
        "help",
        "version",
    }
)

FIND_WRITE_FLAGS = frozenset(
    {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
        "-cpio",
    }
)


def _has_output_target(tokens: list[str]) -> bool:
    return any(
        token in {"-o", "--output"} or token.startswith(("-o=", "--output=", "--output"))
        for token in tokens
    )


def _uniq_has_positional_output(args: list[str]) -> bool:
    positionals = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
        elif token in {
            "-f",
            "-s",
            "-w",
            "--skip-fields",
            "--skip-chars",
            "--check-chars",
        }:
            skip_next = True
        elif re.fullmatch(r"-[fsw].+", token) or token.startswith(
            ("--skip-fields=", "--skip-chars=", "--check-chars=")
        ):
            continue
        elif token == "-" or not token.startswith("-"):
            positionals.append(token)
    return len(positionals) >= 2


def _git_has_write_flag(tokens: list[str]) -> bool:
    args = [token for token in tokens if not token.startswith("-")]
    if not args:
        return False
    subcommand = args[0]
    flags = GIT_SUBCOMMAND_WRITE_FLAGS.get(subcommand, frozenset())
    return any(
        token == flag or token.startswith(flag + "=")
        or (subcommand == "grep" and flag == "-O" and token.startswith("-O"))
        for token in tokens
        for flag in flags
    )


def _is_readonly_shell_segment(tokens: list[str]) -> bool:
    if not tokens or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=", token)
        for token in tokens
        if not token.startswith("-")
    ):
        return False
    name = Path(tokens[0]).name
    if name in SHELL_INTERPRETERS:
        return False
    args = tokens[1:]
    if name == "find":
        return not any(flag in FIND_WRITE_FLAGS for flag in args)
    if name == "git":
        return _is_readonly_git(args)
    if name == "sort":
        if _has_output_target(args) or any(
            re.fullmatch(r"-[a-z]*o.*", token)
            for token in args
        ):
            return False
    elif name in {"diff", "cmp", "comm"} and _has_output_target(args):
        return False
    if name == "uniq" and _uniq_has_positional_output(args):
        return False
    if name == "rg" and any(
        token == "--pre"
        or token.startswith(("--pre=", "--pre-glob", "--pre-glob="))
        for token in args
    ):
        return False
    if name == "date":
        return not args or (len(args) == 1 and args[0].startswith("+"))
    if name == "mount":
        return not args
    return name in READONLY_SHELL_COMMANDS and not _has_output_target(args)


def readonly_shell_verdict(command: str | None) -> str:
    """Classify a shell command conservatively."""
    cmd = command or ""
    if not cmd.strip() or "\x00" in cmd:
        return "unprovable"
    if any(sep in cmd for sep in ("\n", "\r", "`", "$(", ">", "<")):
        return "write"
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars="|&;")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return "unprovable"
    if not tokens:
        return "unprovable"
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {"|", ";", "&&", "||"}:
            if not segments[-1] or token == "&":
                return "unprovable"
            segments.append([])
        elif token == "&" or token in {"|||", "&&&", "||&"}:
            return "write"
        else:
            segments[-1].append(token)
    if any(not segment for segment in segments):
        return "unprovable"
    for segment in segments:
        name = Path(segment[0]).name
        args = segment[1:]
        write_commands = {"rm", "rmdir", "unlink", "mv", "cp", "chmod", "mkdir", "touch"}
        apply_commands = {"kubectl", "terraform", "tofu"}
        if name in SHELL_INTERPRETERS or name in write_commands:
            return "write"
        if name in apply_commands and "apply" in args:
            return "write"
        if name == "find" and any(flag in FIND_WRITE_FLAGS for flag in args):
            return "write"
        if name == "rg" and any(
            token == "--pre"
            or token.startswith(("--pre=", "--pre-glob", "--pre-glob="))
            for token in args
        ):
            return "write"
        if name == "sort" and any(re.fullmatch(r"-[a-z]*o.*", token) for token in args):
            return "write"
        if name == "uniq" and _uniq_has_positional_output(args):
            return "write"
        if name == "git" and _git_has_write_flag(args):
            return "write"
        if _is_readonly_shell_segment(segment):
            continue
        if _has_output_target(args):
            return "write"
        return "unprovable"
    return "readonly"


def is_readonly_shell(command: str | None) -> bool:
    return readonly_shell_verdict(command) == "readonly"


def _is_readonly_git(tokens: list[str]) -> bool:
    """Conservatively recognize Git inspection commands.

    Global Git options are skipped to find the subcommand.  Commands with both
    read and write forms (``worktree``, ``branch``, ``tag``, and ``remote``)
    receive a narrower, argument-aware allowlist.
    """
    if _has_output_target(tokens):
        return False
    args = [token for token in tokens if not token.startswith("-")]
    if not args:
        return False
    subcommand = args[0]
    if _git_has_write_flag(tokens):
        return False
    if subcommand in READ_GIT_SUBCOMMANDS:
        return True
    if subcommand == "worktree":
        return args[1:2] == ["list"]
    if subcommand == "branch":
        return _is_readonly_git_branch(tokens, subcommand)
    if subcommand == "tag":
        return _is_readonly_git_tag(tokens, subcommand)
    if subcommand == "remote":
        return _is_readonly_git_remote(tokens, subcommand)
    return False


def _git_subcommand_index(tokens: list[str], subcommand: str) -> int:
    """Return the first non-option occurrence of a Git subcommand."""
    for index, token in enumerate(tokens):
        if token == subcommand:
            return index
    return -1


def _is_readonly_git_branch(tokens: list[str], subcommand: str) -> bool:
    index = _git_subcommand_index(tokens, subcommand)
    command_args = tokens[index + 1 :]
    if not command_args:
        return True
    listing_flags = {
        "-a",
        "-r",
        "-v",
        "--all",
        "--remotes",
        "--verbose",
        "--list",
        "--show-current",
        "--no-color",
    }
    saw_list = False
    for token in command_args:
        if token == "--list":
            saw_list = True
            continue
        if token in listing_flags or (token.startswith("-") and set(token[1:]) <= {"a", "r", "v"}):
            continue
        if token.startswith("--format=") or token.startswith("--color="):
            continue
        # A positional argument is a branch-creation request unless --list
        # explicitly selected listing mode.
        if saw_list:
            continue
        return False
    return True


def _is_readonly_git_tag(tokens: list[str], subcommand: str) -> bool:
    index = _git_subcommand_index(tokens, subcommand)
    command_args = tokens[index + 1 :]
    if not command_args:
        return True
    return all(
        token
        in {
            "-l",
            "--list",
            "-n",
            "--no-column",
            "--contains",
            "--no-contains",
            "--merged",
            "--no-merged",
        }
        or token.startswith(("--format=", "--sort=", "--column="))
        for token in command_args
    )


def _is_readonly_git_remote(tokens: list[str], subcommand: str) -> bool:
    index = _git_subcommand_index(tokens, subcommand)
    command_args = tokens[index + 1 :]
    return not command_args or command_args == ["-v"] or command_args == ["--verbose"]


def is_recon_shell(command: str | None) -> bool:
    """Recognize cooperative tree-mapping commands while recon is owed."""
    tokens = (command or "").strip().split()
    if not tokens:
        return False
    return Path(tokens[0]).name in RECON_SHELL_COMMANDS and is_readonly_shell(command)


def _tool_command(data: dict) -> str:
    tool_input = payload_tool_input(data)
    cmd = tool_input.get("command") or tool_input.get("cmd") or tool_input.get("shell")
    return str(cmd) if isinstance(cmd, str) else ""


def _is_write_tool(tool: str, data: dict | None = None) -> bool:
    """True when a tool can change files/state (unknown tools count as write)."""
    name = (tool or "").casefold()
    if name in (READ_TOOLS | LIFECYCLE_TOOLS):
        return False
    if name == "run_terminal_command":
        return not is_readonly_shell(_tool_command(data or {}))
    return True
