"""Which environment variables and how much time each kind of child process gets.

One table for every process DevRoster starts from project settings, so a variable that
holds a secret (cloud keys, tokens, passwords in the user's shell) reaches a project
command only when the project lists it. Pure data and selection; no process is started.

Kinds:
- hook: .ai/hooks commands (system basics only, plus DEVROSTER_* context)
- mcp_stdio: a project's stdio MCP server (basics, proxy/certificates, configured keys)
- check: verification gates and the checks a code role may run (basics, toolchains,
  proxy/certificates, the project's approved keys)
- preview: a project's preview service (same as check)
- git: git and gh, which need the user's own credentials helpers and agents; they keep
  the full environment but never prompt on a terminal
"""

from __future__ import annotations

from collections.abc import Mapping

SYSTEM = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "LANG", "LANGUAGE",
    "TMPDIR", "TEMP", "TMP",
    # Windows needs these to start most programs.
    "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)
SYSTEM_PREFIXES = ("LC_",)
NETWORK = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
)
TOOLCHAINS = (
    "JAVA_HOME", "GRADLE_HOME", "GRADLE_USER_HOME", "MAVEN_HOME", "M2_HOME",
    "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE", "CARGO_HOME", "RUSTUP_HOME",
    "NVM_DIR", "NODE_PATH", "PNPM_HOME", "VOLTA_HOME", "BUN_INSTALL",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PYENV_ROOT", "UV_CACHE_DIR",
    "DOTNET_ROOT", "ANDROID_HOME", "ANDROID_SDK_ROOT", "FLUTTER_ROOT", "PUB_CACHE", "SDKMAN_DIR",
    "HOMEBREW_PREFIX", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
)
# The app-private CLI homes (core.cli_home) stay pointed at DevRoster's folders.
ISOLATION = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME", "DEVROSTER_HOME")

KINDS = {
    "hook": {"keys": SYSTEM, "timeout": 60},
    "mcp_stdio": {"keys": SYSTEM + NETWORK, "timeout": 30},
    "check": {"keys": SYSTEM + NETWORK + TOOLCHAINS + ISOLATION, "timeout": 900},
    "preview": {"keys": SYSTEM + NETWORK + TOOLCHAINS + ISOLATION, "timeout": None},
    "git": {"keys": None, "timeout": 300},
}


def timeout(kind: str) -> float | None:
    return KINDS[kind]["timeout"]


def environment(kind: str, source: Mapping[str, str], allowed=(), extra: Mapping[str, str] | None = None) -> dict:
    """The child environment: the kind's variables from ``source``, the project's ``allowed``
    keys (names it approved), then ``extra`` values set by DevRoster itself."""
    keys = KINDS[kind]["keys"]
    if keys is None:
        env = dict(source)
    else:
        wanted = set(keys) | set(allowed)
        env = {
            key: value
            for key, value in source.items()
            if key in wanted or key.startswith(SYSTEM_PREFIXES)
        }
    if kind == "git":
        env["GIT_TERMINAL_PROMPT"] = "0"  # A missing credential fails instead of waiting forever.
    env.update(extra or {})
    return env
