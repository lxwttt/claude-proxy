"""T6.5 — 配置解析：validate_config_schema 点路径校验 + load_config 各分支。"""
import common
import local_proxy


def test_validate_schema_present():
    assert common.validate_config_schema({"a": {"b": 1}}, ("a.b",)) is True


def test_validate_schema_missing():
    assert common.validate_config_schema({"a": {}}, ("a.b",)) is False


def test_validate_schema_non_dict_node_no_crash():
    # 中间节点为字符串/None/列表 时不崩，返回 False
    assert common.validate_config_schema({"a": "oops"}, ("a.b",)) is False
    assert common.validate_config_schema({"a": None}, ("a.b",)) is False
    assert common.validate_config_schema({"a": [1, 2]}, ("a.b",)) is False


_GOOD = (
    "current_setting: X\n"
    "settings:\n"
    "  X:\n"
    "    api_base_url: http://u\n"
    "    api_key: k\n"
    "    model_mapping: {haiku: m}\n"
)


def _load(tmp_path, monkeypatch, text):
    cfg = tmp_path / "model_config.yaml"
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setattr(local_proxy, "config_file_path", str(cfg))
    return local_proxy.load_config()


def test_load_config_ok(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch, _GOOD) is True


def test_load_config_missing_env(tmp_path, monkeypatch):
    text = "current_setting: NOPE\nsettings:\n  X: {api_base_url: u, api_key: k, model_mapping: {}}\n"
    assert _load(tmp_path, monkeypatch, text) is False


def test_load_config_missing_required_field(tmp_path, monkeypatch):
    text = "current_setting: X\nsettings:\n  X: {api_base_url: u, model_mapping: {}}\n"  # 缺 api_key
    assert _load(tmp_path, monkeypatch, text) is False


def test_load_config_bad_yaml(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch, "::: not yaml :::\n  - [") is False
