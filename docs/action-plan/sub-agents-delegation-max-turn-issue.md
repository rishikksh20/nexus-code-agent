I’m tracing the control path that decides sub-agent fan-out and loop limits, then I’ll use that to give you concrete architectural changes rather than generic advice.

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/runtime)

Searched for regex `max_loop_iterations|planning_analysis|execution|subagent|resume_tool_calls|Single-query turn limit reached` (`**/nexus/**`), 200 results

Read memory

The hot path is already visible: the supervisor prompt strongly biases every repo inspection into a subagent, and the runtime treats each subagent call as another turn against the same per-query budget. I’m reading the subagent registration and prompt wiring next to pin down where the repetition is coming from.

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/runtime/agent.py#1828-1828), lines 1828 to 1975

Searched for files matching `**/nexus/**/subagents*.py`, 1 match

Searched for files matching `**/nexus/**/agent_tool*.py`, 1 match

Read memory [](file:///Users/rishikeshrishikesh/Library/Application%20Support/Code/User/workspaceStorage/ec5e43b987f5172b7ce2b7a9ce70a704/GitHub.copilot-chat/memory-tool/memories/repo/nexus-foundations.md)

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/prompts/system.py#175-175), lines 175 to 210

The current prompt contract is more aggressive than your use case needs: it effectively forces delegation even for a simple folder summary. I’m checking the subagent tool implementation and turn runner to see where we can collapse multi-step exploration into fewer model turns and where the Cohere formatting bug fits.

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/runtime/agent.py#600-600), lines 600 to 760

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/runtime/turn_runner.py#280-280), lines 280 to 450

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/tools/subagents.py#1-1), lines 1 to 320

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/sandbox/agent_tool.py#1-1), lines 1 to 360

The implementation confirms the second cause: each subagent is itself a mini agent loop with `max_turns=1` per inner batch, so even a small exploration can fragment into many supervisor-visible tool/result cycles. I’m looking at how subagent history is accumulated and how pending tool-call messages are replayed, because that is likely what is causing both duplicate reads and the Cohere 400.

Searched for regex `history.append\(|apply_events_to_messages\(|pending_message|tool_result_messages|final_response =` (`**/nexus/sandbox/agent_tool.py`), 23 results

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/sandbox/agent_tool.py#200-200), lines 200 to 420

Searched for regex `messages with non-empty 'tool_calls'|tool_calls.*content|content.*tool_calls|cohere` (`**/nexus/**`), 154 results

Searched for regex `def _history_safe_completed_events|def _model_response_for_pending_tool_calls|def _tool_calls_from_confirmation_model_response|apply_events_to_messages` (`**/nexus/runtime/**`), 8 results

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/runtime/turn_runner.py#555-555), lines 555 to 860

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/runtime/repl_state.py#239-239), lines 239 to 336

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/integrations/openai_compatible.py#45-45), lines 45 to 95

Read [](file:///Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/nexus/integrations/cohere.py#45-45), lines 45 to 115

I have enough to explain the behavior. I’m collecting exact reference points now so the recommendations map cleanly to the current implementation rather than hand-wavy patterns.

Searched for text `Default to delegation|Direct normal tool use is only for tiny|If both a normal tool and a sub-agent could handle the same substantive work|For implementation requests, prefer` (`**/nexus/prompts/system.py`), 4 results

Searched for regex `name="planning_analysis"|name="execution"|max_turns=12|max_turns=14|max_turns=10|max_turns=6` (`**/nexus/tools/subagents.py`), 6 results

Searched for regex `max_turns=1|history.append\(|pending_message.content|role="tool"` (`**/nexus/sandbox/agent_tool.py`), 12 results

Searched for regex `_model_response_for_pending_tool_calls|content=source_message.content|tool_calls=tuple\(tool_calls\)|Single-query turn limit reached` (`**/nexus/runtime/turn_runner.py`), 6 results

Searched for text `Supervisor direct-use escape hatch|Preferred for codebase exploration|Preferred for file edits` (`**/nexus/runtime/agent.py`), 3 results

Searched for text `allow.*summary|summarize|README|directory` (`**/nexus/**`), 40 results

The current behavior is mostly architectural, not model-quality. Your supervisor is being taught to delegate almost every repo inspection task to a subagent first in system.py and the tool schema reinforces the same preference in agent.py. For a request like “go to ./workspace/calculator and summarize it”, that is the wrong default. It pushes the model into planning_analysis, then execution, then more execution, even though the task should fit into one bounded read-only pass.

The repeated file reads are a second design issue. The built-in subagents are broad role personas with fairly high turn budgets in subagents.py, and each subagent runs as repeated one-turn inner batches in agent_tool.py. Because subagents are isolated and there is no strong evidence-cache contract between them, they start cold and reread the same files. So you get orchestration churn instead of one evidence-gathering pass.

What I would change:

1. Add a routing gate before delegation.
Simple read-only tasks should stay with the supervisor if they fit a small budget, for example one directory listing plus up to two file reads. A folder summary absolutely belongs in this class.

2. Replace role-first delegation with task-shape delegation.
Instead of defaulting to planning_analysis and execution, define three practical paths:
direct supervisor path for tiny read-only tasks,
one bounded explore/summarize subagent for medium read-only tasks,
plan → implement → verify only for actual change-making work.
You already hint at this pattern in init.py. That explore agent should be the default for repository summarization.

3. Make the explore agent answer-capable, not just evidence-capable.
For “summarize this directory”, one subagent should own the whole job: inspect the tree, read the README, read the entrypoint, stop, and return a final summary plus evidence. The supervisor should only do a final polish pass, not spawn more subagents.

4. Add hard evidence budgets and stop conditions to subagent instructions.
The current tasks are too open-ended: “analyze directory structure”, “continue analysis”, “read X”, “read Y”. That invites thrashing. Use contracts like:
target path,
max directories,
max files,
max bytes per file,
preferred files in order,
stop once README + entrypoint + first-level tree are covered,
return concise summary and unanswered questions.
That is the biggest single change for reducing turns.

5. Introduce per-query evidence caching and packet reuse.
If one agent already read calculator/README.md, the next agent should receive that content by packet id or cached evidence object instead of rereading it. The isolation model is fine, but the handoff needs to be explicit and automatic. Right now the runtime has packet concepts, but the workflow is not using them strongly enough to prevent cold restarts.

6. Add duplicate-call suppression at the runtime layer.
If the same agent asks for the same read_file or list_dir on the same path and same slice during one query, the runtime should either:
return the cached result,
or reject the call with a short reminder to use prior evidence.
Do not rely on the model alone to notice repetition.

7. Separate orchestration budget from answer budget.
A single user query should have different limits for:
supervisor turns,
subagent invocations,
tool calls,
total model calls.
Using one max_loop_iterations style budget for everything makes orchestration overhead consume the same budget as actual task progress.

A good target policy for your example query would be:

1. Supervisor classifies it as tiny read-only work.
2. Supervisor does one list_dir on calculator.
3. Supervisor reads README and main entrypoint only.
4. Supervisor answers.

If you still want delegation for that class of task, then:

1. Supervisor calls one subagent_explore.
2. Explore agent performs at most 3 to 5 reads with a clear stop rule.
3. Explore agent returns a user-ready summary.
4. Supervisor sends it with minor refinement only.

One adjacent issue is worth fixing when you implement this: the Cohere 400 is likely coming from replaying assistant messages that contain both tool_calls and text content during resume flow in turn_runner.py and in the subagent replay path in agent_tool.py. That is separate from the over-delegation problem, but it makes long query loops much more fragile.

If you want, I can next turn this into a concrete change plan with exact prompt changes, routing heuristics, and subagent definitions without writing any code yet.