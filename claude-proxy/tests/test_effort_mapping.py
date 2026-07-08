"""T6.3 — _apply_effort_mapping：effort 条件覆写（业务契约：仅当原请求含 output_config 才覆写）。"""
from local_proxy import SmartProxy

CFG = {"effort_mapping": {"opus": "max", "haiku": "high"}}


def apply(data, tier, config=CFG):
    SmartProxy._apply_effort_mapping(None, data, tier, config, False)
    return data


def test_no_output_config_unchanged():
    d = {"x": 1}
    assert apply(d, "opus") == {"x": 1}  # 无 output_config → 不新增字段、原样透传


def test_hit_overrides():
    d = {"output_config": {"effort": "low"}}
    assert apply(d, "opus")["output_config"]["effort"] == "max"


def test_empty_effort_not_overridden():
    cfg = {"effort_mapping": {"opus": ""}}  # 目标 effort 为空
    d = {"output_config": {"effort": "low"}}
    assert apply(d, "opus", cfg)["output_config"]["effort"] == "low"  # falsy → 不覆写


def test_tier_not_in_map_unchanged():
    d = {"output_config": {"effort": "low"}}
    assert apply(d, "sonnet")["output_config"]["effort"] == "low"  # sonnet 不在 effort_map
