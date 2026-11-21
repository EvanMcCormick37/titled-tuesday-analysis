# minirouter quickstart

For no-deposit requests, see https://minirouter.sh/docs/free-inference.
The example below uses a paid model.

Base URL: https://api.minirouter.sh/v1
(The bare host https://api.minirouter.sh also works. Never append /chat/completions to the base URL. The client adds that path.)

Auth: Authorization: Bearer mr-live-<26 base58>
Get a key at https://minirouter.sh/key. No account is required, and keys are created before payment.
Prefer a recoverable key? Sign in with an email or wallet instead: https://minirouter.sh/start

For the accountless funding walkthrough, see https://minirouter.sh/pay/without-credit-card.
Save your API key and permanent dashboard link before payment. Choose only a
method and exact network offered at checkout, then wait for credited status.
If a transfer is unresolved, use transaction lookup; do not send it again.

## First request

    curl https://api.minirouter.sh/v1/chat/completions \
      -H "Authorization: Bearer $MINIROUTER_KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "model": "meta/llama-3.3-70b",
        "messages": [{"role":"user","content":"ping"}],
        "stream": true
      }'

Every response carries x-minirouter-provider, x-minirouter-ttft-ms and
x-minirouter-balance-usd headers. Non-streaming responses add
x-minirouter-cost-usd and usage.cost in the body; a stream carries the charge
as usage.cost in its final usage chunk (send stream_options.include_usage) and
its balance header is the balance at the start of the request.

## Per client

https://minirouter.sh/setup contains literal configuration references for common clients.
https://minirouter.sh/docs/guides/agent-onboarding sets up an agent with its own key and spend limits.
https://minirouter.sh/integrations lists guides for Cline, Kilo Code, OpenCode, Continue, LiteLLM, SillyTavern, Aider, Zed and other custom-provider clients.

## When it fails

Every error string the API emits contains a URL into https://minirouter.sh/docs/errors#<code>.
# Authentication

One header:

    Authorization: Bearer mr-live-<26 base58>

Keys are created before any payment. Two equal entry paths
(https://minirouter.sh/start): accountless at https://minirouter.sh/key, or an
account signed in with an email or Solana wallet (passwordless). Same prices,
same key format, same API behaviour either way. Save your key when it is shown;
you cannot view it again after creation or rotation.

## Accountless mode

No email or wallet identity is required. Setup gives you an API key for clients
and a permanent dashboard link for browser access. Keep both private. Both
must be saved before a payment address is shown. The link survives API-key
rotation and expiry. Expiring API keys do not expire dashboard links or browser
sessions. If every usable key and the link are lost, there is no personal recovery.

## Account mode

Sign in with an email or a Solana wallet, passwordless either way. You get
personal account recovery. Plaintext keys still cannot be recovered because
only their hashes are stored, but a saved dashboard link or signed-in session
can create a replacement. Browser-push and email credit alerts are optional.

## Claim an account later

POST /me/claim (authenticated with the key) attaches an email or wallet to an
accountless account, using the key as proof of control:

    curl https://api.minirouter.sh/me/claim \
      -H "Authorization: Bearer $MINIROUTER_KEY" \
      -H "Content-Type: application/json" \
      -d '{"email":"you@example.com"}'

## Rotation: 24-hour grace

POST /keys/:id/rotate returns a replacement with the same expiry deadline.
The old key accepts new requests until the earlier of that deadline or the
24-hour rotation grace deadline. Expiry alone does not stop requests already
admitted; existing revocation checks still apply.
401s reference
https://minirouter.sh/docs/errors#invalid_api_key

## Optional expiry

Keys never expire by default. At creation, choose 7, 30, or 90 days, or an exact
future date and time. POST /keys and POST /accounts/anonymous accept a nullable
expiresAt ISO 8601 timestamp with a timezone. The dashboard shows the exact
deadline before creation and alongside the key afterward.

Expiry is permanent: PATCH /keys/:id cannot set, change, or clear expiresAt.
Revoke and create another key to choose a different deadline. At the deadline,
new authenticated requests and inference admissions are refused. Requests
already admitted may finish and settle normally. History and balances remain.

Operational email notices are scheduled seven days before, one day before,
and at expiry. Milestones already passed at creation are skipped. Notices use
the personal account's verified email, or the team owner's verified email,
independently of marketing preferences. Keys may expire without a verified
email; those accounts cannot receive reminders. Early revocation cancels
pending notices. Email availability never controls expiry enforcement.

## Managing keys

Rename keys without replacing credentials. Names are optional, need not be
unique, and allow up to 120 characters. Members manage their own team keys;
owners retain team-wide access.

The dashboard hides revoked keys by default and keeps expired keys visible.
Combine status, usage, spending-cap, member, and name/key search filters.
Sort by name, status, last use, or 30-day key spend. Status order is Active,
Rotation grace, Expired, Revoked; never-used keys stay last when sorting by
last use. Filter and sort choices reset on a new visit or account switch.

## Per-key controls

- rpmLimit: requests per minute
- tpmLimit: estimated input and output tokens per minute
- dailyLimitNano: daily spend cap in nano-USD
- monthlyLimitNano: monthly spend cap in nano-USD
- allowedModels: models this key may call

Spend caps are on by default, and lower by default for accountless keys.
Keys and limits for an unattended agent: https://minirouter.sh/docs/guides/agent-onboarding

# Tools and plugins

Search Google, read a page directly, or give a model tools to use.

## Choose who runs the tool

- [Standalone Google Search](https://minirouter.sh/docs/google-search#standalone-search) — get titles, URLs and snippets directly.
- [Standalone Read URL](#standalone-read-url) — retrieve page text directly, without a model request.
- [Server tools](#try-a-server-tool) — MiniRouter gets the current time, searches the web or reads a public web page, then returns the model's answer in one request.
- [Your own functions](#run-your-own-functions) — your app runs code, such as a database query, and sends the result back to the model.
- [Plugins](#plugins) — MiniRouter shortens a long prompt or repairs JSON. The model does not call a plugin.

Model-driven server tools and plugins use **Chat Completions** (`POST /v1/chat/completions`) only.
Standalone tools use their own endpoints.
Model-driven tools require a model that supports function calling; check `tools` in its
`supported_parameters` in the [catalog](https://minirouter.sh/api/v1/models).

## Standalone Read URL

[Open the playground](https://minirouter.sh/tools), or call `POST /v1/tools/web-fetch` with your MiniRouter API key. Check `GET /v1/tools` for current availability, price and limits before submitting. For search results, use [standalone Google Search](https://minirouter.sh/docs/google-search#standalone-search).

```sh
idempotency_key="$(uuidgen)"
curl https://api.minirouter.sh/v1/tools/web-fetch \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $idempotency_key" \
  -d '{"url":"https://minirouter.sh/docs","max_content_tokens":10000}'
```

The request accepts a public HTTP or HTTPS URL and optional `max_content_tokens`.
The content limit must be between 100 and 10,000 tokens. Keys with model or provider restrictions cannot use this endpoint.
It returns readable text, without JavaScript rendering, PDF parsing, authenticated browsing or crawling.
The capability response owns the current limits; the initial maximum is 10,000 content tokens, a 4,096-character URL and a 15-second read deadline.

A completed response includes `id`, `status`, `data`, `result_available`, `usage` and `cost`.
Page data includes `url`, optional `title`, `content`, `retrieved_at` and `truncated`.
Read `cost.amount` in USD for the settled charge. The initial paid price is $0.001 per successful read, using your existing balance and spending limits. Failed reads have no tool charge.

The public playground allows three free reads per UTC day on a network. Shared networks share this allowance; the capability response reports remaining reads and reset time. Sign in to continue with your account balance. The developer API requires an API key.

### Receipts and retries

Send a fresh UUIDv4 `Idempotency-Key` for each new read. Reuse it only for the identical request if delivery is uncertain. A duplicate cannot create another charge; its response is a receipt with `data: null` and `result_available: false`.

After a timeout or disconnect, look up `GET /v1/tools/operations?client_request_id=YOUR_UUID` or `GET /v1/tools/operations/OPERATION_ID` with the same account credential. A `running` receipt is not a failed or free operation. Wait for a terminal receipt before starting another paid read. A missing receipt does not prove that a request failed. An explicit retry with the original UUID and identical input can safely finish a request that never reached admission. After a playground refresh, re-enter the URL and options; the saved UUID is reused. Different input under an existing UUID is rejected.

Page content is returned once and is not stored for replay. Receipts and idempotency records remain available for 30 days, without raw URLs or page content. The playground stores only the request UUID, tool kind and account context for recovery; refreshing the page loses the returned content. Reading again is a new operation at the current price.

Invalid URLs, private networks, unsupported content, rate limits and insufficient balance return bounded errors. Public and authenticated access use separate allowances; signing in does not spend the public trial.

## Try a server tool

Create an [API key](https://minirouter.sh/key), save its recovery link, and
[add credits](https://minirouter.sh/dashboard/billing). Replace the placeholder
below and run it in your terminal before running the examples on this page.

```sh
export MINIROUTER_KEY='paste-your-api-key-here'
```

Examples use paid models. For free requests, follow the
[Auto Free guide](https://minirouter.sh/docs/free-inference).

Run this in the same terminal. It asks Qwen 3.5 Flash to get the current year
with `minirouter:datetime`.

```sh
curl https://api.minirouter.sh/v1/chat/completions \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "alibaba/qwen3.5-flash",
  "messages": [
    {
      "role": "user",
      "content": "Use the datetime tool to find the current year, then reply with only that four-digit year."
    }
  ],
  "max_tokens": 128,
  "reasoning": {
    "effort": "none"
  },
  "tools": [
    {
      "type": "minirouter:datetime"
    }
  ],
  "max_tool_calls": 2
}'
```

Read the year in `choices[0].message.content`. You do not need to write a
datetime function or send a tool result yourself.

## How the tool loop works

1. **You send a prompt and a tool.** The `tools` array tells the model what it can use.
2. **The model requests a tool call.** MiniRouter runs the built-in tool and sends its result back to the model.
3. **The model answers.** MiniRouter returns that answer in the same HTTP response. It can repeat step 2 within your `max_tool_calls` limit.

Listing a tool makes it available; the model may answer without using it.
Ask explicitly when the task needs a tool, as in the example above. In a
non-streaming response, `x-minirouter-server-tool-rounds` lists the model
request IDs. Two or more rounds show that the server tool loop continued.

You can mix built-in tools with your own function definitions. When the model
requests one of your functions, MiniRouter returns that call for your app to
handle using the [function-calling flow](#run-your-own-functions).

## Available server tools

| Tool | Use it for | Options |
| --- | --- | --- |
| `minirouter:datetime` | Current date and time, no tool fee | `parameters.timezone`: an IANA name such as `Europe/London`. Default: `UTC`. |
| `minirouter:web_fetch` | Readable text from a public URL, $0.001 per successful fetch | Limit page size, domains and fetch count with `parameters`. Example below. |
| `minirouter:google_search` | Google results, $0.01 per completed search | Set search count, result count and region. [Google search guide](https://minirouter.sh/docs/google-search). |
| `minirouter:web_search` | Current results from the web, $0.01 per search | Filter sources, freshness and language; limit search context. [Search options](#search-the-web). |
| `minirouter:news_search` | News articles with publisher and date, about $0.012 per search | Filter by recency, date range and source. [News options](#search-the-news). |

To set the timezone, replace the example's `tools` field with:

```json
{
  "tools": [{
    "type": "minirouter:datetime",
    "parameters": { "timezone": "Europe/London" }
  }]
}
```

**Read a web page.** This request allows one fetch from `example.com`:

```sh
curl https://api.minirouter.sh/v1/chat/completions \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "alibaba/qwen3.5-flash",
  "messages": [
    {
      "role": "user",
      "content": "Use web_fetch to read https://example.com and summarize the page in one sentence."
    }
  ],
  "max_tokens": 256,
  "reasoning": {
    "effort": "none"
  },
  "tools": [
    {
      "type": "minirouter:web_fetch",
      "parameters": {
        "allowed_domains": [
          "example.com"
        ],
        "max_uses": 1,
        "max_content_tokens": 2000
      }
    }
  ],
  "max_tool_calls": 2
}'
```

Each successful fetch adds a **$0.001 platform fee**, plus model tokens.
Failed, blocked and timed-out fetches have no fetch fee. Redirects count as one fetch.
`usage.server_tool_use` reports the successful, billed count in `web_fetch_requests`.
Datetime has no tool fee.

Web fetch requires a supported paid route with authoritative billing. Free models,
provider keys (BYOK), and routes without authoritative billing reject it.

Web fetch reads a URL; it does not search the web or sign in to a website.
Private-network addresses are blocked. The page text is sent to the model as
tool output, so treat it as untrusted input.

| Web fetch option | Meaning |
| --- | --- |
| `max_uses` | Fetch attempts per API request, from 1 to 50. The overall `max_tool_calls` limit also applies. |
| `max_content_tokens` | Approximate limit on returned page text, from 100 to 100,000. Default: 10,000. |
| `allowed_domains` | Only allow these domains. Use hostnames, such as `example.com`, without a URL scheme or path. |
| `blocked_domains` | Block these domains, even if they also match the allowlist. |

## Search the web

Search Formula 1's site for results published in the past month:

```sh
curl https://api.minirouter.sh/v1/chat/completions \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "zai/glm-5.3-flash",
  "messages": [
    {
      "role": "user",
      "content": "Search the web for the latest Formula 1 Grand Prix result. Give the winner and source URL."
    }
  ],
  "tools": [
    {
      "type": "minirouter:web_search",
      "parameters": {
        "max_uses": 1,
        "max_results": 3,
        "search_domain_filter": [
          "formula1.com"
        ],
        "search_recency_filter": "month",
        "country": "GB",
        "search_language_filter": [
          "en"
        ],
        "max_tokens": 4096,
        "max_tokens_per_page": 1024
      }
    }
  ]
}'
```

Searches cost $0.01 each, plus model tokens. `usage.server_tool_use` reports
the billed count in `web_search_requests`, capped at `max_uses`.
The model can exceed this allowance; extra searches cost about $0.005 each.

Web search is powered by Perplexity. It currently requires a model served through
Vercel; other routes return `400`.

Set these options inside the search tool's `parameters`. All are optional;
numeric values must be integers.

| Search option | Meaning |
| --- | --- |
| `max_uses` | Billed allowance per API request: 1–10 searches at $0.01 each. Default: 3. The model can exceed it. |
| `max_results` | Results per search, from 1 to 20. Omit to let the model choose. |
| `search_domain_filter` | Up to 20 hostnames, without a scheme or path. Include with `["formula1.com"]` or exclude with `["-reddit.com"]`. Do not mix inclusion and exclusion. |
| `search_recency_filter` | Publication recency: `day`, `week`, `month` or `year`. |
| `country` | Regional results. Use an uppercase, two-letter ISO country code, such as `GB` or `US`. |
| `search_language_filter` | Up to 10 lowercase, two-letter ISO language codes, such as `["en", "fr"]`. |
| `max_tokens` | Total search-result tokens per search, from 1 to 1,000,000. Default: 25,000. |
| `max_tokens_per_page` | Search-result tokens per page, from 256 to 2,048. Default: 2,048. |

The tool's `parameters.max_tokens` bounds retrieved search context.
The request's top-level `max_tokens` limits the model's output per round.

## Search the news

Find this week's Federal Reserve coverage:

```sh
curl https://api.minirouter.sh/v1/chat/completions \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "zai/glm-5.3-flash",
  "messages": [
    {
      "role": "user",
      "content": "Find this week's news about the Federal Reserve. List each headline with its publisher, date and URL."
    }
  ],
  "tools": [
    {
      "type": "minirouter:news_search",
      "parameters": {
        "max_uses": 1,
        "max_results": 5,
        "search_recency_filter": "week"
      }
    }
  ]
}'
```

News search is powered by Exa through Vercel and needs a Vercel-served model.
Each search costs $0.01 plus Exa's extra $0.002 at your usual markup, plus
model tokens. `usage.server_tool_use` reports the billed count in
`news_search_requests`, capped at `max_uses`. The model can exceed this allowance;
extra searches cost about $0.007 each. `web_search` and `news_search`
share a limit of 10 searches per request.

| News option | Meaning |
| --- | --- |
| `max_uses` | Billed allowance per API request: 1–10 searches. Default: 3. |
| `max_results` | Articles per search, from 1 to 10. Default: 5. |
| `search_recency_filter` | Published within the last `day`, `week`, `month` or `year`. |
| `start_published_date` | Published on or after this ISO 8601 date, such as `2026-10-01`. |
| `end_published_date` | Published on or before this ISO 8601 date. |
| `search_domain_filter` | Up to 20 hostnames. Include with `["reuters.com"]` or exclude with `["-example.com"]`. |

Use either `search_recency_filter` or a date range, not both.

The model sends a `query` and receives up to `max_results` articles:

```json
{
  "results": [{
    "title": "Fed holds rates steady",
    "url": "https://www.reuters.com/markets/...",
    "publishedDate": "2026-10-01T00:00:00.000Z",
    "author": "Reuters staff",
    "text": "The first 2,000 characters of the article..."
  }]
}
```

`publishedDate` and `author` are included when the source provides them;
the publisher is the URL's site. No matches return an empty `results` list.
A failed or timed-out search returns `{"error": "timeout", "message": "..."}`
to the model, which can retry or answer without it. Search results stay
with the model; your response contains its answer, not the raw articles.

## Streaming, limits and cost

- **Limit datetime and web-fetch calls:** `max_tool_calls` accepts 1–30, defaults to 5, and requires at least one server tool. After reaching the limit, MiniRouter asks the model to finish without further tool calls. This does not cap web or news searches.
- **Reserve for fetches:** each round holds enough credit for the remaining fetch allowance, bounded by `max_uses` and `max_tool_calls`; only successful fetches are charged.
- **Allow for multiple model rounds:** the model reads the prompt, then reads the tool results. Each round is billed separately; `max_tokens` limits each round, not the whole loop.
- **Read the total:** non-streaming `usage` and `x-minirouter-cost-usd` include all model rounds. For streaming totals, send `stream_options: {"include_usage":true}` and read the final usage chunk.
- **Keep the connection open:** use `stream: true` and `curl -N` for longer loops. MiniRouter sends keep-alives while tools run, then sends the completed answer as SSE events. Intermediate tool rounds are not streamed token by token. Handle error events as well as the final `[DONE]`.

For example, add these fields to the datetime request:

```json
{
  "stream": true,
  "stream_options": { "include_usage": true }
}
```

## Run your own functions

Use a function tool when the work belongs in your app, such as querying an
order or calling a weather API. A function definition describes the input; it
does not upload or run your code.

```sh
curl https://api.minirouter.sh/v1/chat/completions \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "alibaba/qwen3.5-flash",
  "reasoning": {
    "effort": "none"
  },
  "messages": [
    {
      "role": "user",
      "content": "What is the weather in London?"
    }
  ],
  "max_tokens": 256,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {
              "type": "string"
            }
          },
          "required": [
            "city"
          ],
          "additionalProperties": false
        }
      }
    }
  ],
  "tool_choice": "auto"
}'
```

Then complete the conversation in your application:

1. Read `choices[0].message.tool_calls`. With `tool_choice: "auto"`, the model may answer directly instead.
2. Parse each function's `arguments` JSON, validate it, check permissions, and run your function.
3. Append the complete assistant message, including its `tool_calls`, to your original `messages` array. Keep any `extra_content` a tool call carries, such as Gemini's `extra_content.google.thought_signature`: the model needs it to continue its reasoning on the next turn.
4. Append a `role: "tool"` message for each result. Match its `tool_call_id` to the actual call ID and encode `content` as a string.
5. Send the updated conversation to the same endpoint and model to get the answer. Repeat if the model requests another call.

An illustrative tool result message (replace the ID and content with your result):

```json
{
  "role": "tool",
  "tool_call_id": "call_id_from_response",
  "content": "18 C, cloudy"
}
```

On models that support tool choice, `tool_choice: "required"` forces a call
and `"none"` disables calls. `parallel_tool_calls` controls parallel
function calls where supported. Your app controls its own function-call limit;
`max_tool_calls` is for MiniRouter's built-in tools.

## Plugins

Plugins are optional processing steps, enabled through `plugins` rather than
`tools`. They do not add functions for the model to call. You can include both
plugin IDs in one request; omit a plugin or set `"enabled": false` to disable it.

## Fit a long prompt

Add this field to a Chat Completions request with an explicit model ID:

```json
{
  "plugins": [{ "id": "context-compression" }]
}
```

If the prompt is too long for the model's context window after allowing for
output, MiniRouter removes conversation turns from the middle, then shortens
long messages if needed. It preserves the system-message prefix and the first
and latest turns from removal, but message content may still be shortened.

This can discard details; it does not create a summary. Keep essential facts
in a short system message or the latest prompt, and keep your own complete
history. A prompt that already fits is unchanged. Use a named model, not
`minirouter/auto`, so MiniRouter can look up its context window.

## Repair JSON output

Add these fields to a non-streaming request and ask for JSON in your prompt:

```json
{
  "response_format": { "type": "json_object" },
  "plugins": [{ "id": "response-healing" }],
  "stream": false
}
```

The plugin can remove code fences and surrounding text, and close unfinished
brackets in `choices[0].message.content`. It also accepts a `json_schema`
response format; see [structured output](https://minirouter.sh/docs/structured-output)
for model support.

Valid JSON and output cut off by the token limit are returned unchanged.
Repair is best-effort, does not guarantee schema compliance, and does not run
with streaming. Check `finish_reason`, parse the JSON and validate it in your app.

# Decision API

Evaluate shared state against typed questions and receive probabilities, choices and scores.

## Availability

A decision model is available when it appears in the [decision catalog](https://minirouter.sh/models?evaluationOnly=true) and your API key permits it. If no supported route is available, requests return HTTP 503 before a credit hold or provider call. Authenticated native discovery lists the models your key may use.

## Endpoints

| Method | Path                   | Format                                                                     |
| ------ | ---------------------- | -------------------------------------------------------------------------- |
| POST   | /v1/decisions          | Ordered predicate, choice and score questions; model-specific availability |
| POST   | /v1/evaluate           | Boolean, choice and score questions                                        |
| POST   | /typesafe/v1/systemone | Native TypeSafe noul, choice and score questions                           |
| GET    | /typesafe/v1/models    | Authenticated model discovery                                              |

Choose a model from the catalog and use its documented endpoints and limits. The examples below use Jev. Use a MiniRouter integration API key as a bearer token and set `model` to the selected catalog ID. Chat endpoints, native aliases and omitted model defaults are unsupported.

## First request

```sh
curl --fail-with-body https://api.minirouter.sh/v1/evaluate \
  -H "Authorization: Bearer $MINIROUTER_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "model": "typesafe-ai/jev",
    "state": "The subscription was charged twice.",
    "questions": {
      "billing": {
        "type": "boolean",
        "instructions": "Does this describe a billing problem?"
      }
    }
  }'
```

A successful primary response has this shape:

```json
{
  "model": "typesafe-ai/jev",
  "answers": {
    "billing": { "type": "boolean", "probability": 0.98 }
  },
  "usage": { "inputTokens": 283, "outputTokens": 21 }
}
```

These are illustrative values, not a live result.

## Question types

- **Boolean:** use `type: "boolean"` and instructions. Optional criteria describe the true and false cases. The answer contains a probability from 0 to 1.
- **Choice:** use `type: "choice"` with a criteria object mapping option names to descriptions. The answer contains the selected choice and each option's probability.
- **Score:** use `type: "score"` with at least two ordered descriptions in a criteria array. The answer contains an interpolated score and each level's probability.

State and instructions accept strings, objects or arrays. Paid models accept up to **64 questions** per request. Free Laya models accept **one question per request**. Each evaluation question allows **64 options per choice question** or **2–10 levels per score question**.

The normalized JSON sent to the provider must fit the selected model's byte limit, including routing fields added by MiniRouter. Check the model page and leave room for those fields. A byte limit is not a token estimate. The provider's context limit also applies to the input and questions.

## Native decision format

Use `/v1/decisions` only when the selected model page lists that endpoint. Endpoint support and question limits vary by model.

Send an explicit `model`, `input` and an ordered `questions` array. Input accepts text or up to 64 messages with `role: "user"` and text content. Questions accept an optional unique `name` and required text `instructions`:

- **Predicate:** `{ "type": "predicate", "instructions": "Does this describe a billing problem?" }`.
- **Choice:** supply `choices` with up to 255 entries (64 for Laya models) containing a string or boolean `value` and optional text `description`.
- **Score:** supply `levels` with 2–10 entries containing a text `label` and optional text `description`.

Answers preserve question order and names. Predicate answers include `probability`; choice answers include `choice`; score answers include `score`. Choice and score answers may also include `confidence` and a `probabilities` array. Do not assume those optional fields are present. Usage uses `input_tokens`, `output_tokens` and `total_tokens`.

Models with refusal support can return `{ "type": "refusal", "name": null }`. A refusal is a completed, billable result on paid routes. Preserve its position; do not automatically retry it.

Decision routes currently accept text input. Images, remote URLs, file IDs, audio and streaming are unsupported.

## Native TypeSafe format

Jev models also support the native TypeSafe format. Send requests to `/typesafe/v1/systemone` on `https://api.minirouter.sh`. Use `noul` in place of boolean; its answer uses `{ "type": "noul", "noul": 0.98 }`. Native usage fields are `input_tokens` and `output_tokens`. Choice and score answers include confidence, and score also includes the requested level legend.

Native errors use `{ "message": "…", "error_type": "…" }`. Invalid request shapes return 422; malformed JSON returns 400. Missing or invalid MiniRouter keys return 401. Native model discovery requires authentication and lists only enabled models your key may use.

## Pinned SDK examples

These examples use JavaScript SDK **0.6.0** and synchronous Python SDK **0.7.0** with the native TypeSafe endpoint. Use the request fields and question formats documented above.

Install `@typesafe-ai/sdk@0.6.0`, set `MINIROUTER_API_KEY`, and run this JavaScript as an ES module:

```js
import { TypeSafeClient, noul } from "@typesafe-ai/sdk";

const client = new TypeSafeClient({
  apiKey: process.env.MINIROUTER_API_KEY,
  baseURL: "https://api.minirouter.sh/typesafe",
  retry: { maxRetries: 0 },
});
const result = await client.systemOne({
  model: "typesafe-ai/jev",
  state: "The subscription was charged twice.",
  questions: { billing: noul("Does this describe a billing problem?") },
});
console.log(result);
```

For Python, install `typesafe-sdk==0.7.0` and set the same environment variable:

```python
import os
from typesafe_sdk import TypeSafeClient, RetryPolicy, Noul

with TypeSafeClient(
    api_key=os.environ['MINIROUTER_API_KEY'],
    base_url='https://api.minirouter.sh/typesafe',
    retry=RetryPolicy(max_retries=0),
) as client:
    result = client.system_one(
        model='typesafe-ai/jev',
        state='The subscription was charged twice.',
        questions={'billing': Noul(instructions='Does this describe a billing problem?')},
    )
    print(result)
```

Keep the explicit model and zero-retry setting; do not override retries on individual requests. `X-Should-Retry: false` does not disable SDK retries. Two explicit calls remain two separate requests and may produce two charges.

Include question instructions and keep state and questions within the request limits. Native aliases, omitted instructions, structured criterion descriptions and extra request fields are unsupported. The Python example uses the synchronous client. Pointing an AI SDK Gateway client directly at MiniRouter is unsupported.

## Pricing and settlement

The final charge is verified provider inference cost plus **10%**, rounded once to nano-USD. Read the standard input, output and request rates in the [model catalog](https://minirouter.sh/models?evaluationOnly=true). Catalog prices already include the markup. A verified zero-cost request has a zero customer charge. Pending or missing cost does not mean free. Provider promotions may change; no free period is guaranteed.

Paid requests reserve a temporary credit hold from the model’s input bound and the number of questions submitted. Jev uses a shared request-level bound instead of multiplying the bound by question count. The hold can substantially exceed the final charge for a small request; it is not a minimum bill. The unused amount is released after settlement. If the hold expires before provider cost is known, reconciliation continues. A later verified exact-zero receipt finalizes the request without a customer charge. The response header `X-Minirouter-Billing-State: pending` means final provider cost is still being reconciled. Read the settled charge in account activity. Output usage remains counted even when output is free.

## Free decision models

A model explicitly offered as free uses shared free-inference quotas and concurrency limits. Laya and Laya Free accept one question per request, with up to 64 choices. No deposit or monetary hold is required, and the customer charge stays zero even if the upstream later reports a cost. Changing endpoints does not reset the allowance. The model’s token ceiling includes input and any reported output usage; see the selected model's limits. A missing provider-cost receipt remains pending reconciliation.

Free availability depends on current route and zero-price evidence. An unavailable free route does not fall back to a paid model.

## Retries and unsupported fields

Disable automatic client retries. Each new incoming request has a separate billing identity. A timeout or lost response can still incur provider cost. Each accepted request makes at most one upstream attempt; `X-Should-Retry: false` is advisory and does not disable a library's retry policy.

Streaming, chat parameters, fallback models, caller providerOptions, duplicate JSON keys and unknown request fields are rejected. An invalid provider response can still incur a charge. MiniRouter does not automatically retry it.

## References

- [Vercel evaluation API](https://vercel.com/docs/ai-gateway/modalities/decision)
- [TypeSafe API reference](https://docs.typesafe.ai/api)
- [MiniRouter authentication](https://minirouter.sh/docs/authentication)
- [MiniRouter billing](https://minirouter.sh/docs/billing)

# Google search

Search Google directly, or give your model current sources.

## Search without a model

[Open Google Search](https://minirouter.sh/dashboard/tools?tool=google_search), or call `POST /v1/tools/google-search` with a funded integration API key. No model request is needed. The playground requires sign-in; Google searches have no free trial.

```sh
idempotency_key="$(uuidgen)"

curl 'https://api.minirouter.sh/v1/tools/google-search' \
  --request POST \
  --header 'Authorization: Bearer YOUR_MINIROUTER_KEY' \
  --header 'Content-Type: application/json' \
  --header "Idempotency-Key: $idempotency_key" \
  --data '{
  "query": "MiniRouter API documentation",
  "region": "GB",
  "max_results": 5
}'
```

| Field | Behavior |
| --- | --- |
| `query` | Required search text: 1–2,048 characters. |
| `max_results` | One to ten results. Default: five. Google may return fewer. |
| `region` | Optional uppercase country code, such as `GB` or `US`. |

1. **Query**: Send search text and optional country.
2. **Results**: Read titles, URLs and snippets from Google.
3. **Receipt**: Check the completed count and settled charge.

Check `GET /v1/tools` → `google_search` for availability, price and limits. Read results in `data.results`, the query in `data.query`, and the completion time in `data.retrieved_at`. `usage.google_search_requests` counts completed searches; `cost.amount` is the settled USD charge. Each completed search costs $0.01, including searches with no results. No model tokens are charged.

Send a fresh UUIDv4 `Idempotency-Key` for each new search. After an interruption, check `GET /v1/tools/operations?client_request_id=YOUR_UUID` with the same account credential. A `running` or missing receipt does not confirm failure. Retry only with the original key and identical input; do not reuse a key across tools.

Replays return the receipt with `data: null` and `result_available: false`. Queries and results are not saved for recovery. Receipts remain available for 30 days. Starting another search creates a new charge. Keys with model or provider restrictions cannot use standalone tools.

## Search with a model

Set `MINIROUTER_KEY` to your funded [integration API key](https://minirouter.sh/key). Use Chat Completions and a paid, tool-capable model. Free models, chat-session keys and provider keys (BYOK) are unsupported.

```sh
curl https://api.minirouter.sh/v1/chat/completions \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "google/gemma-4-26b-a4b-it",
  "messages": [
    {
      "role": "user",
      "content": "Search Google for MiniRouter API documentation. Summarize what you find and cite the source URLs."
    }
  ],
  "tools": [
    {
      "type": "minirouter:google_search",
      "parameters": {
        "max_uses": 1,
        "max_results": 5,
        "region": "GB"
      }
    }
  ],
  "max_tool_calls": 1,
  "max_tokens": 512
}'
```

Read the answer in `choices[0].message.content`. MiniRouter handles the tool call for you.

1. **Ask**: Give the model a task and permission to search.
2. **Search**: Google results arrive through Serper: titles, URLs and snippets.
3. **Answer**: The model answers using those results in the same response.

Ask explicitly for search; the model may otherwise skip it. Raw results go to the model. Treat source content as untrusted.

## Set limits and region

| Option | Behavior |
| --- | --- |
| `max_uses` | Search attempts per request: 1–10. Default: 3. Dispatched attempts count even if they fail. |
| `max_results` | Results per search: 1–10. Default: 5. Each has `title`, `url` and `description`. Fewer may be available. |
| `region` | Optional uppercase country code, such as `GB` or `US`. |

Set these in `parameters`. The model supplies a `query` of 1–2,048 characters. Date, language and domain filters are unsupported.

`max_tool_calls` caps Google, datetime and web-fetch calls together: 1–30, default 5. Once this cap is reached, the model finishes without tools.

## Cost and usage

Each completed search costs $0.01. Chat Completions also charges model tokens. A completed search with no matches still counts. Failed or timed-out searches have no search fee.

*1 completed search costs $0.01; 3 completed searches cost $0.03; 10 completed searches cost $0.10. Model tokens are additional.*

Credit is reserved for the allowed searches before dispatch. Each model round costs tokens; `max_tokens` applies per round. Completed searches remain billable if the final answer fails.

`usage.server_tool_use.google_search_requests` counts completed searches. `usage.cost` includes search fees and all model rounds.

For streaming, add `stream: true` and `stream_options: {"include_usage": true}`. Totals arrive in the final usage chunk. Keep-alives precede the completed answer; intermediate rounds are not streamed token by token.

## Support and errors

In Chat Completions, search runs independently of the model provider and requires a supported paid route.

| Result | What to do |
| --- | --- |
| Invalid request | Check the endpoint, model and option ranges. |
| Insufficient credits | Add credit or lower search and output limits. Funds must cover searches and model tokens. |
| Search fails | The model receives the failure and can answer without results or retry within your limits. |

Search times out after 15 seconds. Handle [HTTP and streaming errors](https://minirouter.sh/docs/errors); check answers for missing sources.

Use [web fetch](https://minirouter.sh/docs/tools#available-server-tools) to read a full page, or compare [web and news search](https://minirouter.sh/docs/tools#search-the-web) for other search options.

# Models

318 models are listed in the current catalog.
Follow these steps in your terminal to make a text request.

## 1. Set your API key

Create a key at https://minirouter.sh/key and save the key and recovery link.
Replace paste-your-api-key-here with your key, then run:

```sh
export MINIROUTER_KEY='paste-your-api-key-here'
```

The request below uses a paid model. Add credits at
https://minirouter.sh/dashboard/billing first. For a no-deposit request, follow
https://minirouter.sh/docs/free-inference instead.

## 2. Choose a model

List model IDs with your key:

```sh
curl https://api.minirouter.sh/v1/models \
  -H "Authorization: Bearer $MINIROUTER_KEY"
```

Copy an id in author/name format from the response, or browse
https://minirouter.sh/models for pricing and supported features.
Use a text model for Chat Completions.

For text models with published measurements, [compare quality and monthly cost in Model Finder](https://minirouter.sh/rankings/model-finder) before choosing an exact ID.

## 3. Send a request

Run this in the same terminal. To use another text model, replace the model value with its exact ID.

```sh
curl https://api.minirouter.sh/v1/chat/completions \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "abliteration/abliterated-model",
  "messages": [
    {
      "role": "user",
      "content": "Explain an API gateway in one sentence."
    }
  ],
  "max_tokens": 256
}'
```

Read the answer in choices[0].message.content and the request cost in usage.cost.

Next: [request parameters](https://minirouter.sh/docs/parameters),
[thinking and reasoning](https://minirouter.sh/docs/reasoning),
[structured output](https://minirouter.sh/docs/structured-output), and
[streaming](https://minirouter.sh/docs/streaming).

## Let Auto choose a model

If you do not know which model to pick, send model "minirouter/auto". The
router selects a model and reports it in x-minirouter-model and the response
body. On Chat Completions and Responses, Auto can try another eligible model
if the first choice fails before output begins. Intents:
minirouter/auto:smart (the default for bare auto), minirouter/auto:cheap,
minirouter/auto:code, minirouter/auto:roleplay. These are paid intents. Auto works on chat completions,
responses and messages. See the current choices at https://minirouter.sh/api/v1/auto and
https://minirouter.sh/models/auto. A 503 auto_unresolvable means no candidate
for that intent is available right now; name a model directly. How Auto
chooses: https://minirouter.sh/docs/auto-router.

## Let Fusion read the request

Auto never reads prompt text. When minirouter/fusion is listed in /v1/models,
sending it has a classifier (TypeSafe Jev) label the request's task type and
complexity, and that label picks the model and the reasoning effort. The
classifier is sent the system prompt, the latest user message (both clipped)
and tool names; never tool results, images or assistant turns. Your own
reasoning, reasoning_effort or think setting is kept. The response header
x-minirouter-route-reason reports the label, for example
"task=code; tier=mid; effort=high". If the classifier is unavailable the
request is answered as minirouter/auto and the header is absent. Price:
Provider cost plus 10%, classification included. If classification is unavailable, the Auto rate of 8% applies. Details:
https://minirouter.sh/docs/fusion.

## Auto Free

minirouter/auto:free is a separate no-deposit alias with a $0 customer charge.
It never falls back to paid models, even after funding. Its initial named model,
poolside/laguna-s-2.1-free, shares the same free path and account quotas.
Check https://minirouter.sh/models/auto/free for availability and
https://minirouter.sh/docs/free-inference for setup, limits, and errors.
The three-message browser trial at /chat is separate from this API allowance.
Bare minirouter/auto remains paid Smart routing.

## Model aliases

Aliases are alternate names listed in a model's aliases array. Copy a listed
alias into the model field, just as you would a model ID.

Aliases ending in -latest follow the newest model in a family, such as
anthropic/claude-opus-latest. Model IDs used by other gateways, such as
x-ai/grok-4.6, also work.

Example using a currently listed alias:

```sh
curl https://api.minirouter.sh/v1/chat/completions \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "anthropic/claude-fable-latest",
  "messages": [
    {
      "role": "user",
      "content": "Explain an API gateway in one sentence."
    }
  ],
  "max_tokens": 256
}'
```

An alias can point to a different model over time, changing its capabilities and
price. Use the concrete model ID to keep your requests on the same model.
Your key must allow that model. The response model and x-minirouter-model header
identify the model that answered; Request logs also record the alias you requested.

## Model-list endpoints

GET https://api.minirouter.sh/v1/models: OpenAI-shaped list (authenticated)
GET https://minirouter.sh/api/v1/models: public model list with pricing,
context_length and architecture (no key required, CORS enabled).

Text-generation models have input and output token prices. Speech models
(type speech) price per character in pricing.speech_character; transcription
models (type transcription) price per second in pricing.transcription_second.
Language models that accept audio list audio in architecture.input_modalities.
See https://minirouter.sh/docs/audio.md. Video models (type video) price per second of
output in video.pricing and list accepted sizes and durations in video.controls.
See https://minirouter.sh/docs/videos.md.

## Image input (vision)

Models that accept images can describe photos, read screenshots, and extract
information from charts. Examples include GPT 5.6 Sol, Claude Opus 5,
Gemini 2.5 Flash, and GPT 4.1 Mini.

Check `architecture.input_modalities` in the
[public catalog](https://minirouter.sh/api/v1/models) for `image`, and
`architecture.output_modalities` for the response type. Check the exact
model ID: variants can have different capabilities. An image input capability
does not mean the model generates images.

Send images alongside your prompt using Chat Completions, Responses, or Messages.
Use a publicly accessible HTTPS image URL or base64-encoded JPEG, PNG, GIF or WebP.
You can include up to 20 images per request, within the request body size limit.
Set your API key and add credits as shown above before running these examples.

Image requests can require a larger temporary hold: MiniRouter reserves enough
for the model's full input capacity, since an image URL does not reveal its token
count. Final billing uses the provider's actual usage and cost.

## Chat Completions with images

Run this example, replacing `image_url.url` with your own image URL.
The prompt and image go in the same user message's `content` array.
`detail` accepts `auto`, `low`, or `high` where supported by the model.

```sh
curl --fail-with-body https://api.minirouter.sh/v1/chat/completions \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  --data '{
  "model": "openai/gpt-5.6-sol",
  "max_tokens": 512,
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "Describe what you see in this image."
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/1024px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg",
            "detail": "auto"
          }
        }
      ]
    }
  ]
}'
```

Read the answer in `choices[0].message.content`. For a local image, use a
base64 data URL such as `data:image/png;base64,YOUR_BASE64_IMAGE` in
`image_url.url`. Set `stream: true` to stream the text response.

## Responses with images

Use `input_image` content parts on the Responses endpoint. Replace
`image_url` with your HTTPS image URL or base64 data URL.

```sh
curl --fail-with-body https://api.minirouter.sh/v1/responses \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  --data '{
  "model": "openai/gpt-5.6-sol",
  "store": false,
  "max_output_tokens": 512,
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "Describe what you see in this image."
        },
        {
          "type": "input_image",
          "image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/1024px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg",
          "detail": "auto"
        }
      ]
    }
  ]
}'
```

Read text from `output` message items' `content` blocks with
`type: "output_text"`. Keep `store: false` and supply conversation history
on each turn. Set `stream: true` to stream the answer. Tool results can also
include `input_image` parts when their matching tool call is in the supplied history.

## Messages with images

Use a `type: "image"` block alongside your prompt. Replace `source.url`
with your publicly accessible HTTPS image URL.

```sh
curl --fail-with-body https://api.minirouter.sh/v1/messages \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  --data '{
  "model": "openai/gpt-5.6-sol",
  "max_tokens": 512,
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "Describe what you see in this image."
        },
        {
          "type": "image",
          "source": {
            "type": "url",
            "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/1024px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg"
          }
        }
      ]
    }
  ]
}'
```

Read the answer in response `content` blocks with `type: "text"`.
For a local file, replace the image's `source` object with
`{"type":"base64","media_type":"image/png","data":"YOUR_BASE64_IMAGE"}`.
Use raw base64 without a data-URL prefix on this endpoint.

To generate an image or edit an existing image, use the Images API below.


## Images API

Generate images with any model listed under Images on the
[models page](https://minirouter.sh/models). Each model page lists its price,
the settings it accepts, and examples for the operations it supports.

GPT Image models:

- [openai/gpt-image-2.5-flare](https://minirouter.sh/models/openai/gpt-image-2.5-flare)
- [openai/gpt-image-2.5-sunburst](https://minirouter.sh/models/openai/gpt-image-2.5-sunburst)

Set your API key as shown above and add credits, then run this request to
`POST /v1/images/generations`. It saves the response to `response.json` and
its headers to `response.headers`.

```sh
curl --fail-with-body --retry 0 --max-time 120 \
  https://api.minirouter.sh/v1/images/generations \
  -H "Authorization: Bearer $MINIROUTER_KEY" \
  -H "Content-Type: application/json" \
  -D response.headers --output response.json \
  --data '{
  "model": "openai/gpt-image-2.5-flare",
  "prompt": "A blue circle on a white background.",
  "quality": "low",
  "size": "1024x1024",
  "output_format": "png",
  "n": 1
}'
```

After a successful response, decode `data[0].b64_json` to save the PNG:

```sh
python3 -c 'import base64,json; r=json.load(open("response.json")); open("image.png","wb").write(base64.b64decode(r["data"][0]["b64_json"]))'
```

Check the model's `image.operations` and `image.controls` in the
[public catalog](https://minirouter.sh/api/v1/models).
Model-specific settings must be listed in `image.controls`; other provider settings are rejected.
Where `image.controls.quality` is present, choose a quality from that list and
a size from `image.controls.size`. Omitted values use `image.controls.defaults`.
The matching entry in `image.pricing.variants` gives the output-image estimate
for that size and quality; add prompt and reference-image costs when estimating
the total. Higher quality and larger images can cost more. Auto settings let the
provider choose and reserve credit for the largest or highest-quality output it
may choose. Image Studio shows the estimate for your selected settings.

A generation capability does not imply editing. Where `operations.edit` is
present, use `POST /v1/images/edits` with multipart form data and one image.
GPT Image 2.5 Flare and Sunburst require a 1024 × 1024 PNG; other editors accept
PNG, JPEG or WebP up to 1,048,576 pixels. Where the catalog lists a non-zero
`pricing.image_input`, the longer side can be at most 4,096 pixels. A file can
be at most 8 MiB. Use a mask only when the exact model's edit capability allows it.

Each request returns one image. Copy the required settings from the model page;
GPT Image controls and sizes do not apply to every image model. Catalog prices
show their unit: per-image reservation estimates and token reservation rates
are different quantities. Final billing uses the provider's actual cost plus
the MiniRouter fee.
Use a concrete image model ID or one of its listed aliases. Image requests do
not use Auto or text-model fallback lists.

A temporary hold reserves twice the estimated cost, including the MiniRouter
fee. The final charge cannot exceed that hold. The image response can arrive
before billing finishes. Keep `x-minirouter-request-id` from the response
headers and check `GET /v1/images/status?request_id=YOUR_REQUEST_ID` with the
same account before resubmitting an uncertain request. Disable automatic retries.

If you cache image catalog records, refresh them after `catalog_expires_at`.
The image endpoints do not serve video, embedding, reranking or realtime requests:
embeddings and reranking have their own endpoints, speech and transcription use the
[Audio API](https://minirouter.sh/docs/audio.md), video uses the
[Video API](https://minirouter.sh/docs/videos.md), and realtime is not offered.


## Fallback models

For text requests, models is an ordered list of acceptable fallback model IDs. minirouter never
uses a model outside that list. minirouter/auto cannot be combined with a
caller-supplied fallback list; it automatically uses its own ordered list,
published at /api/v1/auto. For Gateway routing, every model in an explicit
fallback list must share a qualified provider. Incompatible lists are rejected
before dispatch. Auto and Fusion omit incompatible backups while keeping the
selected model available.

See [model fallbacks](https://minirouter.sh/docs/model-fallbacks) for request examples, limits and billing.

## Unknown model

An unavailable ID returns 404 unknown_model with a did-you-mean suggestion. A
real ID this key's policy excludes returns 403 model_not_allowed.