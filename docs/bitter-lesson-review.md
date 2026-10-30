# Bitter Lesson review

The principle applied here is to give the agent the evidence and general tools it
needs to make editorial decisions, then measure the results. This is an
application of Sutton's argument for scalable search and learning; using an
agent does not itself establish that the editorial choices are better.

Read: [Rich Sutton, The Bitter Lesson, 2019](https://www.cs.utexas.edu/~eunsol/courses/data/bitter_lesson.pdf).
Google's [discovery format](https://developers.google.com/discovery/v1/reference/apis)
is the source of API methods, request schemas, parameters and HTTP verbs.

| Previous constraint | Change |
|---|---|
| Separate OpenRouter model picked excerpts and wrote summaries/chapters | The daily Claude session reads the complete transcript and authors every selection and its metadata |
| Summary sentence counts and chapter-count targets in code | Removed; The agent chooses what the discussion needs |
| Fixed source-sampling positions | The agent supplies the interval to transcribe |
| Fixed analytics queries and two ranking choices | General discovery-driven requests with unmodified responses |
| Retention rounded and sampled at fixed percentages | The agent can request the full service response |
| Dedicated title-update and live-thumbnail API wrappers | General mutations; thumbnail rendering remains a local mechanism |
| Fixed 24/48-hour delta output | Raw dated observations; The agent chooses comparisons |
| Hardcoded channel label/order table | Configuration supplies handles and ordering |
| Source directory existence treated as proof of clipping | Saved selections determine local completion; a prepared transcript remains eligible |
| Duplicate JSON submission examples in configuration | The MCP tool schema defines the render contract |

## Deliberately retained

- The owner's channel identities, speaker/content preferences, thumbnail design,
  Sydney publishing cadence and experiment policies. These define the desired
  outcome and remain editable in channel guidance and configuration.
- Generic validation, channel/file boundaries, explicit read/write separation,
  durable mutation journals and no automatic write retries. The discovery
  document supplies the method classification; there is no endpoint allowlist.
- ffmpeg encoding and verification, cached transcripts, stable render selections,
  queue deduplication and resumable upload/recovery jobs. These execute and
  preserve work rather than decide which ideas deserve publication. Queue
  status reconciliation and confirmed-upload media cleanup now run with the
  hourly collector instead of being hidden inside a list-videos tool.
- Existing OAuth permissions. This change does not request additional scopes.
- The hourly Studio collector, by the owner's explicit choice. Its private API
  requests, metric mappings and response parsing remain the main brittle
  integration. They supply measured data, not editorial decisions. Studio has
  no published discovery document to drive the same general interface.

## Operational scope and verification

All state lives in one SQLite database; the observer reads it through a single
read-only SQL tool rather than purpose-built report endpoints. Existing public
uploads are never reuploaded. Transcript preparation and render jobs persist
their stages. New transcripts never enter an external text-model
selection or annotation path; OpenRouter remains only for image generation.

Local end-to-end tests exercise MCP and CLI preparation, lossless transcript
pagination, agent-supplied selections, real ffmpeg exports and timing, immutable
retries, queue deduplication, channel/file isolation, discovered API calls,
raw analytics responses, mutations and their journals, failure ambiguity and
media uploads. External services are replaced by local providers behind a
TLS-intercepting proxy, so the binary runs with its production URLs. These
checks do not measure live editorial quality or establish deployment.
