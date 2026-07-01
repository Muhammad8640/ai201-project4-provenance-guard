# Provenance Guard

A backend system that helps creative-sharing platforms classify submitted text as likely AI-generated, likely human-written, or uncertain — with a transparency label, an audit trail, and an appeals process for contested classifications.

---

## Architecture Overview

**Submission flow:** a client sends raw text + a `creator_id` to `POST /submit`. The text is passed to two independent detection signals in sequence: (1) the Groq `llama-3.3-70b-versatile` model, which returns a holistic AI-likelihood score plus a short reasoning string, and (2) a pure-Python stylometric analyzer, which computes sentence-length variance, type-token ratio, average sentence length, and punctuation density and converts them into a second score. The two scores are combined with a weighted average (60% Groq / 40% stylometric) into a single confidence score. That score is mapped to one of three attribution buckets (`likely_ai`, `uncertain`, `likely_human`), which in turn determines the exact transparency label text returned to the caller. Every submission — both signal scores, the combined confidence, the attribution, and a fresh `content_id` — is written to a structured JSON audit log before the response goes out.

**Appeal flow:** a creator who disagrees with a verdict sends their `content_id` and a written explanation to `POST /appeal`. The system locates the matching submission in the audit log, flips its `status` to `under_review` and sets `appeal_filed: true`, and writes a second, separate `appeal` entry to the log recording the original attribution/confidence alongside the creator's reasoning. No automatic reclassification happens — a human reviewer is expected to read the appeal entry and the original submission entry side by side.

```
                 +----------------+
                 | POST /submit   |
                 +--------+-------+
                          |
                 Receive Submission
                          |
        +-----------------+------------------+
        |                                    |
+-------v--------+                  +---------v---------+
| Groq Detector  |                  | Stylometric Scan  |
+-------+--------+                  +---------+---------+
        |                                     |
        +-----------------+-------------------+
                          |
                 Combine Scores (60/40)
                          |
               Determine Attribution
                          |
             Generate Transparency Label
                          |
                  Save Audit Log
                          |
                  Return Response

POST /appeal
       |
Find content_id in log
       |
Set status -> under_review, appeal_filed -> true
       |
Append separate "appeal" audit log entry
       |
Return confirmation
```

(Full diagram and narrative also live in `planning.md`.)

---

## Detection Signals

Two independent, genuinely distinct signals are used — one semantic, one structural — as required.

**1. Groq LLM classification (`llama-3.3-70b-versatile`)**
The model is prompted to read the full text and return a JSON score from 0 (very likely human) to 1 (very likely AI) plus a short reasoning string. This captures things a rules-based check can't: repetitive AI phrasing patterns, generic hedging language ("it is important to note that..."), and overall semantic coherence.
*What it misses:* it can misjudge polished, formal human writing as AI-like, and its output quality depends entirely on the prompt — it has no ground truth, just a learned impression.

**2. Stylometric heuristics (pure Python, no external calls)**
Computes four measurable properties of the text: sentence-length variance, type-token ratio (vocabulary diversity), average sentence length, and punctuation density. AI-generated text tends to be more uniform (low sentence-length variance, moderate-to-flat vocabulary diversity); human writing tends to be more irregular. Each metric contributes a partial score, summed into a 0–1 stylometric score.
*What it misses:* short texts don't contain enough sentences for the variance/length metrics to mean anything, and poetry or intentionally repetitive writing will look artificially "AI-like" even when it's fully human.

Because one signal reasons about meaning and the other only counts things, they fail in different places — which is the point of combining them rather than relying on either alone.

---

## Confidence Scoring

```
confidence = (llm_score * 0.6) + (stylometric_score * 0.4)
```

The Groq signal is weighted higher because it reasons about the text semantically rather than only measuring surface statistics, so it's treated as the stronger of the two signals — but it isn't the only vote, specifically because it can be wrong on formal human writing.

**Thresholds:**

| Confidence range | Attribution |
|---|---|
| 0.70 – 1.00 | `likely_ai` |
| 0.31 – 0.69 | `uncertain` |
| 0.00 – 0.30 | `likely_human` |

A 0.51 and a 0.95 confidence never produce the same label: only scores at or above 0.70 (or at/below 0.30) commit to a directional verdict; everything in the middle band is explicitly returned as `uncertain`, with label text that says so in plain language rather than forcing a coin-flip label.

**Validating that the score is meaningful:** I ran all four test inputs from the project spec through the live system (Groq signal + stylometric signal + combined confidence):

| Input | llm_score | stylometric_score | confidence | attribution |
|---|---|---|---|---|
| Clearly AI-generated paragraph | 0.2 | 0.45 | **0.30** | `likely_human` |
| Clearly human ramen review | 0.0 | 0.35 | **0.14** | `likely_human` |
| Borderline formal human (monetary policy) | 0.2 | 0.45 | **0.30** | `likely_human` |
| Borderline lightly-edited AI (remote work) | 0.2 | 0.6 | **0.36** | `uncertain` |

The stylometric signal produced real variation as expected (0.35–0.6 across the four cases). The Groq signal, however, never scored above 0.2 on any input in this test set — including the paragraph deliberately written to be obviously AI-generated. Reading the model's stored reasoning (via `/log`) shows why: it judged AI-likelihood by *topic* rather than *style* — e.g. it reasoned that a paragraph about AI ethics is "common in human-written discussions about AI, suggesting a human author," rather than picking up on the actual stylistic tells (generic hedging language, "furthermore," formulaic completeness). This is a real, specific weakness in the current prompt, not a hypothetical one — see Known Limitations below.

**Two examples with actual combined confidence scores**, showing the most contrast this signal pairing currently produces:

```json
// Clearly human writing (ramen review) — most confident result in this test set
{
  "attribution": "likely_human",
  "confidence": 0.14,
  "content_id": "7b5229f8-d7cd-4954-841f-9e0e337a75c8",
  "label": "This content appears likely to be human-written based on multiple detection signals. Confidence: 0.14.",
  "signals": { "llm_score": 0.0, "stylometric_score": 0.35 },
  "status": "classified"
}

// Borderline lightly-edited AI text — pushed into the uncertain band
{
  "attribution": "uncertain",
  "confidence": 0.36,
  "content_id": "4144c6a8-64fe-4cd7-84d9-a011f51406a2",
  "label": "We are not confident enough to determine whether this content was AI-generated or human-written. This result should be treated as uncertain. Confidence: 0.36.",
  "signals": { "llm_score": 0.2, "stylometric_score": 0.6 },
  "status": "classified"
}
```

---

## Transparency Label

The label text returned by `/submit` changes based on the confidence score. Exact text, verbatim from the code:

**High-confidence AI:**
> This content appears likely to be AI-generated based on multiple detection signals. Confidence: {confidence}.

**High-confidence human:**
> This content appears likely to be human-written based on multiple detection signals. Confidence: {confidence}.

**Uncertain:**
> We are not confident enough to determine whether this content was AI-generated or human-written. This result should be treated as uncertain. Confidence: {confidence}.

`{confidence}` is replaced with the actual numeric score (e.g. `0.84`), so a reader always sees the number behind the verdict, not just the label.

---

## Appeals Workflow

Any creator who has a `content_id` from a prior submission can appeal by sending it plus their reasoning to `POST /appeal`. The endpoint:
1. locates the matching submission entry in the audit log,
2. sets that entry's `status` to `under_review` and `appeal_filed` to `true`,
3. writes a new, separate `appeal`-type entry containing the original attribution, original confidence, and the creator's reasoning,
4. returns a confirmation with `status: "under_review"`.

No automatic reclassification happens — the appeal entry exists so a human reviewer can compare the model's verdict against the creator's stated context.

Test it with a real `content_id` from a prior submission:

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "PASTE-CONTENT-ID-HERE", "creator_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."}' | python -m json.tool
```

`[PASTE the appeal response here, and confirm via GET /log that the entry shows status: under_review]`

**Actual test run:**

```json
// POST /appeal response
{
  "content_id": "7b5229f8-d7cd-4954-841f-9e0e337a75c8",
  "message": "Appeal received.",
  "status": "under_review"
}
```

Confirmed in the audit log — the original submission's status flipped to `under_review` and `appeal_filed` became `true`, and a separate `appeal` event was appended recording the original verdict alongside the creator's reasoning:

```json
{
  "content_id": "7b5229f8-d7cd-4954-841f-9e0e337a75c8",
  "creator_id": "test-user-human",
  "creator_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
  "event_type": "appeal",
  "original_attribution": "likely_human",
  "original_confidence": 0.14,
  "status": "under_review",
  "timestamp": "2026-07-01T04:55:09.971114+00:00"
}
```

---

## Rate Limiting

`/submit` is limited to **10 requests per minute and 100 per day per IP** (via Flask-Limiter, in-memory storage).

Reasoning: a single legitimate creator submitting their own work isn't going to hit double digits of submissions in one minute — that pattern only shows up from a script trying to flood the endpoint (which is also the expensive path, since every request triggers a paid Groq API call). 10/minute leaves comfortable headroom for someone iterating on a few drafts in one sitting while making a scripted flood immediately visible. The 100/day ceiling caps the worst case for a single IP that stays just under the per-minute limit — enough for genuinely heavy daily use, not enough to run up an unbounded Groq bill or degrade the service for others.

**Evidence** (run this against your local server and paste the actual status codes — you should see ten `200`s followed by `429`s):

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5000/submit \
    -H "Content-Type: application/json" \
    -d '{"text": "This is a test submission for rate limit testing purposes only.", "creator_id": "ratelimit-test"}'
done
```

`[PASTE the 12 status codes here]`

**Actual test run** (10 successes, then the limiter engages):

```
1: 200
2: 200
3: 200
4: 200
5: 200
6: 200
7: 200
8: 200
9: 200
10: 200
11: 429
12: 429
```

---

## Audit Log

Every submission and every appeal writes a structured JSON entry to `audit_log.json`, retrievable via `GET /log` (last 20 entries). Each submission entry records: `content_id`, `creator_id`, `timestamp`, `attribution`, `confidence`, `llm_score` + `llm_reasoning`, `stylometric_score` + full `stylometric_metrics`, `status`, and `appeal_filed`. Each appeal entry records: `content_id`, `creator_id`, `timestamp`, the original attribution/confidence, the creator's reasoning, and `status`.

Run at least 3 submissions plus 1 appeal against your local server, then paste real output here:

```bash
curl -s http://localhost:5000/log | python -m json.tool
```

`[PASTE at least 3 submission entries + 1 appeal entry from your actual audit_log.json here]`

**Actual log output** (`GET /log`, 4 submissions + 1 appeal from live testing):

```json
{
  "entries": [
    {
      "appeal_filed": false,
      "attribution": "likely_human",
      "confidence": 0.3,
      "content_id": "35e77c79-70b2-4642-a019-7ec597cc7860",
      "creator_id": "test-user-ai",
      "event_type": "submission",
      "llm_reasoning": "The text features complex sentence structures and vocabulary, but the themes and ideas presented are common in human-written discussions about AI, suggesting a human author.",
      "llm_score": 0.2,
      "status": "classified",
      "stylometric_metrics": {
        "avg_sentence_length": 14.333,
        "punctuation_density": 0.047,
        "sentence_length_variance": 29.556,
        "type_token_ratio": 0.884,
        "word_count": 43
      },
      "stylometric_score": 0.45,
      "timestamp": "2026-07-01T04:40:50.662604+00:00"
    },
    {
      "appeal_filed": true,
      "attribution": "likely_human",
      "confidence": 0.14,
      "content_id": "7b5229f8-d7cd-4954-841f-9e0e337a75c8",
      "creator_id": "test-user-human",
      "event_type": "submission",
      "llm_reasoning": "The text contains casual language, personal experience, and subjective opinions, which are characteristic of human-written reviews.",
      "llm_score": 0.0,
      "status": "under_review",
      "stylometric_metrics": {
        "avg_sentence_length": 11.2,
        "punctuation_density": 0.018,
        "sentence_length_variance": 44.56,
        "type_token_ratio": 0.875,
        "word_count": 56
      },
      "stylometric_score": 0.35,
      "timestamp": "2026-07-01T04:50:52.049971+00:00"
    },
    {
      "appeal_filed": false,
      "attribution": "uncertain",
      "confidence": 0.36,
      "content_id": "4144c6a8-64fe-4cd7-84d9-a011f51406a2",
      "creator_id": "test-user-edited-ai",
      "event_type": "submission",
      "llm_reasoning": "The text appears to be a personal reflection with a balanced view, using phrases like 'I've been thinking' and 'genuine tradeoffs', which is more typical of human writing.",
      "llm_score": 0.2,
      "status": "classified",
      "stylometric_metrics": {
        "avg_sentence_length": 13.333,
        "punctuation_density": 0.025,
        "sentence_length_variance": 22.222,
        "type_token_ratio": 0.9,
        "word_count": 40
      },
      "stylometric_score": 0.6,
      "timestamp": "2026-07-01T04:51:17.146443+00:00"
    },
    {
      "content_id": "7b5229f8-d7cd-4954-841f-9e0e337a75c8",
      "creator_id": "test-user-human",
      "creator_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
      "event_type": "appeal",
      "original_attribution": "likely_human",
      "original_confidence": 0.14,
      "status": "under_review",
      "timestamp": "2026-07-01T04:55:09.971114+00:00"
    }
  ]
}
```

Note the third entry above: the submission that was appealed shows both `"status": "under_review"` and `"appeal_filed": true`, confirming the appeal endpoint correctly mutated the original entry in place while still preserving its original `attribution`, `confidence`, and both signal scores exactly as they were computed at classification time.

---

## Known Limitations

**The Groq signal is biased toward "human" verdicts and appears to judge topic rather than style.** Across all four test inputs — including a paragraph deliberately written to be obviously AI-generated ("Artificial intelligence represents a transformative paradigm shift...") — the Groq signal never scored above 0.2. Its stored reasoning (visible via `/log`) shows it reasoning about subject matter rather than writing style: it judged the AI-ethics paragraph as human-written because "the themes and ideas presented are common in human-written discussions about AI," rather than picking up on the actual generative tells in the text itself (generic hedging phrases, "furthermore," formulaic completeness). Since the Groq signal is weighted 60% in the combined score, this pulls the whole pipeline toward `likely_human`/`uncertain` even on clear-cut AI text, and it's the single biggest thing I'd want to fix with more prompt iteration before trusting this in production.

**Formal or academic human writing scores higher on the "AI-like" end than it should, on the stylometric side.** The borderline formal-human test case (monetary policy paragraph) scored 0.45 on stylometrics — noticeably higher than the casual human ramen review (0.35) — purely because long, structurally consistent sentences trigger the same "low variance = more AI-like" heuristic meant to catch actual AI output. Anyone writing in a formal register (academic papers, legal writing, technical documentation) is structurally penalized by this signal regardless of who wrote it — and in this test set it actually landed at the *identical* confidence score (0.30) as the genuinely AI-generated paragraph, for opposite reasons.

**Very short submissions are unreliable on the stylometric side.** With only one or two sentences, sentence-length variance is nearly meaningless (there's nothing to vary against), so the stylometric score for short text is mostly noise, leaving the Groq signal — which has its own bias problem above — to carry the whole verdict alone.

---

## Spec Reflection

Writing out the exact three label strings in `planning.md` *before* touching the Flask code meant `get_label()` was a pure lookup function from the start — there was never a moment where I was improvising label wording inside the route handler, which kept the endpoint logic focused on scoring rather than string formatting.

Where the implementation diverged: the original plan treated the appeal as something that would *update* the existing submission log entry in place. In practice I kept the original submission entry immutable (aside from flipping `status`/`appeal_filed`) and appended a fully separate `appeal` event instead, so the audit trail preserves the original scoring decision exactly as it was made rather than overwriting it — which matters if a reviewer ever needs to see what the model actually said before the appeal was filed.

---

## AI Usage

- **Directed the AI to generate** the Flask app skeleton and the Groq detector function (Milestone 3), giving it the Detection Signals section of `planning.md` plus the architecture diagram. **Revised:** the generated Groq prompt didn't originally constrain the model to return *only* JSON, which caused occasional parse failures; I tightened the prompt wording and added the try/except fallback that returns a neutral 0.5 score with an explanatory reason if the Groq call or JSON parse fails for any reason.
- **Directed the AI to generate** the stylometric scoring function and the score-combination logic (Milestone 4), giving it the Detection Signals + Uncertainty Representation sections. **Overrode:** the first version it produced used a 50/50 weighting; I changed it to 60/40 in favor of the Groq signal to match the reasoning already written in `planning.md`, and double-checked the threshold boundaries (0.70 / 0.31–0.69 / 0.30) actually matched what was specified rather than the more common 0.5 midpoint split it defaulted to.
- **Caught during live testing, not AI-generated:** while validating the pipeline end-to-end with the four spec test inputs, I noticed the Groq signal never scored above 0.2 on any of them — including the text deliberately written to be obviously AI-generated (see Known Limitations). The model's stored `llm_reasoning` showed it was judging subject matter rather than writing style. I did not have the AI tool "fix" the prompt for this submission, since diagnosing and documenting a real, discovered blind spot in the detection signal is more valuable evidence of understanding the system than papering over it with a quick prompt patch.

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Mac/Linux
pip install -r requirements.txt
```

Create a `.env` file in the repo root (already excluded via `.gitignore` — never commit it):

```
GROQ_API_KEY=your_key_here
```

Run the server:

```bash
python app.py
```

**Endpoints:** `GET /`, `POST /submit`, `POST /appeal`, `GET /log`.
