from agent_dev.config import settings


def test_masked_settings_round_trip_preserves_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'ENV_PATH', tmp_path/'.env')
    monkeypatch.setattr(settings, 'MCP_CONFIG_PATH', tmp_path/'.mcp.json')
    settings.set_env_vars({'API_KEY': 'original-secret'})
    settings.set_env_vars({'API_KEY': '****'})
    assert settings.read_env_pairs()['API_KEY'] == 'original-secret'
    settings.replace_mcp_servers({'tool': {'headers': {'Authorization': 'Bearer original-secret'}}})
    settings.replace_mcp_servers({'tool': {'headers': {'Authorization': '****'}}})
    assert settings.read_mcp_servers()['tool']['headers']['Authorization'] == 'Bearer original-secret'
