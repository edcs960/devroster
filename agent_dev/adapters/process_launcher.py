"""Start child processes with the environment and time limit their kind allows.

The rules live in domain.exec_policy; this module applies them to the real process
environment. New code that runs a project command or git uses ``environment`` and
``run`` here instead of passing ``env=None`` or omitting ``timeout``.
"""

from __future__ import annotations

import os
import subprocess

from agent_dev.domain import exec_policy


def environment(kind, allowed=(), extra=None, source=None) -> dict:
    return exec_policy.environment(kind, os.environ if source is None else source, allowed, extra)


def run(kind, argv, *, timeout=None, env=None, allowed=(), extra=None, check=False, **options):
    """subprocess.run with the kind's environment and a default time limit."""
    limit = exec_policy.timeout(kind) if timeout is None else timeout
    return subprocess.run(
        argv,
        env=environment(kind, allowed, extra) if env is None else env,
        timeout=limit,
        check=check,
        **options,
    )
