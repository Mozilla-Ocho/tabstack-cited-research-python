from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, List

import pytest
from tabstack._models import construct_type
from tabstack.types.research_event import ResearchEvent

FIXTURES = Path(__file__).parent / "fixtures"


def load_events(name: str) -> List[Any]:
    """Parse a JSONL fixture into the SDK's typed event models, exactly as the stream would."""
    events: List[Any] = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(construct_type(type_=ResearchEvent, value=json.loads(line)))
    return events


class FakeAgent:
    def __init__(self, events: List[Any]):
        self._events = events
        self.calls: List[dict] = []

    def research(self, **kwargs: Any) -> Iterator[Any]:
        self.calls.append(kwargs)
        return iter(self._events)


class FakeClient:
    max_retries = 2

    def __init__(self, events: List[Any]):
        self.agent = FakeAgent(events)
        self.closed = False

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.closed = True


@pytest.fixture
def fake_client_factory():
    def factory(fixture_name: str):
        client = FakeClient(load_events(fixture_name))
        return client, (lambda: client)

    return factory
