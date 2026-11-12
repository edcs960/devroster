"""Explicit registered-client OAuth with PKCE, resource binding and private storage."""

import base64
import hashlib
import secrets
import time
from typing import NoReturn
from urllib.parse import urlencode, urlsplit, urlunsplit

from agent_dev.adapters.mcp_client import request_remote
from agent_dev.adapters.project_setup import atomic_json
from agent_dev.domain.errors import DomainError, ErrorCode


def reject(message) -> NoReturn:
    raise DomainError(ErrorCode.AUTH_REQUIRED, message)


class IntegrationOAuth:
    def __init__(self, root):
        self.root = root
        self.pending = {}

    def path(self, key):
        from pathlib import Path

        from agent_dev.adapters.workspace import configuration_root
        from agent_dev.adapters.workspace_locator import workspace_locations

        # Tokens belong to one workspace; a connection ID never selects another one's file.
        directory = workspace_locations(configuration_root(Path(self.root))).oauth_dir
        if directory.is_symlink() or directory.parent.is_symlink():
            reject("인증 저장 위치에 심볼릭 링크를 사용할 수 없습니다.")
        return directory / (key + ".json")

    def token(self, key, resource=None):
        import json

        path = self.path(key)
        if not path.is_file():
            return ""
        if path.is_symlink():
            reject("인증 파일에 심볼릭 링크를 사용할 수 없습니다.")
        row = json.loads(path.read_text(encoding="utf-8"))
        if resource is not None and row.get("resource") != resource:
            return ""
        if row.get("expires_at", 0) <= time.time() + 30:
            return ""
        return row.get("access_token", "")

    def start(self, row, redirect_uri):
        client_id = row.get("oauth_client_id", "")
        if not client_id:
            reject(
                "제공자에 등록한 OAuth client ID를 입력해주세요. 지원 클라이언트 제한은 제공자에서 승인해야 합니다."
            )
        callback = urlsplit(redirect_uri)
        if (
            callback.scheme != "http"
            or callback.hostname != "127.0.0.1"
            or callback.path != "/api/integrations/callback"
            or callback.query
            or callback.fragment
        ):
            reject("로컬 인증 callback 주소를 확인해주세요.")
        resource = urlsplit(row["endpoint"])
        discovery = urlunsplit(
            (
                resource.scheme,
                resource.netloc,
                "/.well-known/oauth-protected-resource" + resource.path,
                "",
                "",
            )
        )
        status, _, protected = request_remote(discovery)
        if (
            status != 200
            or not isinstance(protected, dict)
            or protected.get("resource") != row["endpoint"]
        ):
            reject("제공자의 protected-resource metadata/resource 주소를 확인해주세요.")
        issuers = protected.get("authorization_servers", [])
        if not issuers or not isinstance(issuers[0], str):
            reject("제공자의 OAuth authorization metadata가 없습니다.")
        issuer = issuers[0].rstrip("/")
        parsed = urlsplit(issuer)
        metadata_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                "/.well-known/oauth-authorization-server" + parsed.path,
                "",
                "",
            )
        )
        status, _, metadata = request_remote(metadata_url)
        if (
            status != 200
            or metadata.get("issuer", "").rstrip("/") != issuer
            or "S256" not in metadata.get("code_challenge_methods_supported", [])
        ):
            reject("제공자가 PKCE S256 OAuth를 지원하지 않습니다.")
        for field in ("authorization_endpoint", "token_endpoint"):
            address = urlsplit(metadata.get(field, ""))
            if (
                address.scheme != "https"
                or address.hostname != parsed.hostname
                or address.username
                or address.fragment
            ):
                reject("OAuth metadata의 공식 endpoint를 확인해주세요.")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        self.pending[state] = {
            "id": row["id"],
            "client_id": client_id,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": row["endpoint"],
            "metadata": metadata,
            "expires": time.monotonic() + 300,
            "version": row["version"],
        }
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": row["endpoint"],
            }
        )
        return {
            "authorization_url": metadata["authorization_endpoint"] + "?" + query,
            "expires_in": 300,
        }

    def complete(self, state, code, resolver=None):
        pending = self.pending.pop(state, None)
        if (
            not pending
            or pending["expires"] <= time.monotonic()
            or not isinstance(code, str)
            or not 0 < len(code) < 4000
        ):
            reject("인증 요청이 만료되었거나 callback state가 다릅니다. 인증을 다시 시작해주세요.")
        if resolver is not None:
            row = resolver(pending["id"])
            if (
                row["version"] != pending["version"]
                or row["endpoint"] != pending["resource"]
                or not row["enabled"]
            ):
                reject("인증 중 연결 설정이 변경되었습니다. 인증을 다시 시작해주세요.")
        status, _, token = request_remote(
            pending["metadata"]["token_endpoint"],
            form={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "code_verifier": pending["verifier"],
                "resource": pending["resource"],
            },
            method="POST",
        )
        if (
            status != 200
            or not isinstance(token, dict)
            or token.get("token_type", "").lower() != "bearer"
            or not isinstance(token.get("access_token"), str)
        ):
            reject("OAuth token 교환에 실패했습니다.")
        try:
            expires = min(float(token.get("expires_in", 3600)), 86400 * 30)
        except (TypeError, ValueError):
            reject("OAuth token 만료 정보를 확인해주세요.")
        from agent_dev.adapters.project_setup import private_directory

        private_directory(self.path(pending["id"]).parent)
        atomic_json(
            self.path(pending["id"]),
            {
                "access_token": token["access_token"],
                "expires_at": time.time() + expires,
                "resource": pending["resource"],
            },
        )
        return {
            "connection_id": pending["id"],
            "authenticated": True,
            "actual_read_required": True,
            "version": pending["version"],
        }
