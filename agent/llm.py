"""The model, as the workflow uses it: a question in, an answer of a given shape out.

The model itself is self-hosted (llama.cpp's llama-server, OpenAI-compatible). Its answers are
constrained to a JSON schema by the server, so an answer cannot name something that does not
exist; it can still be wrong, which is what the evidence checks and the evals are for.
"""
import time
from dataclasses import dataclass
from typing import Protocol

import openai
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from agent.trace import Trace

Messages = list[tuple[str, str]]   # (role, content), role = "system", "human" or "ai" (an earlier answer)


class ModelUnavailable(Exception):
    """The model server did not answer."""


class ModelError(Exception):
    """The model answered, but not with what was asked for."""


@dataclass(frozen=True)
class Reply:
    answer: BaseModel
    input_tokens: int | None = None
    output_tokens: int | None = None


class Model(Protocol):
    async def ask(self, messages: Messages, shape: type[BaseModel], max_tokens: int, thinking: bool = False) -> Reply: ...


def thinking_switch(model_name: str, thinking: bool) -> dict:
    """The chat template's own switch for a model's built-in thinking, sent with every request.
    Off, or as low as the model allows, unless a step asks for it: the answer's reasoning field
    does that job, and thinking counts against max_tokens. gpt-oss cannot stop reasoning; its
    lowest effort is the nearest to off. Other models (Qwen) take enable_thinking."""
    if model_name.startswith("gpt-oss"):
        return {"reasoning_effort": "high" if thinking else "low"}
    return {"enable_thinking": thinking}


class LocalModel:
    def __init__(self, url: str, name: str, http_async_client=None, cache_prompt: bool = False):
        self._url = url
        self.name = name
        self._http_async_client = http_async_client
        self.cache_prompt = cache_prompt     # see config.LLM_CACHE_PROMPT

    def _llm(self, max_tokens: int, thinking: bool) -> ChatOpenAI:
        return ChatOpenAI(
            base_url=self._url,
            api_key="not-needed",
            model=self.name,
            temperature=0,
            max_retries=0,
            extra_body={
                # Sent as written: the client library's own max_tokens setting does not reach
                # the request when an answer shape is asked for, and an answer without a limit
                # can run until the context is full.
                "max_tokens": max_tokens,
                # Thinking counts against max_tokens: off unless a step asks for it (per request,
                # through llama-server's chat template; each model has its own switch).
                "chat_template_kwargs": thinking_switch(self.name, thinking),
                # llama-server's own setting, on unless a request says otherwise
                "cache_prompt": self.cache_prompt,
            },
            http_async_client=self._http_async_client,
        )

    async def ask(self, messages: Messages, shape: type[BaseModel], max_tokens: int, thinking: bool = False) -> Reply:
        structured = self._llm(max_tokens, thinking).with_structured_output(shape, method="json_schema", include_raw=True)
        try:
            result = await structured.ainvoke(messages)
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise ModelUnavailable(f"The model server does not answer at {self._url} ({type(e).__name__}). "
                                   "Start it with scripts/start-llm.ps1") from e
        except (ValidationError, openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as e:
            # cut off by the token limit, or (from a server that does not enforce the shape) simply not it
            raise ModelError(f"The model's answer did not fit the shape asked for: {str(e).splitlines()[0]}") from e
        if result["parsed"] is None:
            raise ModelError(f"The model's answer did not fit the shape asked for: {result['parsing_error']}")
        usage = result["raw"].usage_metadata or {}
        return Reply(result["parsed"], usage.get("input_tokens"), usage.get("output_tokens"))


class RecordingModel:
    """Wraps a model: every question and answer goes into the trace, under the step that asked."""

    def __init__(self, model: Model, trace: Trace):
        self._model = model
        self._trace = trace

    async def ask(self, messages: Messages, shape: type[BaseModel], max_tokens: int, thinking: bool = False) -> Reply:
        clock = time.perf_counter()
        reply = await self._model.ask(messages, shape, max_tokens, thinking)
        self._trace.model_call(messages, reply.answer.model_dump(), time.perf_counter() - clock,
                               reply.input_tokens, reply.output_tokens)
        return reply
