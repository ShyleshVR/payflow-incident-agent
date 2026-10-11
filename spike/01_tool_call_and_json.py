"""Spike: the two things the agent needs from the local model, through LangChain.

  1. Tool calling: the model asks for a tool to be run, our code runs it, the model answers.
  2. Structured output: the model's answer is forced to match a fixed JSON shape.

Needs the model server (scripts/start-llm.ps1). Run:
    .venv\\Scripts\\python.exe spike\\01_tool_call_and_json.py
"""
import time
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

# llama-server speaks the OpenAI API, so LangChain's OpenAI client works with a local base_url.
llm = ChatOpenAI(
    base_url="http://127.0.0.1:8090/v1",
    api_key="not-needed",
    model="qwen3.5-9b",
    temperature=0,  # the same input should give the same answer
    max_tokens=600,
    # Qwen3.5 reasons privately before answering unless told not to (see the README of the spike).
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)


# ---------------------------------------------------------------- 1. tool calling

# A fake tool: the real one will ask Prometheus. The docstring is what the model reads to decide
# when to call it, so it says what the tool returns and when it is useful.
@tool
def get_consumer_lag(group: str) -> str:
    """Returns how many messages a Kafka consumer group is behind, and whether its pods are up.
    Use it when an alert says a service is not keeping up with its messages."""
    fake = {
        "ledger-service": "lag=22759 messages and growing; pods: 2/2 Running",
        "notification-service": "lag=12 messages, steady; pods: 2/2 Running",
    }
    return fake.get(group, f"no consumer group named {group}")


def tool_calling() -> None:
    print("=== 1. Tool calling")
    llm_with_tools = llm.bind_tools([get_consumer_lag])
    messages = [
        SystemMessage("You are an on-call engineer for a payments system. Use the tools to get facts "
                      "before you answer. Answer in one or two sentences."),
        HumanMessage("The alert KafkaConsumerLag fired for the ledger-service consumer group. "
                     "How bad is it?"),
    ]

    started = time.perf_counter()
    first = llm_with_tools.invoke(messages)
    # The model did not answer: it returned a request to run a tool, with arguments it chose.
    print("model asked for:", first.tool_calls)
    messages.append(first)

    # Our code runs the tool and hands the result back. The model never runs anything itself.
    for call in first.tool_calls:
        result = get_consumer_lag.invoke(call["args"])
        print("tool returned:  ", result)
        messages.append(ToolMessage(content=result, tool_call_id=call["id"]))

    final = llm_with_tools.invoke(messages)
    print("model answered: ", final.content.strip())
    print(f"({time.perf_counter() - started:.1f}s for both model calls)\n")


# ---------------------------------------------------------------- 2. structured output

class Diagnosis(BaseModel):
    """What the diagnose step of the agent will return."""
    root_cause: Literal["processor_outage", "kafka_down", "ledger_down", "database_unreachable", "unknown"]
    confidence: float = Field(ge=0, le=1, description="0 = a guess, 1 = certain")
    evidence: list[str] = Field(description="The facts from the input that support the root cause")
    summary: str = Field(description="One sentence for the engineer on call")


def structured_output() -> None:
    print("=== 2. Structured output")
    # The schema is sent to the server, which only lets the model produce text that matches it.
    diagnoser = llm.with_structured_output(Diagnosis)

    started = time.perf_counter()
    diagnosis = diagnoser.invoke([
        SystemMessage("You diagnose incidents in a payments system. Use only the evidence given. "
                      "If it does not point to one cause, answer unknown."),
        HumanMessage(
            "Runbook for KafkaConsumerLag: check that the group's pods are up. If they are, check "
            "their logs: an infrastructure error (the database down) holds the partition until it "
            "recovers.\n\n"
            "Evidence:\n"
            "- ledger-service consumer lag is 22759 and growing\n"
            "- ledger-service pods: 2/2 Running, 0 restarts\n"
            "- ledger-service logs, last 5 minutes: 412 x 'Connection to postgres:5432 refused'\n"
            "- Kafka broker: up\n"
            "- processor circuit breaker: closed"
        ),
    ])
    # A Python object with typed fields, not text to parse.
    print("type:      ", type(diagnosis).__name__)
    print("root_cause:", diagnosis.root_cause)
    print("confidence:", diagnosis.confidence)
    print("evidence:  ", diagnosis.evidence)
    print("summary:   ", diagnosis.summary)
    print(f"({time.perf_counter() - started:.1f}s)")


if __name__ == "__main__":
    tool_calling()
    structured_output()
