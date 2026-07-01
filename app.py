import json
import os
import re
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq


load_dotenv()

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

LOG_FILE = "audit_log.json"
client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def read_log():
    if not os.path.exists(LOG_FILE):
        return []

    with open(LOG_FILE, "r", encoding="utf-8") as file:
        try:
            return json.load(file)
        except json.JSONDecodeError:
            return []


def write_log(entries):
    with open(LOG_FILE, "w", encoding="utf-8") as file:
        json.dump(entries, file, indent=2)


def add_log_entry(entry):
    entries = read_log()
    entries.append(entry)
    write_log(entries)


def groq_signal(text):
    """
    Returns a score from 0 to 1.
    1 = likely AI-generated
    0 = likely human-written
    """
    api_key = os.getenv("GROQ_API_KEY")

    if not api_key:
        return {
            "score": 0.5,
            "reasoning": "Groq API key missing, so neutral fallback score was used."
        }

    prompt = f"""
You are an AI-content attribution assistant.

Analyze the text and return ONLY valid JSON in this exact format:
{{
  "score": 0.0,
  "reasoning": "brief explanation"
}}

The score must be between 0 and 1:
- 1.0 means very likely AI-generated
- 0.5 means uncertain
- 0.0 means very likely human-written

Text:
{text}
"""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
        )

        content = response.choices[0].message.content.strip()
        result = json.loads(content)

        score = float(result.get("score", 0.5))
        score = max(0.0, min(1.0, score))

        return {
            "score": score,
            "reasoning": result.get("reasoning", "No reasoning provided.")
        }

    except Exception as error:
        return {
            "score": 0.5,
            "reasoning": f"Groq signal failed, so neutral fallback score was used. Error: {error}"
        }


def stylometric_signal(text):
    """
    Returns a score from 0 to 1.
    1 = likely AI-generated
    0 = likely human-written
    """

    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]

    words = re.findall(r"\b\w+\b", text.lower())
    word_count = len(words)

    if word_count == 0:
        return {
            "score": 0.5,
            "metrics": {
                "word_count": 0,
                "type_token_ratio": 0,
                "avg_sentence_length": 0,
                "sentence_length_variance": 0,
                "punctuation_density": 0
            }
        }

    unique_words = len(set(words))
    type_token_ratio = unique_words / word_count

    sentence_lengths = [
        len(re.findall(r"\b\w+\b", sentence))
        for sentence in sentences
    ]

    if sentence_lengths:
        avg_sentence_length = sum(sentence_lengths) / len(sentence_lengths)
        sentence_length_variance = sum(
            (length - avg_sentence_length) ** 2 for length in sentence_lengths
        ) / len(sentence_lengths)
    else:
        avg_sentence_length = word_count
        sentence_length_variance = 0

    punctuation_count = len(re.findall(r"[,:;!?]", text))
    punctuation_density = punctuation_count / word_count

    score = 0.0

    if sentence_length_variance < 12:
        score += 0.35
    elif sentence_length_variance < 25:
        score += 0.2
    else:
        score += 0.05

    if type_token_ratio < 0.55:
        score += 0.3
    elif type_token_ratio < 0.7:
        score += 0.15
    else:
        score += 0.05

    if 12 <= avg_sentence_length <= 25:
        score += 0.2
    else:
        score += 0.1

    if punctuation_density < 0.08:
        score += 0.15
    else:
        score += 0.05

    score = max(0.0, min(1.0, score))

    return {
        "score": round(score, 3),
        "metrics": {
            "word_count": word_count,
            "type_token_ratio": round(type_token_ratio, 3),
            "avg_sentence_length": round(avg_sentence_length, 3),
            "sentence_length_variance": round(sentence_length_variance, 3),
            "punctuation_density": round(punctuation_density, 3)
        }
    }


def combine_scores(llm_score, stylometric_score):
    return round((llm_score * 0.6) + (stylometric_score * 0.4), 3)


def get_attribution(confidence):
    if confidence >= 0.7:
        return "likely_ai"
    if confidence <= 0.3:
        return "likely_human"
    return "uncertain"


def get_label(attribution, confidence):
    if attribution == "likely_ai":
        return (
            f"This content appears likely to be AI-generated based on multiple "
            f"detection signals. Confidence: {confidence}."
        )

    if attribution == "likely_human":
        return (
            f"This content appears likely to be human-written based on multiple "
            f"detection signals. Confidence: {confidence}."
        )

    return (
        f"We are not confident enough to determine whether this content was "
        f"AI-generated or human-written. This result should be treated as "
        f"uncertain. Confidence: {confidence}."
    )


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "message": "Provenance Guard backend is running",
        "endpoints": ["/submit", "/appeal", "/log"]
    })


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True) or {}

    text = data.get("text", "").strip()
    creator_id = data.get("creator_id", "").strip()

    if not text or not creator_id:
        return jsonify({
            "error": "Both text and creator_id are required."
        }), 400

    content_id = str(uuid.uuid4())

    llm_result = groq_signal(text)
    stylometric_result = stylometric_signal(text)

    llm_score = llm_result["score"]
    stylometric_score = stylometric_result["score"]

    confidence = combine_scores(llm_score, stylometric_score)
    attribution = get_attribution(confidence)
    label = get_label(attribution, confidence)

    log_entry = {
        "event_type": "submission",
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": now_utc(),
        "attribution": attribution,
        "confidence": confidence,
        "llm_score": llm_score,
        "llm_reasoning": llm_result["reasoning"],
        "stylometric_score": stylometric_score,
        "stylometric_metrics": stylometric_result["metrics"],
        "status": "classified",
        "appeal_filed": False
    }

    add_log_entry(log_entry)

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": confidence,
        "label": label,
        "signals": {
            "llm_score": llm_score,
            "stylometric_score": stylometric_score
        },
        "status": "classified"
    })


@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json(silent=True) or {}

    content_id = data.get("content_id", "").strip()
    creator_reasoning = data.get("creator_reasoning", "").strip()

    if not content_id or not creator_reasoning:
        return jsonify({
            "error": "Both content_id and creator_reasoning are required."
        }), 400

    entries = read_log()

    matching_submission = None
    for entry in entries:
        if entry.get("content_id") == content_id and entry.get("event_type") == "submission":
            matching_submission = entry
            entry["status"] = "under_review"
            entry["appeal_filed"] = True

    if not matching_submission:
        return jsonify({
            "error": "No matching content_id found."
        }), 404

    appeal_entry = {
        "event_type": "appeal",
        "content_id": content_id,
        "creator_id": matching_submission.get("creator_id"),
        "timestamp": now_utc(),
        "original_attribution": matching_submission.get("attribution"),
        "original_confidence": matching_submission.get("confidence"),
        "creator_reasoning": creator_reasoning,
        "status": "under_review"
    }

    entries.append(appeal_entry)
    write_log(entries)

    return jsonify({
        "message": "Appeal received.",
        "content_id": content_id,
        "status": "under_review"
    })


@app.route("/log", methods=["GET"])
def get_log():
    entries = read_log()
    return jsonify({
        "entries": entries[-20:]
    })


if __name__ == "__main__":
    app.run(debug=True)