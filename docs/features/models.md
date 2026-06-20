# Model Configuration

Rocky supports multiple LLM providers and models. You can use short names for convenience or specify full `provider/model` strings.

## Model Registry

| Short Name | Provider | Model ID | Max Turns | Tier |
|---|---|---|---|---|
| `flash` | Google | gemini-2.5-flash | 10 | Economy |
| `haiku` | Anthropic | claude-haiku-4-5 | 10 | Economy |
| `standard` | Anthropic | claude-sonnet-4-6 | 20 | Standard |
| `sonnet` | Anthropic | claude-sonnet-4-6 | 20 | Standard |
| `gemini-pro` | Google | gemini-2.5-pro | 20 | Standard |
| `glm` | Synthetic | hf:zai-org/GLM-5.2 | 30 | Premium |
| `premium` | Google | gemini-3.1-pro-preview | 30 | Premium |
| `gemini-3.1-pro` | Google | gemini-3.1-pro-preview | 30 | Premium |
| `opus` | Anthropic | claude-opus-4-6 | 30 | Premium |

> `glm` is the **default** primary model, served through Synthetic's
> OpenAI-compatible API. `synthetic` is a custom provider registered in
> [`pi/models.json`](../../pi/models.json) — see [Custom Providers](#custom-providers).

## Choosing a Model

- **Economy** (`flash`, `haiku`) — Good for simple PRs (docs, deps, configs). Fast and cheap. Also used internally for FP verification.
- **Standard** (`standard`, `sonnet`, `gemini-pro`) — Handles most code reviews well. Best cost/quality balance.
- **Premium** (`glm`, `premium`, `gemini-3.1-pro`, `opus`) — Best for complex PRs with deep logic, security-sensitive code, or architectural changes. `glm` (GLM-5.2 via Synthetic) is the default, with `premium` (Gemini 3.1 Pro) as the default fallback.

## Configuration

```bash
# Use a short name (default)
AGENT_MODEL=glm

# Or a full provider/model string
AGENT_MODEL=anthropic/claude-sonnet-4-6

# Another premium model
AGENT_MODEL=google/gemini-3.1-pro-preview
```

## Automatic Fallback

If the primary model fails (rate limit, timeout, availability), Rocky automatically retries with a fallback model:

```bash
# Default: GLM (Synthetic) primary, Gemini 3.1 Pro fallback
AGENT_FALLBACK_MODEL=google/gemini-3.1-pro-preview
```

The fallback uses a different provider to maximize availability. Set to empty to disable fallback.

When fallback is used, the review metadata includes:
- `fallback_used: true`
- `primary_model` — which model failed
- `primary_error` — why it failed

## API Keys

Each provider needs its own API key:

| Provider | Environment Variable |
|---|---|
| Synthetic | `SYNTHETIC_API_KEY` |
| Google | `GEMINI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |

The `synthetic` provider authenticates with a standard `Authorization: Bearer`
header built from `SYNTHETIC_API_KEY`. The key is referenced only as
`${SYNTHETIC_API_KEY}` in `pi/models.json`; the value lives in the environment
(`.env` / runtime env), never in committed files.

## Custom Providers

Built-in providers (Anthropic, Google, OpenAI) work out of the box. Additional
OpenAI-compatible providers — such as `synthetic` (Synthetic.new, used for GLM
models) — are registered for `pi` in [`pi/models.json`](../../pi/models.json),
which `pi` loads from `~/.pi/agent/models.json`:

- **Docker:** the image copies `pi/models.json` to `/home/baloo/.pi/agent/models.json` (see `Dockerfile`).
- **Local dev:** copy it once into place:

  ```bash
  mkdir -p ~/.pi/agent
  cp pi/models.json ~/.pi/agent/models.json
  ```

Built-in and custom providers are merged, so fallback to Gemini/Anthropic keeps
working alongside the custom `synthetic` provider. API keys are interpolated
from environment variables (`${SYNTHETIC_API_KEY}`) at runtime, so no secret is
stored in the file.

## Thinking Level

Controls the depth of reasoning the model uses:

```bash
PI_THINKING_LEVEL=medium  # off, minimal, low, medium, high
```

Higher thinking = better analysis but slower and more expensive. `medium` is the default and recommended for most use cases.

## Cost Estimates

Approximate cost per review (typical 5-file PR):

| Model | Cost per Review |
|---|---|
| `flash` | ~$0.005 |
| `haiku` | ~$0.01 |
| `sonnet` | ~$0.03–0.08 |
| `gemini-pro` | ~$0.05–0.15 |
| `opus` | ~$0.15–0.40 |
| `glm` | Depends on your Synthetic plan |

Actual costs depend on PR size, number of agent turns, and thinking level.
