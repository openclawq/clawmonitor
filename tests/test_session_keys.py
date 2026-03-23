from clawmonitor.session_keys import is_modelprobe_session_key, parse_session_key


def test_parse_session_key_channel_like_modelprobe_still_detectable() -> None:
    key = "agent:main:modelprobe:2484dad0e551"
    info = parse_session_key(key)
    assert info.kind == "channel"
    assert info.channel == "modelprobe"
    assert is_modelprobe_session_key(key) is True


def test_non_modelprobe_session_key_not_marked() -> None:
    assert is_modelprobe_session_key("agent:main:main") is False
    assert is_modelprobe_session_key("agent:main:feishu:group:oc_xxx") is False
