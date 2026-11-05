"""Outbox sender, independent of task execution and with durable Discord nonce."""

import json
import os
import urllib.request

from agent_dev.domain.errors import DomainError, ErrorCode


class DiscordOutboxSender:
    def __init__(self, channel_id, credential_ref="DISCORD_BOT_TOKEN", resolver=os.environ.get):
        if not str(channel_id).isdigit():
            raise DomainError(ErrorCode.INVALID_INPUT, "Discord channel must be a snowflake ID.")
        self.channel, self.ref, self.resolve = str(channel_id), credential_ref, resolver

    def __call__(self, delivery_key, payload):
        token = self.resolve(self.ref)
        if not token:
            raise DomainError(ErrorCode.AUTH_REQUIRED, "Discord credential reference unavailable.")
        nonce = str(int(delivery_key[:15], 16))
        content = (
            f"{payload.get('aggregate_id', '')}: {payload.get('kind', 'Workflow')}\n"
            + json.dumps(payload.get("payload", {}), ensure_ascii=False)
        )
        request = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{self.channel}/messages",
            data=json.dumps(
                {
                    "content": content[:1900],
                    "nonce": nonce,
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                }
            ).encode(),
            headers={
                "Authorization": "Bot " + token,
                "Content-Type": "application/json",
                "User-Agent": "devroster/0.3",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read(12000))
        except OSError:
            raise DomainError(
                ErrorCode.EXTERNAL_TRANSIENT, "Discord delivery failed; retry scheduled."
            ) from None
