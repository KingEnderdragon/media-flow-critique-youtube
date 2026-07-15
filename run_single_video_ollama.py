"""One-off local runner: summarizes a single YouTube video via a local
Ollama model instead of Groq/Anthropic - fully local inference, no API key,
no per-minute/per-day rate limits (see run_single_video_groq.py's docstring
for why Groq needed chunking+pacing in the first place; those constraints
don't apply here, only local compute time does).

Requires Ollama running locally (`ollama serve`, or the desktop app) with
a model already pulled - defaults to mistral-small, chosen after comparing
it against qwen2.5:14b/32b and deepseek-r1:14b (see the Summary Engine
Comparisons artifact / conversation history): best citation-grounding
accuracy and most detailed per-point analysis of the models tried.

Usage:
    python run_single_video_ollama.py <video_url_or_id> [model]

model defaults to mistral-small; pass e.g. deepseek-r1:14b to compare a
reasoning model instead. Reasoning models emit a <think>...</think> block
ahead of their answer even under schema-constrained decoding - that block
is stripped before JSON parsing, see strip_think_block() below.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from typing import Literal

import requests
from pydantic import BaseModel, Field, ValidationError

from app.summarize import (
    FALLBACK_VIDEO_TYPE_DESCRIPTIONS,
    FALLBACK_VIDEO_TYPES,
    SummaryPoint,
    _build_model_output_schema,
    _build_system_prompt,
    _index_transcript_lines,
    _max_points_for_duration,
    _max_transcript_seconds,
    _resolve_points,
    _select_significant_points,
    strip_frontmatter,
)
from app.youtube import (
    extract_video_id,
    fetch_transcript,
    fetch_video_metadata,
    render_transcript_markdown,
)

OLLAMA_URL = "http://localhost:11434/api/chat"
# mistral-small was chosen over qwen2.5:14b/32b and deepseek-r1:14b after a
# head-to-head comparison across several videos - best grounding accuracy
# and most detailed per-point analysis, at an acceptable speed cost from
# not fully fitting in 15.9GB VRAM.
DEFAULT_OLLAMA_MODEL = "mistral-small"
OLLAMA_MODEL = DEFAULT_OLLAMA_MODEL
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# Local inference has no token-per-minute/day ceiling to dodge - this is
# sized for speed and context-window headroom (num_ctx below), not a rate
# limit. Still chunk long transcripts: keeping the model's working context
# smaller keeps generation fast and reduces the chance it loses track of
# the citation-grounding instructions over a very long input.
MAX_CHUNK_CHARS = 30000
NUM_CTX = 16384
MAX_JSON_REPAIR_ATTEMPTS = 2
# Generous ceiling: a model that doesn't fully fit in VRAM (e.g. a 32B
# model against 16GB) partially offloads to CPU, which can push a single
# chunk's generation time well past 10 minutes.
REQUEST_TIMEOUT_SECONDS = 2400


class SynthesisOutput(BaseModel):
    summary: str = Field(min_length=1)
    video_type: Literal[tuple(FALLBACK_VIDEO_TYPES)]


def build_transcript_body(video_id: str) -> tuple[str, str]:
    metadata = fetch_video_metadata(video_id)
    transcript = fetch_transcript(video_id, languages=["en"])
    if transcript.status != "ok":
        raise RuntimeError(f"Could not fetch transcript for {video_id}: {transcript.status} - {transcript.message}")

    markdown = render_transcript_markdown(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=metadata.title,
        author=metadata.author,
        fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        language=transcript.language,
        language_code=transcript.language_code,
        is_generated=transcript.is_generated,
        lines=transcript.lines,
    )
    return metadata.title, strip_frontmatter(markdown)


def chunk_transcript(transcript_body: str, max_chars: int) -> list[str]:
    lines = transcript_body.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        added_len = len(line) + 1
        if current and current_len + added_len > max_chars:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += added_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def call_ollama_structured(system_prompt: str, user_content: str, schema_model):
    """Uses Ollama's structured-output support (format=<JSON Schema>), which
    constrains decoding to valid JSON matching the schema - unlike Groq's
    json_object mode this doesn't need a prose-schema-in-the-prompt hack,
    but a repair retry is kept anyway since constrained JSON doesn't
    guarantee every field satisfies pydantic's own semantic constraints
    (e.g. min_length)."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_JSON_REPAIR_ATTEMPTS + 2):
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "format": schema_model.model_json_schema(),
                "stream": False,
                "options": {"temperature": 0, "num_ctx": NUM_CTX},
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        raw = response.json()["message"]["content"]
        cleaned = _THINK_BLOCK_RE.sub("", raw).strip()
        try:
            data = json.loads(cleaned)
            return schema_model(**data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            print(f"    attempt {attempt}: invalid output ({exc}); retrying...", file=sys.stderr)
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": f"That response was invalid ({exc}). Reply again with ONLY the corrected JSON object.",
                }
            )

    raise RuntimeError(f"Ollama never returned valid structured output: {last_error}")


def format_seconds(seconds: int) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def summarize_video(transcript_body: str) -> tuple[str, str, list]:
    video_types = FALLBACK_VIDEO_TYPES
    overall_max_points = _max_points_for_duration(_max_transcript_seconds(transcript_body))
    chunks = chunk_transcript(transcript_body, MAX_CHUNK_CHARS)

    all_points: list[SummaryPoint] = []
    chunk_summaries: list[str] = []
    chunk_video_types: list[str] = []

    for i, chunk in enumerate(chunks, start=1):
        indexed = _index_transcript_lines(chunk)
        chunk_span_seconds = (indexed[-1][0] - indexed[0][0]) if indexed else 0
        chunk_max_points = _max_points_for_duration(chunk_span_seconds)

        print(f"  chunk {i}/{len(chunks)} ({len(chunk)} chars, ~{chunk_span_seconds // 60} min span)...")
        system_prompt = _build_system_prompt(chunk_max_points, video_types, FALLBACK_VIDEO_TYPE_DESCRIPTIONS)
        schema_model = _build_model_output_schema(video_types)
        parsed = call_ollama_structured(system_prompt, chunk, schema_model)

        all_points.extend(parsed.points)
        chunk_summaries.append(parsed.summary)
        chunk_video_types.append(parsed.video_type)

    resolved_points = _resolve_points(all_points, transcript_body)
    resolved_points, _was_truncated = _select_significant_points(resolved_points, overall_max_points)

    if len(chunks) == 1:
        final_summary, final_video_type = chunk_summaries[0], chunk_video_types[0]
    else:
        print("  synthesizing final summary across all chunks...")
        majority_video_type = max(set(chunk_video_types), key=chunk_video_types.count)
        synthesis_prompt = (
            "You are given per-segment summaries of consecutive parts of a single YouTube video, in order. "
            "Write one cohesive 2 to 4 sentence summary of the video as a whole, and classify it as exactly one "
            f"of {', '.join(video_types)}. Do not mention 'segments' or 'parts' - describe the video's content directly."
        )
        segments_text = "\n\n".join(f"Segment {i}: {s}" for i, s in enumerate(chunk_summaries, start=1))
        synthesis = call_ollama_structured(synthesis_prompt, segments_text, SynthesisOutput)
        final_summary = synthesis.summary
        final_video_type = synthesis.video_type or majority_video_type

    return final_video_type, final_summary, resolved_points


def main() -> None:
    global OLLAMA_MODEL
    if len(sys.argv) not in (2, 3):
        print("Usage: python run_single_video_ollama.py <video_url_or_id> [model]", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) == 3:
        OLLAMA_MODEL = sys.argv[2]

    video_id = extract_video_id(sys.argv[1])
    print(f"Fetching transcript for {video_id}...")
    print(f"Model: {OLLAMA_MODEL}")
    title, transcript_body = build_transcript_body(video_id)
    print(f"Title: {title} ({len(transcript_body)} chars)")

    video_type, summary, resolved_points = summarize_video(transcript_body)

    print("\n" + "=" * 70)
    print(f"TITLE: {title}")
    print(f"TYPE:  {video_type}")
    print("=" * 70)
    print(f"\n{summary}\n")
    print("-" * 70)
    for point in resolved_points:
        ts = format_seconds(point.timestamp_seconds) if point.timestamp_seconds is not None else "??:??"
        tag = "MAJOR" if point.importance == "major" else "minor"
        print(f"\n[{ts}] ({tag}) {point.main_point}")
        print(f"  {point.explanation}")

    output = {
        "video_id": video_id,
        "title": title,
        "video_type": video_type,
        "summary": summary,
        "points": [
            {
                "importance": p.importance,
                "main_point": p.main_point,
                "explanation": p.explanation,
                "timestamp_seconds": p.timestamp_seconds,
            }
            for p in resolved_points
        ],
        "model": OLLAMA_MODEL,
    }
    model_slug = OLLAMA_MODEL.replace(":", "-").replace("/", "-")
    out_path = f"summary_{video_id}_{model_slug}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
