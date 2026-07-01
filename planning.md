# Provenance Guard - Planning

## Project Overview

Provenance Guard is a backend system that helps determine whether submitted text is likely AI-generated or human-written. The system combines multiple independent detection signals, calculates a confidence score, displays a transparency label to users, records every decision in an audit log, and provides an appeals workflow for creators.

---

# Detection Signals

## Signal 1 – Groq LLM Classification

### Purpose

Uses the Groq Llama-3.3-70B model to evaluate the overall writing style and determine whether it resembles AI-generated or human-written text.

### Output

A score between **0 and 1**

- 0.0 = Very likely human
- 0.5 = Uncertain
- 1.0 = Very likely AI

### Strengths

- Understands writing style holistically.
- Detects repetitive AI language.
- Considers semantic consistency.

### Weaknesses

- Can misclassify polished human writing.
- Depends on the quality of the prompt.

---

## Signal 2 – Stylometric Heuristics

The second signal analyzes measurable characteristics of the text.

Metrics include:

- Sentence length variance
- Average sentence length
- Vocabulary diversity (Type-Token Ratio)
- Punctuation density

Output:

Score from 0–1

- Higher score → more AI-like
- Lower score → more human-like

### Strengths

- Fast
- Completely deterministic
- Doesn't require an API

### Weaknesses

- Less accurate on very short text
- Poetry may produce misleading statistics

---

# Confidence Scoring

The two signals are combined using weighted averaging.

```
Final Score =
60% Groq Score
+
40% Stylometric Score
```

## Thresholds

```
0.70 – 1.00
Likely AI

0.31 – 0.69
Uncertain

0.00 – 0.30
Likely Human
```

Reasoning:

The Groq model is given slightly more weight because it captures semantic meaning rather than only numerical writing characteristics.

## What a Confidence Score Actually Means

A score isn't a probability in any statistically rigorous sense — it's a weighted blend of two heuristic signals, and it should be read as "how much agreement exists across independent evidence," not "the odds this text is AI-generated."

- **0.6** means the two signals lean mildly toward AI but don't agree strongly enough to commit — it lands in the `uncertain` band on purpose. A user seeing 0.6 should understand "the system noticed some AI-like patterns but isn't confident," not "there's a 60% chance this is AI."
- **0.95** means both signals agree strongly and consistently — enough that the system is willing to state a direction plainly.
- The gap between 0.51 and 0.95 is deliberately reflected in the label text, not just the number: only scores at or beyond the 0.70 / 0.30 thresholds commit to a directional label. Everything between stays in the `uncertain` bucket with wording that says the system couldn't decide, rather than manufacturing false confidence.

## The False Positive Problem

On a platform for creative work, wrongly labeling a human's writing as AI-generated is worse than the reverse — it damages a real person's reputation and their trust in the platform, whereas a missed AI-generated piece is a milder, more recoverable failure. This shaped three concrete decisions:

1. **The uncertain band is wide on purpose** (0.31–0.69 is intentionally wider than a naive 0.5 midpoint split would suggest), so borderline cases default to "we're not sure" rather than a confident-sounding wrong answer.
2. **The label copy for `likely_ai` doesn't claim certainty** — it says "appears likely to be AI-generated based on multiple detection signals" and always shows the numeric confidence, so a reader can judge for themselves how much weight to put on it rather than treating the label as a verdict.
3. **The appeal path exists specifically so a false positive is recoverable** — a misclassified human creator has an immediate, low-friction way to contest the label rather than being stuck with it. Tracing this scenario end to end: a human writer submits formal, structurally consistent writing → the stylometric signal (and potentially the LLM signal) reads that formality as AI-like → confidence lands in the 0.5–0.7 range → the label reads as `likely_ai` or `uncertain` rather than a hard accusation → the creator sees the confidence number, recognizes it's not overwhelming, and files an appeal with context the model didn't have (e.g. "I'm a non-native English speaker" or "this is an academic excerpt") → the audit log preserves both the original verdict and the appeal side by side for a human reviewer.

---

# Transparency Labels

## High Confidence AI

> This content appears likely to be AI-generated based on multiple detection signals.
>
> Confidence: HIGH

---

## High Confidence Human

> This content appears likely to be human-written based on multiple detection signals.
>
> Confidence: HIGH

---

## Uncertain

> We are not confident enough to determine whether this content was AI-generated or human-written.
>
> This result should be treated as uncertain.

---

# Appeals Workflow

A creator can appeal a classification by submitting:

- content_id
- creator_reasoning

The system will

1. Locate the original submission
2. Update its status to:

```
under_review
```

3. Save the creator's reasoning
4. Record the appeal inside the audit log

No automatic reclassification is performed.

## Who Can Appeal, and What a Reviewer Sees

Any creator who received a `content_id` from a prior `/submit` call can file an appeal — no additional authentication is required at this stage, since the goal is a low-friction path for a creator to contest a result, not a gated review process.

A human reviewer opening the appeal queue (in this implementation, reading `GET /log` and filtering for `event_type: "appeal"`) would see, for each appeal:
- The original submission's full record — both signal scores, the combined confidence, the attribution, and the exact label text the creator saw — untouched and preserved exactly as it was at classification time.
- The creator's own reasoning, in their own words, submitted as free text.
- The current status (`under_review`), distinguishing appealed content from content that was simply classified and never contested.

The reviewer's job is to compare the model's stated reasoning (visible in `llm_reasoning`) against the creator's explanation and make a human judgment call — the system deliberately does not attempt this itself.

---

# API Endpoints

## GET /

Returns a welcome message.

---

## POST /submit

Input

```json
{
  "text": "...",
  "creator_id": "user123"
}
```

Returns

```json
{
  "content_id": "...",
  "attribution": "likely_ai",
  "confidence": 0.84,
  "label": "...",
  "status": "classified"
}
```

---

## POST /appeal

Input

```json
{
  "content_id": "...",
  "creator_reasoning": "I wrote this myself."
}
```

Returns

```json
{
  "message": "Appeal received.",
  "status": "under_review"
}
```

---

## GET /log

Returns all audit log entries.

---

# Architecture

```
                 +----------------+
                 | POST /submit   |
                 +--------+-------+
                          |
                          |
                 Receive Submission
                          |
        +-----------------+------------------+
        |                                    |
        |                                    |
+-------v--------+                  +---------v---------+
| Groq Detector  |                  | Stylometric Scan |
+-------+--------+                  +---------+---------+
        |                                     |
        +-----------------+-------------------+
                          |
                 Combine Scores
                          |
                 Confidence Score
                          |
               Determine Attribution
                          |
             Generate Transparency Label
                          |
                  Save Audit Log
                          |
                  Return Response
```

Appeal Flow

```
POST /appeal
       |
Find content_id
       |
Update status
       |
under_review
       |
Save appeal
       |
Audit Log
       |
Return confirmation
```

---

# Calibration Validation

Milestone 4 requires testing the scoring pipeline against deliberately chosen inputs spanning the confidence range, and checking whether the results match intuition. Actual results from a live run against the four spec test inputs:

| Input | llm_score | stylometric_score | confidence | attribution |
|---|---|---|---|---|
| Clearly AI-generated paragraph | 0.2 | 0.45 | 0.30 | `likely_human` |
| Clearly human ramen review | 0.0 | 0.35 | 0.14 | `likely_human` |
| Borderline formal human (monetary policy) | 0.2 | 0.45 | 0.30 | `likely_human` |
| Borderline lightly-edited AI (remote work) | 0.2 | 0.6 | 0.36 | `uncertain` |

The stylometric signal behaved as designed — real variation across inputs, correctly flagging the formal/uniform text as more AI-like on structure alone. The Groq signal did not: it never scored above 0.2 on any input, including the paragraph deliberately written to be obviously AI-generated. Its stored reasoning showed it was evaluating whether the *topic* is something humans commonly write about, rather than whether the *style* of the writing itself looks generated. This means the current system, as calibrated, is biased toward `likely_human`/`uncertain` verdicts and would need Groq-prompt iteration (e.g., asking explicitly about generative stylistic markers rather than plausibility of authorship) before it could be trusted to catch clear-cut AI text in production. This is documented as a known limitation in the README rather than fixed post hoc, since it's a legitimate finding about a real blind spot in the signal as specified.

---

# Edge Cases

### Formal Academic Writing

May appear AI-generated because it is structured and grammatically consistent.

---

### Poetry

Sentence length and punctuation metrics are unreliable.

---

### Very Short Text

Small samples don't contain enough information for reliable stylometric analysis.

---

### Non-native English Writers

Writing style may resemble AI due to simple vocabulary and repetitive sentence structure.

---

# AI Tool Plan

## Milestone 3

Provide:

- Detection Signals
- Architecture

Ask AI to generate:

- Flask application
- /submit endpoint
- Groq detector

Verify:

- Endpoint returns valid JSON.

---

## Milestone 4

Provide:

- Detection Signals
- Confidence Scoring

Ask AI to generate:

- Stylometric detector
- Score combination logic

Verify:

- AI text scores higher than human text.

---

## Milestone 5

Provide:

- Transparency Labels
- Appeals Workflow

Ask AI to generate:

- Label generation
- /appeal endpoint
- Rate limiting
- Audit logging

Verify:

- All three labels appear.
- Appeals update status correctly.
- Audit log records every action.