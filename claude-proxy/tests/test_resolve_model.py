"""T6.2 — _resolve_model：模型名关键字映射（项目核心纯函数）。"""
import pytest
from local_proxy import SmartProxy

CONFIG = {"model_mapping": {"haiku": "ds-flash", "sonnet": "ds-pro", "opus": "ds-pro"}}


def resolve(model, config=CONFIG):
    # _resolve_model 不使用 self，传 None 即可
    return SmartProxy._resolve_model(None, model, config)


@pytest.mark.parametrize("model,tier,target", [
    ("claude-opus-4-1", "opus", "ds-pro"),
    ("claude-3-5-sonnet-20241022", "sonnet", "ds-pro"),
    ("claude-3-5-haiku-20241022", "haiku", "ds-flash"),
    ("CLAUDE-OPUS-4", "opus", "ds-pro"),          # 大小写
    ("", "haiku", "ds-flash"),                     # 空串 → 默认 haiku
    ("claude-unknown-model", "haiku", "ds-flash"), # 未识别 → 默认 haiku
])
def test_resolve(model, tier, target):
    assert resolve(model) == (tier, target)


def test_priority_first_keyword():
    # 同时含多个关键字时按 TIERS 顺序取第一个（opus 在 sonnet 前）
    assert resolve("claude-opus-sonnet-mix")[0] == "opus"


def test_mapping_missing_key_falls_back_to_default():
    cfg = {"model_mapping": {"haiku": "ds-flash"}}  # 缺 opus
    assert resolve("claude-opus-4", cfg) == ("opus", "ds-flash")  # 回落 default(haiku) 的映射


def test_mapping_missing_all_raises():
    with pytest.raises(ValueError):
        resolve("claude-opus-4", {"model_mapping": {}})  # 缺 opus 且缺 default


def test_none_model_raises_attribute_error():
    # model=None → .lower() 崩（已知边界：调用处默认空串，仅显式 "model": null 才到此）
    with pytest.raises(AttributeError):
        resolve(None)
