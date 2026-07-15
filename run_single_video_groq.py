"""One-off local runner: summarizes a single YouTube video via Groq instead
of Anthropic (app/summarize.py is wired to the Anthropic SDK's structured-
output feature, which Groq doesn't support the same way). Reuses this repo's
transcript-fetching and prompt-building logic; only the model call itself is
swapped out. Not part of the deployed pipeline - a local experiment only.

Groq's free tier caps a request at 12,000 tokens/minute, which a long
video's full transcript can exceed on its own. When that happens, the
transcript is split into sequential chunks (each under the cap), each
chunk is summarized independently, and the per-chunk points/summaries are
compiled into one final result covering the whole video - see
chunk_transcript() and the main() loop below.

Usage:
    GROQ_API_KEY=... python run_single_video_groq.py <video_url_or_id>
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from typing import Literal

import groq
import pydantic
from groq import Groq
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

GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_OUTPUT_TOKENS = 2048
MAX_JSON_REPAIR_ATTEMPTS = 2
# Groq's free tier caps requests at 12,000 tokens/minute total (prompt +
# completion). At ~4 chars/token this keeps a single chunk's whole request
# (system prompt + schema + transcript slice + reserved completion)
# comfortably under that ceiling.
MAX_CHUNK_CHARS = 18000
# Spacing between chunk calls so consecutive requests don't stack up
# against the same rolling 60s TPM window.
CHUNK_DELAY_SECONDS = 20
RATE_LIMIT_MAX_RETRIES = 4


class SynthesisOutput(BaseModel):
    """Small final-pass schema: given every chunk's points, write one
    cohesive summary of the video as a whole and pick a single overall
    classification - cheap enough to never itself risk the rate limit,
    unlike re-sending the full transcript again."""

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
    """Splits the transcript into consecutive chunks, each under max_chars,
    always breaking on a line boundary so a timestamped line is never cut
    in half (which would break source_timestamp/source_anchor grounding)."""

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


def call_groq_structured(client: Groq, system_prompt: str, user_content: str, schema_model):
    schema_json = json.dumps(schema_model.model_json_schema())
    messages = [
        {
            "role": "system",
            "content": system_prompt
            + "\n\nRespond with ONLY a single JSON object (no markdown fences, no commentary) matching this "
            f"JSON Schema exactly:\n{schema_json}",
        },
        {"role": "user", "content": user_content},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_JSON_REPAIR_ATTEMPTS + 2):
        response = call_groq_with_rate_limit_retry(client, messages)
        raw = response.choices[0].message.content
        try:
            data = json.loads(raw)
            return schema_model(**data), response.usage
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

    raise RuntimeError(f"Groq never returned valid structured output: {last_error}")


def call_groq_with_rate_limit_retry(client: Groq, messages: list[dict]):
    for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
                response_format={"type": "json_object"},
                messages=messages,
            )
        except groq.RateLimitError as exc:
            wait_seconds = 20 * attempt
            print(f"    rate limited (attempt {attempt}/{RATE_LIMIT_MAX_RETRIES}); waiting {wait_seconds}s: {exc}", file=sys.stderr)
            time.sleep(wait_seconds)
    raise RuntimeError(f"Groq rate limit did not clear after {RATE_LIMIT_MAX_RETRIES} retries.")


def format_seconds(seconds: int) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def summarize_video(client: Groq, transcript_body: str) -> tuple[str, list, dict]:
    """Runs the full chunk-and-compile pipeline for one transcript,
    returning (final_summary, resolved_points, usage_totals)."""

    video_types = FALLBACK_VIDEO_TYPES
    overall_max_points = _max_points_for_duration(_max_transcript_seconds(transcript_body))
    chunks = chunk_transcript(transcript_body, MAX_CHUNK_CHARS)

    total_prompt_tokens = 0
    total_completion_tokens = 0
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
        parsed, usage = call_groq_structured(client, system_prompt, chunk, schema_model)

        all_points.extend(parsed.points)
        chunk_summaries.append(parsed.summary)
        chunk_video_types.append(parsed.video_type)
        if usage:
            total_prompt_tokens += usage.prompt_tokens
            total_completion_tokens += usage.completion_tokens

        if i < len(chunks):
            time.sleep(CHUNK_DELAY_SECONDS)

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
        synthesis, usage = call_groq_structured(client, synthesis_prompt, segments_text, SynthesisOutput)
        final_summary = synthesis.summary
        final_video_type = synthesis.video_type or majority_video_type
        if usage:
            total_prompt_tokens += usage.prompt_tokens
            total_completion_tokens += usage.completion_tokens

    usage_totals = {"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens}
    return final_video_type, final_summary, resolved_points, usage_totals


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python run_single_video_groq.py <video_url_or_id>", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("GROQ_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    video_id = extract_video_id(sys.argv[1])
    print(f"Fetching transcript for {video_id}...")
    title, transcript_body = build_transcript_body(video_id)
    print(f"Title: {title} ({len(transcript_body)} chars)")

    client = Groq(api_key=api_key)
    video_type, summary, resolved_points, usage = summarize_video(client, transcript_body)

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
        "usage": usage,
        "model": GROQ_MODEL,
    }
    out_path = f"summary_{video_id}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
