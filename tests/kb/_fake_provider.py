# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""A canned LLMProvider for fact-tier tests.

Returns whatever you queue via :meth:`FakeProvider.enqueue`. Each call pops
the next response off the queue. Records calls so tests can assert on
prompts and ordering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from synthadoc.providers.base import CompletionResponse, LLMProvider, Message


@dataclass
class _Call:
    messages: list[Message]
    system: Optional[str]
    temperature: float
    max_tokens: int


class FakeProvider(LLMProvider):
    supports_vision = False

    def __init__(self) -> None:
        self._queue: list[str | Exception] = []
        self.calls: list[_Call] = []

    def enqueue(self, response: str) -> None:
        self._queue.append(response)

    def enqueue_error(self, exc: Exception) -> None:
        self._queue.append(exc)

    async def complete(
        self,
        messages: list[Message],
        system: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> CompletionResponse:
        self.calls.append(_Call(messages=messages, system=system,
                                temperature=temperature, max_tokens=max_tokens))
        if not self._queue:
            raise RuntimeError("FakeProvider: no canned response queued")
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return CompletionResponse(text=nxt, input_tokens=100, output_tokens=50)
