from loader import load_config


def test_config_values():
    assert load_config() == {"name": "demo", "retries": 3}
