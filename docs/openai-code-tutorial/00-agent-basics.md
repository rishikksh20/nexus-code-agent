# 00 — Agent Basics: Build Your First Agent From Scratch

## Who this tutorial is for

You know Python. You have used functions, loops, and dictionaries. You may have called an API once or twice. That is enough.

This tutorial does **not** assume you know anything about AI agents, LLMs, or agent frameworks. It builds every idea from zero, in code, one step at a time.

By the end of this chapter you will have written a tiny working agent — entirely in plain Python, no external libraries — and you will understand exactly why every part is there.

---

## What you will build in this chapter

A minimal agent that can:

1. Accept text from the user
2. Decide (via a fake model) whether to answer directly or run a tool
3. Run the tool if requested
4. Feed the result back and produce a final answer
5. Keep looping until the user says `quit`

It will run from your terminal with `python agent.py`.

**Prerequisites:**

- Python 3.10+
- A text editor
- No pip installs needed for this chapter

---

## 1. What is an agent, exactly?

Before writing code, lock in the definition you will use throughout this series:

> **An agent is a program that repeatedly asks a model what to do next, executes actions when requested, and continues until the task is complete.**

The critical word is *repeatedly*. A simple chatbot asks the model once and stops. An agent keeps going — inspecting each model response, doing work, feeding results back, asking again — until the model signals it is done.

Here is that contrast as pseudocode:

```
# Chatbot (one-shot)
answer = model.ask(user_prompt)
print(answer)

# Agent (loop)
while not done:
    response = model.ask(conversation_so_far)
    if response wants an action:
        result = run_action(response.action)
        add result to conversation
    else:
        print(response.text)
        done = True
```

Everything in this series is a refinement of that second pattern.

---

## 2. Vocabulary you need right now

These five terms appear in every agentic system. Learn them once here; they will not change.

| Term | What it means in code |
|---|---|
| **Model** | A callable that receives a prompt and returns a structured response |
| **Runtime** | The program surrounding the model — it calls the model, runs tools, stores history |
| **Tool** | A Python function the runtime can invoke when the model requests it |
| **Message history** | The growing list of turns passed to the model each round |
| **Agent loop** | The `while` loop that drives the whole thing |

A sixth term will appear later:

| Term | What it means in code |
|---|---|
| **Stream event** | A structured dict/dataclass emitted while work is happening (e.g. `tool_started`, `text_chunk`) |

You do not need stream events yet, but keep the term in mind.

---

## 3. Start with the simplest possible loop

Create a file called `agent.py`. Paste this:

```python
# agent.py  –  step 1: plain loop, no AI

while True:
    user_input = input("you> ").strip()

    if user_input in {"quit", "exit", "q"}:
        print("Goodbye.")
        break

    if not user_input:
        continue

    print(f"echo> {user_input}")
```

Run it:

```bash
python agent.py
```

```
you> hello
echo> hello
you> what time is it?
echo> what time is it?
you> quit
Goodbye.
```

**What this teaches:**

- `while True` is the agent loop skeleton. It has no fixed number of iterations.
- `break` is the only exit. An agent stops when a *condition* is met, not after a fixed count.
- Each iteration can use information from the previous one (you will add that soon).

This loop alone handles multi-turn interaction. The only missing piece is intelligence inside the loop.

---

## 4. Replace the echo with a fake model

A real LLM API costs money and needs a key. For learning, a fake model is better — it is deterministic, fast, and reveals the structure without noise.

Add this above your loop:

```python
# agent.py  –  step 2: fake model

def fake_model(prompt: str) -> str:
    """Simulates a language model. Returns plain text."""
    text = prompt.lower().strip()

    if "hello" in text or "hi" in text:
        return "Hey! How can I help you today?"

    if "name" in text:
        return "I'm a minimal agent. I don't have a name yet."

    if "help" in text:
        return "I can answer questions and run simple tools. Try asking what time it is."

    return "Interesting question. I'll think on that."


while True:
    user_input = input("you> ").strip()

    if user_input in {"quit", "exit", "q"}:
        print("Goodbye.")
        break

    if not user_input:
        continue

    response = fake_model(user_input)
    print(f"agent> {response}")
```

Run it:

```
you> hello
agent> Hey! How can I help you today?
you> what is your name?
agent> I'm a minimal agent. I don't have a name yet.
you> quit
Goodbye.
```

**What this teaches:**

The model is *one function call* inside a larger program. The loop controls:

- *when* the model is called
- *what* is passed to it
- *what happens* with the response

The model does not control any of that. The **runtime** does. Keep that separation clear — it matters a lot in later chapters.

---

## 5. Why returning a plain string is not enough

The fake model above returns a `str`. That works for chitchat, but an agent model must also be able to say:

> "Do not answer yet. First run this action. Then use the result to answer."

A plain string cannot express two different kinds of intent at once. You need **structured responses**.

The minimal structure is a dictionary with a `"type"` field:

```python
# Two possible response shapes:

# Shape A — the model has an answer
{"type": "text", "content": "Here is my answer."}

# Shape B — the model wants an action first
{"type": "tool_call", "tool": "get_time", "args": {}}
```

This is the single most important design decision in agent systems. Every production framework (OpenAI function calling, Anthropic tool use, Gemini function declarations) uses exactly this pattern — a structured response where the model signals intent rather than producing raw text.

---

## 6. Upgrade the fake model to return structured responses

Rewrite `fake_model` to return a `dict` instead of a `str`:

```python
# agent.py  –  step 3: structured model responses

def fake_model(prompt: str) -> dict:
    """
    Returns one of two shapes:
      {"type": "text",      "content": str}
      {"type": "tool_call", "tool": str, "args": dict}
    """
    text = prompt.lower().strip()

    if "time" in text:
        return {"type": "tool_call", "tool": "get_time", "args": {}}

    if "hello" in text or "hi" in text:
        return {"type": "text", "content": "Hey! How can I help?"}

    if "name" in text:
        return {"type": "text", "content": "I'm a minimal agent."}

    return {"type": "text", "content": "I'm not sure how to help with that yet."}
```

Now the caller can inspect `response["type"]` and branch:

```python
response = fake_model(user_input)

if response["type"] == "text":
    print(f"agent> {response['content']}")

elif response["type"] == "tool_call":
    # We will handle this next
    print(f"[agent wants to run tool: {response['tool']}]")
```

---

## 7. Add your first tool

A **tool** is a plain Python function. The runtime looks up the tool name the model requested, calls it with the provided args, and gets back a result string.

```python
# agent.py  –  step 4: a real tool

import datetime


def get_time(args: dict) -> str:
    """Returns the current UTC time as a string."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC")
```

Register it in a **tool registry** — a simple dict that maps names to functions:

```python
TOOLS = {
    "get_time": get_time,
}
```

Now handle `"tool_call"` responses in the loop:

```python
response = fake_model(user_input)

if response["type"] == "text":
    print(f"agent> {response['content']}")

elif response["type"] == "tool_call":
    tool_name = response["tool"]
    tool_args = response["args"]

    if tool_name in TOOLS:
        tool_result = TOOLS[tool_name](tool_args)
        print(f"agent> [ran {tool_name}] → {tool_result}")
    else:
        print(f"agent> [unknown tool: {tool_name}]")
```

Run it:

```
you> what time is it?
agent> [ran get_time] → 2026-04-24 10:33:01 UTC
you> hello
agent> Hey! How can I help?
you> quit
Goodbye.
```

You now have a working agent with tool execution.

---

## 8. Put it all together — the complete script

Here is the full `agent.py` so far, clean and complete:

```python
# agent.py  –  complete step-4 version
import datetime


# ── Tools ────────────────────────────────────────────────────────────────────

def get_time(args: dict) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC")


TOOLS = {
    "get_time": get_time,
}


# ── Fake model ────────────────────────────────────────────────────────────────

def fake_model(prompt: str) -> dict:
    text = prompt.lower().strip()

    if "time" in text:
        return {"type": "tool_call", "tool": "get_time", "args": {}}

    if "hello" in text or "hi" in text:
        return {"type": "text", "content": "Hey! How can I help?"}

    if "name" in text:
        return {"type": "text", "content": "I'm a minimal agent."}

    return {"type": "text", "content": "I'm not sure how to help with that yet."}


# ── Agent loop ────────────────────────────────────────────────────────────────

def run_agent():
    print("Agent ready. Type 'quit' to exit.\n")

    while True:
        user_input = input("you> ").strip()

        if user_input in {"quit", "exit", "q"}:
            print("Goodbye.")
            break

        if not user_input:
            continue

        response = fake_model(user_input)

        if response["type"] == "text":
            print(f"agent> {response['content']}")

        elif response["type"] == "tool_call":
            tool_name = response["tool"]
            tool_args = response["args"]

            if tool_name in TOOLS:
                tool_result = TOOLS[tool_name](tool_args)
                print(f"agent> [ran {tool_name}] → {tool_result}")
            else:
                print(f"agent> [unknown tool: {tool_name}]")


if __name__ == "__main__":
    run_agent()
```

Run it:

```bash
python agent.py
```

---

## 9. The five structural parts every agent needs

Look at what you just built. It has five distinct parts. Every agent system — simple or complex — has all five:

```
┌─────────────────────────────────────────────────────────┐
│                        RUNTIME                          │
│                                                         │
│  1. User Input  ──▶  2. Model  ──▶  3. Branch          │
│                                        │                │
│                              ┌─────────┴────────┐      │
│                              ▼                  ▼      │
│                          4. Tool            Text out   │
│                              │                         │
│                              └──▶  back to model       │
│                                                         │
│  5. Agent Loop  (while True / break on done)            │
└─────────────────────────────────────────────────────────┘
```

| Part | In your code |
|---|---|
| User input | `input("you> ")` |
| Model | `fake_model(prompt)` |
| Branch on response type | `if response["type"] == ...` |
| Tool execution | `TOOLS[tool_name](tool_args)` |
| Control loop | `while True` / `break` |

Nothing in later chapters removes any of these five. They only become more capable, better structured, and safer.

---

## 10. Checkpoint: things to try before moving on

Make changes to the code to make sure you understand each part:

**Exercise A** — Add a second tool

```python
def flip_coin(args: dict) -> str:
    import random
    return "heads" if random.random() > 0.5 else "tails"

TOOLS["flip_coin"] = flip_coin
```

Then add logic to `fake_model` so asking about coins triggers it.

**Exercise B** — Make the model return an error shape

Add a third response type:

```python
{"type": "error", "message": "I cannot process that input."}
```

Handle it in the loop with a proper error message printed to the user.

**Exercise C** — Count turns

Add a `turn` counter that increments each loop iteration. Print it as part of the prompt:

```
[turn 1] you> hello
[turn 2] you> what time is it?
```

This is the seed of conversation state management, which Chapter 3 covers in depth.

---

## 11. What comes next

You have a working agent skeleton. Here is where each chapter takes it:

| Chapter | What it adds |
|---|---|
| 01 — Agent Loop | A proper `AgentLoop` class; real message history; streaming model responses |
| 02 — Tools | Tool schema definitions; argument validation; error handling inside tools |
| 03 — Session Manager | Persistent conversation state; saving/loading sessions |
| 04 — Hooks | Lifecycle callbacks: `on_tool_start`, `on_text`, `on_error` |
| 05 — Context Engineering | System prompts; context window management; injection strategies |
| 06 — Memory & Storage | Short-term and long-term memory; key-value and vector memory |
| 07 — Permissions | Restricting which tools an agent can call |
| 08 — Skills | Composable, reusable agent behaviours |
| 09 — Plan & Auto Mode | Having the agent plan steps before executing |
| 10 — Swarms & Delegation | Multiple agents collaborating |
| 11 — Agent Communication | Message passing between agents |
| 12 — Dangerous Actions | User confirmation before risky tool calls |
| 13 — Guardrails & Safety | Input/output filtering; rate limits; policy enforcement |

**The mental model to carry forward:**

> An agent is a loop where the model can either answer directly or ask the runtime to do something before continuing. Everything else is a refinement of that single idea.

---

Next: [01-agent-loop.md](01-agent-loop.md)

