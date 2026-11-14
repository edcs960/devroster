"""Shared credential values go to the OS credential store; the file keeps where they are."""

import json
import os
import subprocess
import sys

import pytest

from agent_dev.adapters import credential_store, os_vault
from agent_dev.adapters.credential_store import CredentialStore, shared_directory
from agent_dev.adapters.diagnostics import diagnose
from agent_dev.adapters.os_vault import Dpapi, Keychain, VaultError
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.errors import DomainError, ErrorCode

CONNECTION = "vault-fixture"
SECRET = "sk-fixture-값-1234567890"


class Memory:
    """A store that behaves like the OS one, kept in this process."""

    kind = "memory"

    def __init__(self):
        self.items, self.labels, self.refuse, self.locked = {}, [], False, False

    def protect(self, label, value):
        if self.refuse:
            raise VaultError("unavailable")
        token = f"{len(self.labels):032x}"
        self.labels.append(label)
        self.items[token] = value
        return token

    def reveal(self, token):
        if self.locked or token not in self.items:
            raise VaultError("unavailable")
        return self.items[token]

    def forget(self, token):
        self.items.pop(token, None)


@pytest.fixture
def memory(monkeypatch):
    store = Memory()
    monkeypatch.setattr(os_vault, "vault", lambda *a, **k: store)
    monkeypatch.setattr(os_vault, "reader", lambda kind, *a: store if kind == "memory" else None)
    yield store
    CredentialStore.revoke_shared(CONNECTION)


@pytest.fixture(autouse=True)
def fresh_cache():
    os_vault.forget_cache()
    yield
    os_vault.forget_cache()


def stored():
    return (shared_directory() / f"{CONNECTION}.json").read_text(encoding="utf-8")


def workspace_store(tmp_path):
    from agent_dev.adapters.workspace_locator import workspace_locations

    initialize(tmp_path)
    locations = workspace_locations(tmp_path)
    return CredentialStore(locations), locations.workspace_id


def test_the_value_is_kept_by_the_os_store_and_not_written_to_the_file(memory, tmp_path):
    CredentialStore.save_shared(CONNECTION, "OPENAI_API_KEY", SECRET)
    row = json.loads(stored())
    assert SECRET not in stored() and row["values"] == {}
    assert row["vault"]["OPENAI_API_KEY"]["kind"] == "memory"
    assert memory.labels == [f"DevRoster/{CONNECTION}/OPENAI_API_KEY"]
    assert CredentialStore.shared_entry(CONNECTION) == {"OPENAI_API_KEY": SECRET}
    assert SECRET in CredentialStore.shared_values()  # Still masked in output.
    store, workspace_id = workspace_store(tmp_path)
    assert store.resolve(f"shared:{CONNECTION}:OPENAI_API_KEY", workspace_id, "") == {"OPENAI_API_KEY": SECRET}


def test_when_the_os_store_refuses_the_value_stays_in_the_file_as_before(memory):
    memory.refuse = True
    CredentialStore.save_shared(CONNECTION, "TOKEN", SECRET)
    row = json.loads(stored())
    assert row["values"] == {"TOKEN": SECRET} and row["vault"] == {}
    assert CredentialStore.shared_entry(CONNECTION) == {"TOKEN": SECRET}


def test_without_an_os_store_the_file_is_written_as_before(monkeypatch):
    # The default of the test run: DEVROSTER_VAULT=file.
    CredentialStore.save_shared(CONNECTION, "TOKEN", SECRET)
    try:
        assert json.loads(stored()) == {"resource": "", "values": {"TOKEN": SECRET}, "env": {}, "vault": {}}
    finally:
        CredentialStore.revoke_shared(CONNECTION)


def test_replacing_removing_and_revoking_leave_nothing_in_the_os_store(memory):
    CredentialStore.save_shared(CONNECTION, "A", "first-value")
    CredentialStore.save_shared(CONNECTION, "A", "second-value")
    assert list(memory.items.values()) == ["second-value"]
    CredentialStore.save_shared(CONNECTION, "B", "other-value")
    CredentialStore.remove_shared_key(CONNECTION, "A")
    assert list(memory.items.values()) == ["other-value"]
    assert CredentialStore.shared_entry(CONNECTION) == {"B": "other-value"}
    CredentialStore.save_shared(CONNECTION, "B", "env:FIXTURE_VAULT_B")  # A name replaces the value.
    assert memory.items == {} and json.loads(stored())["env"] == {"B": "FIXTURE_VAULT_B"}
    CredentialStore.save_shared(CONNECTION, "C", "third-value")
    CredentialStore.revoke_shared(CONNECTION)
    assert memory.items == {} and not (shared_directory() / f"{CONNECTION}.json").exists()


def test_a_failed_file_write_does_not_leave_the_new_value_behind(memory, monkeypatch):
    CredentialStore.save_shared(CONNECTION, "A", "first-value")

    def broken(path, value):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(credential_store, "atomic_json", broken)
        with pytest.raises(OSError):
            CredentialStore.save_shared(CONNECTION, "A", "second-value")
    assert list(memory.items.values()) == ["first-value"]
    assert CredentialStore.shared_entry(CONNECTION) == {"A": "first-value"}


def test_a_value_the_os_store_cannot_return_reads_as_not_set_and_says_why(memory, tmp_path, monkeypatch):
    CredentialStore.save_shared(CONNECTION, "TOKEN", SECRET)
    CredentialStore.save_shared(CONNECTION, "PLAIN", "env:FIXTURE_VAULT_PLAIN")
    monkeypatch.setenv("FIXTURE_VAULT_PLAIN", "from-env")
    memory.locked = True
    assert CredentialStore.shared_entry(CONNECTION) == {"PLAIN": "from-env"}
    store, workspace_id = workspace_store(tmp_path)
    with pytest.raises(DomainError) as error:
        store.resolve(f"shared:{CONNECTION}:TOKEN", workspace_id, "")
    assert error.value.code is ErrorCode.AUTH_REQUIRED and "OS 보안 저장소" in str(error.value)
    doctor = {c["check"]: c for c in diagnose(tmp_path)["checks"]}["credential_storage"]
    assert doctor["status"] == "warn" and "읽지 못한 공통 인증 값이 1개" in doctor["message"]
    memory.locked = False  # Unlocked again: nothing was lost.
    assert CredentialStore.shared_entry(CONNECTION)["TOKEN"] == SECRET


def test_doctor_reports_values_left_in_files_and_moves_them_on_request(memory, tmp_path):
    memory.refuse = True
    CredentialStore.save_shared(CONNECTION, "A", "first-value")
    CredentialStore.save_shared(CONNECTION, "B", "second-value")
    initialize(tmp_path)
    check = {c["check"]: c for c in diagnose(tmp_path)["checks"]}["credential_storage"]
    assert check["status"] == "warn" and "2개" in check["message"]
    assert "--protect-credentials" in check["fix"]
    # Still refused: reported, nothing lost.
    check = {c["check"]: c for c in diagnose(tmp_path, protect_credentials=True)["checks"]}["credential_storage"]
    assert check["status"] == "warn" and "옮기지 못했습니다" in check["message"]
    assert CredentialStore.shared_entry(CONNECTION) == {"A": "first-value", "B": "second-value"}
    memory.refuse = False
    result = diagnose(tmp_path, protect_credentials=True)
    check = {c["check"]: c for c in result["checks"]}["credential_storage"]
    assert check["status"] == "pass" and "2개를 OS 보안 저장소로 옮겼습니다" in check["message"]
    assert "first-value" not in stored() and "second-value" not in stored()
    assert "first-value" not in json.dumps(result, ensure_ascii=False)
    assert CredentialStore.shared_entry(CONNECTION) == {"A": "first-value", "B": "second-value"}
    check = {c["check"]: c for c in diagnose(tmp_path)["checks"]}["credential_storage"]
    assert check["status"] == "pass" and "2개를 OS 보안 저장소에 보관" in check["message"]


def test_doctor_passes_where_there_is_no_os_store(tmp_path):
    initialize(tmp_path)
    check = {c["check"]: c for c in diagnose(tmp_path)["checks"]}["credential_storage"]
    assert check["status"] == "pass" and "파일에 보관" in check["message"]


def test_a_file_with_a_malformed_vault_entry_is_refused(memory):
    CredentialStore.save_shared(CONNECTION, "A", "first-value")
    path = shared_directory() / f"{CONNECTION}.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["vault"]["A"] = "not-an-entry"
    path.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(DomainError):
        CredentialStore.shared_entry(CONNECTION)
    CredentialStore.revoke_shared(CONNECTION)  # An unreadable file can still be removed.
    assert not path.exists()


def test_which_store_is_used():
    assert os_vault.vault({}, "darwin").kind == "keychain"
    # Windows: only when asked for, until it has been run on a real Windows computer.
    assert os_vault.vault({}, "win32") is None
    assert os_vault.vault({"DEVROSTER_VAULT": "os"}, "win32").kind == "dpapi"
    assert os_vault.vault({}, "linux") is None
    assert os_vault.vault({"DEVROSTER_VAULT": "os"}, "linux") is None
    assert os_vault.vault({"DEVROSTER_VAULT": "file"}, "darwin") is None
    assert os_vault.vault({"DEVROSTER_VAULT": " FILE "}, "win32") is None


def test_the_switch_value_is_not_treated_as_a_secret(tmp_path):
    """A variable whose name looks like a secret has its value redacted everywhere."""
    from agent_dev.adapters.secrets import registered_secrets
    from agent_dev.config.settings import is_secret_key

    assert not is_secret_key(os_vault.SWITCH)
    assert os.environ[os_vault.SWITCH] == "file"
    assert "file" not in registered_secrets(tmp_path)
    # A value kept on another kind of computer cannot be read here.
    assert os_vault.reader("keychain", "win32") is None and os_vault.reader("dpapi", "darwin") is None
    assert os_vault.reader("keychain", "darwin").kind == "keychain"


class Security:
    """Stands in for /usr/bin/security: records every call and keeps items in memory."""

    def __init__(self):
        self.items, self.calls, self.corrupt, self.hang = {}, [], False, False

    def __call__(self, command, input=None, **options):
        self.calls.append((command, input))
        assert options["encoding"] == "utf-8" and options["check"] is False
        if self.hang:
            raise subprocess.TimeoutExpired(command, options["timeout"])
        words = (input.split() if command[1] == "-i" else command[1:])
        account = words[words.index("-a") + 1]
        code, out = 0, ""
        if words[0] == "add-generic-password":
            if account in self.items:
                code = 45
            else:
                self.items[account] = "v1.broken" if self.corrupt else words[words.index("-w") + 1]
        elif words[0] == "find-generic-password":
            code, out = (0, self.items[account] + "\n") if account in self.items else (44, "")
        elif words[0] == "delete-generic-password":
            code = 0 if self.items.pop(account, None) is not None else 44
        return subprocess.CompletedProcess(command, code, out, "")


@pytest.fixture
def security(monkeypatch):
    fake = Security()
    monkeypatch.setattr(os_vault.subprocess, "run", fake)
    return fake


def test_keychain_round_trip_keeps_the_value_out_of_the_arguments(security):
    keychain = Keychain()
    token = keychain.protect("DevRoster/ai tools/OPENAI_API_KEY", SECRET)
    assert keychain.reveal(token) == SECRET
    command, line = security.calls[0]
    assert command == ["/usr/bin/security", "-i"]
    assert "-l DevRoster/ai_tools/OPENAI_API_KEY " in line and line.endswith("\n") and line.count("\n") == 1
    for command, _ in security.calls:
        assert SECRET not in " ".join(command) and "v1." not in " ".join(command)
    assert SECRET not in line  # Encoded: any character survives the tool's own parsing.
    keychain.forget(token)
    assert security.items == {}
    with pytest.raises(VaultError):
        keychain.reveal(token)


def test_keychain_reads_once_per_process_and_does_not_retry_a_failure_at_once(security):
    keychain = Keychain()
    token = keychain.protect("label", SECRET)
    calls = len(security.calls)
    assert keychain.reveal(token) == SECRET and len(security.calls) == calls
    missing = "0" * 32
    for _ in range(3):
        with pytest.raises(VaultError):
            keychain.reveal(missing)
    assert len(security.calls) == calls + 1


def test_keychain_refuses_what_it_cannot_pass_safely(security):
    keychain = Keychain()
    with pytest.raises(VaultError):
        keychain.protect("label", "x" * 4000)  # Longer than one command line of the tool.
    with pytest.raises(VaultError):
        Keychain(keychain="/tmp/with space/test.keychain-db").protect("label", SECRET)
    assert security.calls == []
    with pytest.raises(VaultError):
        keychain.reveal("../not-a-token")
    assert security.calls == []


def test_keychain_that_stores_something_else_is_not_trusted(security):
    security.corrupt = True
    with pytest.raises(VaultError):
        Keychain().protect("label", SECRET)
    assert security.items == {}  # The wrong item is removed again.


def test_keychain_timeout_is_a_refusal_without_the_value_in_the_error(security):
    security.hang = True
    with pytest.raises(VaultError) as error:
        Keychain().protect("label", SECRET)
    assert SECRET not in repr(error.value) and error.value.__cause__ is None
    assert error.value.__suppress_context__


def test_dpapi_round_trip_and_failures():
    def transform(data, protect):
        return bytes(b ^ 0x5A for b in data)

    dpapi = Dpapi(transform)
    token = dpapi.protect("label", SECRET)
    assert SECRET not in token and dpapi.reveal(token) == SECRET
    os_vault.forget_cache()
    assert dpapi.reveal(token) == SECRET

    def broken(data, protect):
        raise OSError("no such library")

    with pytest.raises(VaultError):
        Dpapi(broken).protect("label", SECRET)
    with pytest.raises(VaultError):
        Dpapi(lambda data, protect: data + b"x").protect("label", SECRET)  # Does not read back.
    with pytest.raises(VaultError):
        Dpapi(transform).reveal("not base64 !!")
    if sys.platform != "win32":
        with pytest.raises(VaultError):
            Dpapi().protect("label", SECRET)  # The real call exists on Windows only.


@pytest.mark.skipif(sys.platform != "darwin", reason="the keychain is a macOS store")
def test_real_keychain_file_round_trip(tmp_path):
    """The real tool against a keychain file of this test (the login keychain is not used)."""
    path = tmp_path / "test.keychain-db"
    if not os_vault.WORD.fullmatch(str(path)):
        pytest.skip("temporary folder name has characters the tool's command line cannot carry")

    def tool(*arguments):
        return subprocess.run(
            ["/usr/bin/security", *arguments], check=False, capture_output=True, text=True, encoding="utf-8", timeout=30
        )

    if tool("create-keychain", "-p", "fixture-only", str(path)).returncode != 0:
        pytest.skip("this session cannot create a keychain file")
    try:
        tool("unlock-keychain", "-p", "fixture-only", str(path))
        keychain = Keychain(keychain=path)
        for value in (SECRET, "x" * 2500, "quote\"'\\ $HOME `id`\n둘째 줄"):
            token = keychain.protect(f"DevRoster/{CONNECTION}/KEY", value)
            assert keychain.reveal(token, cached=False) == value
            keychain.forget(token)
            with pytest.raises(VaultError):
                keychain.reveal(token, cached=False)
        with pytest.raises(VaultError):
            keychain.protect("label", "x" * 8000)
    finally:
        tool("delete-keychain", str(path))
