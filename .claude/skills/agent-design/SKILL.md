# Agent Design Principles

## Architecture: Hub-and-spoke, never free-form
- Orchestrator (hub) decides which specialists to invoke and in what order.
- Specialists (spokes) never call each other directly.
- Specialists return results to the orchestrator, which composes the final output.
- Hard cap: orchestrator may invoke at most 5 specialist calls per task. Higher needs explicit human config.

## Model selection (cost-tuned)
- Orchestrator: claude-opus-4-7 (smart routing pays for itself)
- Customer-facing conversation: claude-sonnet-4-6 (warmth + cost balance)
- Content generation: claude-sonnet-4-6
- Classification, intent detection, lead scoring: claude-haiku-4-5
- Never use opus for high-volume cheap tasks; never use haiku for nuanced reasoning.

## Autonomy levels
Each agent has autonomy_level in {0, 1, 2}:
- 0 = draft only: agent generates output, human must approve before any external action
- 1 = propose: agent acts only on low-risk things, escalates anything affecting money or external messages
- 2 = autonomous: agent acts within bounded budgets and rule-defined limits, logs everything

Default: new agents start at level 0 for the first 7 days, then can be promoted by tenant admin.

## System prompt structure (every agent)
1. Role definition (one paragraph)
2. Tenant context (brand voice, business type, language preferences)
3. Available tools and when to use each
4. Strict output format (JSON or structured text)
5. Hard rules (what the agent must NEVER do)
6. Escalation triggers (when to hand off to human)
7. Few-shot examples (3-5, drawn from real successful interactions if available)

## Tool use over free generation
- Whenever the agent needs to look up data, generate a payment link, create an order, etc., use Anthropic tool use, not free-text generation.
- Tools have strict schemas. Tool results go back to the model for final synthesis.

## Confidence and escalation
- Every agent output includes a confidence score (0-1).
- If confidence < 0.7, escalate to human.
- If output contains complaint keywords, profanity, or legal/medical/financial advice requests, escalate regardless of confidence.

## Language handling
- Detect language of incoming message (Uzbek Latin, Uzbek Cyrillic, Russian, English).
- Reply in the same script and language as the customer's last message.
- For mixed-language messages, default to whichever language has more tokens.
- Brand voice doc may override default tone but never overrides language matching.

## Memory and context discipline
- Pass only the slice of conversation history relevant to the current task (last 10 messages, not entire history).
- RAG retrieval: top 5 docs max, filtered by relevance score > 0.4.
- Never include another tenant's data in any prompt, ever, for any reason.

## Logging
- Every agent invocation creates an audit_log entry.
- Every LLM call creates an llm_calls entry.
- Errors logged at WARNING with full context except secrets.