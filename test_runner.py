"""Deterministic, offline tests for runner.py's run_turn helper."""

from __future__ import annotations

from runner import run_turn


class _FakePart:
    def __init__(self, text: str | None) -> None:
        self.text = text


class _FakeContent:
    def __init__(self, parts: list[_FakePart]) -> None:
        self.parts = parts


class _FakeEvent:
    def __init__(self, *, final: bool, text: str | None) -> None:
        self._final = final
        self.content = _FakeContent([_FakePart(text)]) if text is not None else None

    def is_final_response(self) -> bool:
        return self._final


class _FakeRunner:
    def __init__(self, events: list[_FakeEvent]) -> None:
        self._events = events
        self.calls: list[dict] = []

    def run(self, *, user_id: str, session_id: str, new_message):
        self.calls.append(
            {"user_id": user_id, "session_id": session_id, "prompt": new_message.parts[0].text}
        )
        return iter(self._events)


def test_run_turn_returns_final_text():
    runner = _FakeRunner([_FakeEvent(final=False, text=None), _FakeEvent(final=True, text="hi there")])
    result = run_turn(runner, user_id="u", session_id="s", prompt="hello")
    assert result == "hi there"
    assert runner.calls == [{"user_id": "u", "session_id": "s", "prompt": "hello"}]


def test_run_turn_no_final_event_returns_placeholder():
    runner = _FakeRunner([_FakeEvent(final=False, text="ignored")])
    assert run_turn(runner, user_id="u", session_id="s", prompt="hello") == "(no response)"
