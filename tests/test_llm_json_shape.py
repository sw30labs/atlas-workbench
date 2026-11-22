"""chat_json helpers must return a container and must not raise."""

from workbench_atlas.llm import LLMError, OMLXClient


class _Stub:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def chat_json(self, _messages, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.payload


def test_chat_json_object_returns_a_dict_or_empty():
    assert OMLXClient.chat_json_object(_Stub({"a": 1}), []) == {"a": 1}
    assert OMLXClient.chat_json_object(_Stub({"a": 1}), [], keys=("a",)) == {"a": 1}
    for payload in ([1], "x", None, 3):
        assert OMLXClient.chat_json_object(_Stub(payload), []) == {}
    for error in (LLMError("bad"), ValueError("bad"), TypeError("bad")):
        assert OMLXClient.chat_json_object(_Stub(error=error), []) == {}


def test_chat_json_list_returns_a_list_or_empty():
    assert OMLXClient.chat_json_list(_Stub([1, 2]), []) == [1, 2]
    assert OMLXClient.chat_json_list(_Stub({"items": [3], "n": 1}), []) == [3]
    for payload in ({"n": 1}, "x", None, 3):
        assert OMLXClient.chat_json_list(_Stub(payload), []) == []
    for error in (LLMError("bad"), ValueError("bad"), TypeError("bad")):
        assert OMLXClient.chat_json_list(_Stub(error=error), []) == []
