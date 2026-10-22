from src.utils.config import load_config


def test_config_supplements_env_with_env_example(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_MODEL", raising=False)
    (tmp_path / ".env").write_text("FUSION_MODE=mock\n")
    (tmp_path / ".env.example").write_text(
        "LITELLM_BASE_URL=https://example.invalid/\n"
        "LITELLM_API_KEY=example-key\n"
        "LITELLM_MODEL=example-model\n"
    )

    config = load_config()

    assert config.litellm_base_url == "https://example.invalid/"
    assert config.litellm_api_key == "example-key"
    assert config.litellm_model == "example-model"


def test_explicit_environment_values_override_both_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_MODEL", "environment-model")
    (tmp_path / ".env").write_text("LITELLM_MODEL=env-file-model\n")
    (tmp_path / ".env.example").write_text("LITELLM_MODEL=example-model\n")

    assert load_config().litellm_model == "environment-model"
