"""Local "dossier" runner: evolves run_single_video_ollama.py's
summary+points output into an archive-style page modelled on
zizek.org/talk/* (e.g. /talk/a-plea-for-bureaucratic-socialism):

    title / meta line / tags / prose summary / located-moment timeline /
    embed / ideas / stories / works / people / what it argues /
    moves / claims / quotes / related / transcript

Same local stack as run_single_video_ollama.py - Ollama + mistral-small,
schema-constrained JSON, chunked transcript - and the same grounding rule:
every timestamp shown on the page is the model's copied "[MM:SS]" +
verbatim anchor, checked by app.summarize._resolve_points(); a failed check
hides the timestamp instead of trusting it. Verbatim quotes get a stricter
check (see QUOTE_MATCH_THRESHOLD) and are dropped outright if they don't
match the transcript, since a fabricated quote is worse than a missing one.

Usage:
    python run_video_dossier_ollama.py <video_url_or_id> [model]
    python run_video_dossier_ollama.py --render <dossier_json>   # re-render HTML only

Writes dossier_<id>_<model>.json and dossier_<id>_<model>.html. "Related"
is computed against every other dossier_*.json in the same folder.
"""

from __future__ import annotations

import datetime
import time
import glob
import html
import json
import os
import re
import sys
from typing import Literal

import requests
from pydantic import BaseModel, Field, ValidationError

import run_single_video_ollama as base
from app.summarize import (
    SummaryPoint,
    _index_transcript_lines,
    _resolve_points,
    _significant_words,
    strip_frontmatter,
)
from app.youtube import extract_video_id, fetch_transcript, fetch_video_metadata, render_transcript_markdown

# Smaller than base.MAX_CHUNK_CHARS: each chunk now gets two extraction
# passes with much larger outputs, so leave more of NUM_CTX for the answer.
MAX_CHUNK_CHARS = 20000
QUOTE_MATCH_THRESHOLD = 0.6  # share of a quote's significant words found near its cited line
QUOTE_WINDOW_LINES = 4
TRANSCRIPT_PARAGRAPH_SECONDS = 45
# Hard output ceiling per call. mistral-small only part-fits a 12GB card
# (~40% CPU offload, a few tok/s), and an unbounded structured-JSON call
# once ran past the 40-minute request timeout - the schema's max_length
# bounds plus this cap keep every call finite.
NUM_PREDICT = 3500
REQUEST_TIMEOUT_SECONDS = 1800
# At temperature 0 mistral-small tends to loop a stock phrase ("...is a key
# part of the game's mechanics...") until a field's max_length cuts it off.
REPEAT_PENALTY = 1.15

FORMATS = ["Talk", "Lecture", "Tutorial", "Review", "Essay", "Devlog", "Interview", "Podcast", "Livestream", "Other"]


# --------------------------------------------------------------------------- schemas


class Idea(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="concept name, Title Case")
    alias: str = Field(max_length=100, description="the exact phrase the speaker uses for it, verbatim")
    related: str = Field(max_length=80, description="one closely related concept, or empty")
    description: str = Field(min_length=1, max_length=500)
    source_timestamp: str = Field(max_length=12)
    source_anchor: str = Field(max_length=200)


class Story(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=500)
    quote: str = Field(max_length=300, description="a short verbatim line from the story, or empty")
    source_timestamp: str = Field(max_length=12)
    source_anchor: str = Field(max_length=200)


class Move(BaseModel):
    statement: str = Field(min_length=1, max_length=300, description="verbatim line where the reversal/turn happens")
    explanation: str = Field(min_length=1, max_length=500)
    source_timestamp: str = Field(max_length=12)


class Quote(BaseModel):
    text: str = Field(min_length=1, max_length=300, description="verbatim transcript words")
    topic: str = Field(min_length=1, max_length=120)
    source_timestamp: str = Field(max_length=12)


class MomentsOutput(BaseModel):
    ideas: list[Idea] = Field(max_length=12)
    stories: list[Story] = Field(max_length=6)
    moves: list[Move] = Field(max_length=5)
    quotes: list[Quote] = Field(max_length=8)


class Work(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["film", "tv", "book", "game", "music", "article", "video", "product", "other"]
    description: str = Field(min_length=1, max_length=500)


class Person(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sentiment: Literal["positive", "negative", "neutral"]
    description: str = Field(min_length=1, max_length=500)


class Claim(BaseModel):
    verb: Literal["asserts", "reframes", "rejects", "predicts", "recommends", "questions"]
    claim: str = Field(min_length=1, max_length=500)
    topic: str = Field(min_length=1, max_length=120)


class ReferencesOutput(BaseModel):
    segment_summary: str = Field(min_length=1, max_length=500)
    works: list[Work] = Field(max_length=15)
    people: list[Person] = Field(max_length=15)
    claims: list[Claim] = Field(max_length=10)
    themes: list[str] = Field(max_length=6)


class SynthesisOutput(BaseModel):
    summary: str = Field(min_length=1, max_length=1200)
    format: Literal[tuple(FORMATS)]
    tags: list[str] = Field(min_length=1, max_length=7)
    themes: list[str] = Field(min_length=1, max_length=10)


# --------------------------------------------------------------------------- prompts

_CITATION_RULES = """Every transcript line starts with its timestamp in brackets, e.g. "[14:32] ...".
"source_timestamp" must be copied verbatim (brackets included) from ONE real transcript line - never computed or estimated.
"source_anchor" must be a few words copied verbatim from that same line."""

MOMENTS_PROMPT = f"""You are building an archive entry for a segment of a YouTube video transcript, like a scholarly index of a talk.
Extract, using only what the transcript supports:

- "ideas": the distinct concepts, frameworks, mechanics or terms the speaker works with (up to {{max_ideas}}). "name" is a clean concept name; "alias" is the exact phrase the speaker uses; "related" is one neighbouring concept (or ""); "description" is 1-2 sentences on how the speaker uses it HERE.
- "stories": anecdotes, examples, jokes, hypotheticals or scenarios the speaker tells to make a point (up to {{max_stories}}). "title" is a short descriptive title, "summary" 1-2 sentences including what point it serves, "quote" a short verbatim line from it (or "").
- "moves": turns in the argument - places where the speaker reverses an expectation, overturns a common assumption, or reframes something as its opposite (up to {{max_moves}}). "statement" is the speaker's own words at that turn, copied from the transcript; "explanation" says in plain language what is being reversed. If there are no such turns, return an empty list.
- "quotes": the most quotable lines, copied word-for-word (up to {{max_quotes}}), each with a 2-5 word lowercase "topic".

{_CITATION_RULES}
For moves and quotes, "statement"/"text" must themselves be verbatim transcript words - they are checked against the transcript and discarded if not found.
Skip channel housekeeping (sponsors, like/subscribe, schedule). Return empty lists rather than inventing content."""

REFERENCES_PROMPT = """You are building an archive entry for a segment of a YouTube video transcript.
Extract, using only what the transcript supports:

- "segment_summary": 2-3 sentences on what this segment argues or covers.
- "works": named films, shows, books, games, songs, articles, videos or products the speaker invokes, each with "kind" and a one-line "description" of how it is used.
- "people": named people (real or fictional) the speaker invokes, with "sentiment" toward them ("positive", "negative", or "neutral") and a one-line "description" of their role in the argument.
- "claims": the speaker's substantive claims (up to {max_claims}), each with a "verb" (asserts / reframes / rejects / predicts / recommends / questions), the "claim" in one plain sentence, and a 2-5 word lowercase "topic".
- "themes": 3-6 short lowercase phrases (no punctuation) naming what this segment argues.

Only include works and people that are actually named. Skip channel housekeeping. Return empty lists rather than inventing content."""

SYNTHESIS_PROMPT = f"""You are given segment summaries and themes from consecutive parts of one YouTube video, in order.
Write:
- "summary": one cohesive prose paragraph of 4-6 sentences describing the video's argument and scope as a whole - what it sets out to do, how it proceeds, what it concludes. No bullet points; don't mention "segments".
- "format": exactly one of {", ".join(FORMATS)}.
- "tags": 3-7 lowercase-hyphenated topic tags (e.g. "game-design", "tabletop-rpg").
- "themes": 5-10 short lowercase phrases, no punctuation, naming what the video argues overall (merge duplicates)."""


# --------------------------------------------------------------------------- grounding


def resolve_ts(source_timestamp: str, source_anchor: str, transcript_body: str) -> int | None:
    """Runs one citation through the same anchor gate the summary points use;
    if the cited second is off (common on auto-captions, where a line is
    only ~2s long) but the verbatim anchor is still found in the transcript,
    falls back to where the anchor actually is - same rule as quotes."""
    point = SummaryPoint(
        importance="minor", main_point="-", explanation="-",
        source_timestamp=clean_ts(source_timestamp) or "-", source_anchor=source_anchor or "-",
    )
    t = _resolve_points([point], transcript_body)[0].timestamp_seconds
    if t is None and source_anchor:
        t = verify_quote(source_anchor, source_timestamp, _index_transcript_lines(transcript_body))
    return t


def verify_quote(text: str, source_timestamp: str, indexed: list[tuple[int, str]]) -> int | None:
    """Returns the quote's timestamp if its words are actually found near the
    cited line (or, failing that, anywhere in a sliding window of the
    transcript - models often cite the wrong second for a real quote).
    None means the words aren't in the transcript: drop the quote."""
    words = _significant_words(text)
    if len(words) < 2:
        return None

    def window_score(i: int) -> float:
        lo, hi = max(0, i - QUOTE_WINDOW_LINES), min(len(indexed), i + QUOTE_WINDOW_LINES + 1)
        found = _significant_words(" ".join(t for _, t in indexed[lo:hi]))
        return len(words & found) / len(words)

    cited = base_parse_ts(source_timestamp)
    if cited is not None:
        for i, (sec, _) in enumerate(indexed):
            if sec == cited and window_score(i) >= QUOTE_MATCH_THRESHOLD:
                return cited
    best_i, best = None, 0.0
    for i in range(len(indexed)):
        s = window_score(i)
        if s > best:
            best_i, best = i, s
    if best_i is not None and best >= QUOTE_MATCH_THRESHOLD:
        return indexed[best_i][0]
    return None


_TS_RE = re.compile(r"^\[?(?:(\d+):)?(\d{1,2}):(\d{2})\]?$")
_TS_SEARCH_RE = re.compile(r"\[?(?:\d+:)?\d{1,2}:\d{2}\]?")


def clean_ts(raw: str) -> str:
    """mistral-small often runs on past the bracket ("[01:03] cin") -
    keep just the bracketed timestamp it led with."""
    m = _TS_SEARCH_RE.search(raw or "")
    return m.group(0) if m else (raw or "")


def base_parse_ts(raw: str) -> int | None:
    m = _TS_RE.match(clean_ts(raw).strip())
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


# --------------------------------------------------------------------------- extraction


def call_structured(system_prompt: str, user_content: str, schema_model):
    """base.call_ollama_structured() plus an output-token cap and timing."""
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
    last_error: Exception | None = None
    for attempt in range(1, base.MAX_JSON_REPAIR_ATTEMPTS + 2):
        started = time.time()
        response = requests.post(
            base.OLLAMA_URL,
            json={
                "model": base.OLLAMA_MODEL,
                "messages": messages,
                "format": schema_model.model_json_schema(),
                "stream": False,
                "options": {"temperature": 0, "num_ctx": base.NUM_CTX, "num_predict": NUM_PREDICT,
                            "repeat_penalty": REPEAT_PENALTY},
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        print(f"    {body.get('eval_count', '?')} tokens in {time.time() - started:.0f}s")
        raw = body["message"]["content"]
        cleaned = base._THINK_BLOCK_RE.sub("", raw).strip()
        try:
            return schema_model(**json.loads(cleaned))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            print(f"    attempt {attempt}: invalid output ({str(exc)[:200]}); retrying...", file=sys.stderr)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"That response was invalid ({exc}). Reply again with ONLY the corrected, complete JSON object, keeping it shorter."})
    raise RuntimeError(f"Ollama never returned valid structured output: {last_error}")


def fetch(video_id: str):
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
    return metadata, transcript.lines, strip_frontmatter(markdown)


def _dedupe(items: list[dict], key: str) -> list[dict]:
    seen: dict[str, dict] = {}
    for item in items:
        k = re.sub(r"[^a-z0-9]+", " ", item[key].lower()).strip()
        if k and k not in seen:
            seen[k] = item
    return list(seen.values())


def extract(transcript_body: str, checkpoint_path: str | None = None) -> dict:
    """checkpoint_path caches each model call's raw output (kept after the run:
    re-running re-applies grounding + rendering with no new model calls;
    delete the file, or a key in it, to force fresh extraction), so a run killed
    mid-way (e.g. by memory pressure - mistral-small spills ~7GB into RAM on
    a 12GB card) resumes from the last finished call instead of starting over."""
    cache: dict = {}
    if checkpoint_path and os.path.exists(checkpoint_path):
        cache = json.load(open(checkpoint_path, encoding="utf-8"))
        print(f"  resuming from {checkpoint_path} ({len(cache)} cached calls)")

    def cached(key: str, prompt: str, content: str, schema):
        if key in cache:
            print(f"    {key}: cached")
            return schema(**cache[key])
        result = call_structured(prompt, content, schema)
        cache[key] = result.model_dump()
        if checkpoint_path:
            with open(checkpoint_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        return result

    indexed_all = _index_transcript_lines(transcript_body)
    chunks = base.chunk_transcript(transcript_body, MAX_CHUNK_CHARS)
    ideas, stories, moves, quotes, works, people, claims, themes, seg_summaries = ([] for _ in range(9))

    for i, chunk in enumerate(chunks, start=1):
        indexed = _index_transcript_lines(chunk)
        span_min = max(1, ((indexed[-1][0] - indexed[0][0]) if indexed else 0) // 60)
        caps = dict(
            max_ideas=min(12, 3 + span_min // 3), max_stories=min(6, 1 + span_min // 6),
            max_moves=min(5, 1 + span_min // 8), max_quotes=min(8, 2 + span_min // 5),
        )
        print(f"  chunk {i}/{len(chunks)} (~{span_min} min): moments...")
        m = cached(f"moments-{i}", MOMENTS_PROMPT.format(**caps), chunk, MomentsOutput)
        print(f"  chunk {i}/{len(chunks)}: references...")
        r = cached(
            f"references-{i}", REFERENCES_PROMPT.format(max_claims=min(10, 3 + span_min // 3)), chunk, ReferencesOutput
        )

        for x in m.ideas:
            ideas.append({"name": x.name, "alias": x.alias, "related": x.related, "description": x.description,
                          "t": resolve_ts(x.source_timestamp, x.source_anchor, transcript_body)})
        for x in m.stories:
            q_t = verify_quote(x.quote, x.source_timestamp, indexed_all) if x.quote else None
            stories.append({"title": x.title, "summary": x.summary, "quote": x.quote if q_t is not None else "",
                            "t": resolve_ts(x.source_timestamp, x.source_anchor, transcript_body)})
        for x in m.moves:
            t = verify_quote(x.statement, x.source_timestamp, indexed_all)
            if t is not None:
                moves.append({"statement": x.statement, "explanation": x.explanation, "t": t})
        for x in m.quotes:
            t = verify_quote(x.text, x.source_timestamp, indexed_all)
            if t is not None:
                quotes.append({"text": x.text, "topic": x.topic, "t": t})
            else:
                print(f"    dropped unverifiable quote: {x.text[:60]!r}")
        works += [w.model_dump() for w in r.works]
        people += [p.model_dump() for p in r.people]
        claims += [c.model_dump() for c in r.claims]
        themes += r.themes
        seg_summaries.append(r.segment_summary)

    print("  synthesizing summary, tags, themes...")
    synth_input = "\n\n".join(f"Segment {i}: {s}" for i, s in enumerate(seg_summaries, 1))
    synth_input += "\n\nThemes noted: " + "; ".join(themes)
    s = cached("synthesis", SYNTHESIS_PROMPT, synth_input, SynthesisOutput)

    return {
        "summary": s.summary,
        "format": s.format,
        "tags": [re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") for t in s.tags],
        "themes": [t.lower().strip(" .") for t in s.themes],
        "ideas": sorted(_dedupe(ideas, "name"), key=lambda x: (x["t"] is None, x["t"] or 0)),
        "stories": sorted(_dedupe(stories, "title"), key=lambda x: (x["t"] is None, x["t"] or 0)),
        "moves": sorted(moves, key=lambda x: (x["t"] is None, x["t"] or 0)),
        "quotes": sorted(_dedupe(quotes, "text"), key=lambda x: (x["t"] is None, x["t"] or 0)),
        "works": _dedupe(works, "title"),
        "people": _dedupe(people, "name"),
        "claims": _dedupe(claims, "claim"),
    }


# --------------------------------------------------------------------------- related


def _fingerprint(d: dict) -> set[str]:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return ({"tag:" + t for t in d.get("tags", [])}
            | {"idea:" + norm(x["name"]) for x in d.get("ideas", [])}
            | {"person:" + norm(x["name"]) for x in d.get("people", [])}
            | {"work:" + norm(x["title"]) for x in d.get("works", [])})


def find_related(d: dict, folder: str, limit: int = 6) -> list[dict]:
    mine = _fingerprint(d)
    out = []
    for path in glob.glob(os.path.join(folder, "dossier_*.json")):
        try:
            other = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if other.get("video_id") == d["video_id"]:
            continue
        theirs = _fingerprint(other)
        shared = mine & theirs
        if not shared:
            continue
        out.append({
            "title": other["title"], "href": os.path.basename(path)[:-5] + ".html",
            "similarity": round(100 * len(shared) / len(mine | theirs)),
            "shared": sorted(x.split(":", 1)[1] for x in shared)[:6],
        })
    return sorted(out, key=lambda x: -x["similarity"])[:limit]


# --------------------------------------------------------------------------- render

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def tidy(text: str) -> str:
    """Drops repeated sentences and a trailing fragment cut off by a field's
    max_length - render-time, so already-extracted dossiers clean up on
    --render without new model calls."""
    sentences, seen = [], []
    for sentence in _SENTENCE_RE.split((text or "").strip()):
        words = _significant_words(sentence)
        # near-duplicates too: the loops vary filler ("explains" / "also notes")
        if sentence and not any(len(words & w) / max(1, len(words | w)) >= 0.6 for w in seen):
            seen.append(words)
            sentences.append(sentence)
    if len(sentences) > 1 and not sentences[-1].rstrip().endswith((".", "!", "?", '"', "”")):
        sentences.pop()
    return " ".join(sentences)


def fmt(seconds: int | None) -> str:
    return base.format_seconds(seconds) if seconds is not None else ""


def render_html(d: dict, folder: str) -> str:
    e = lambda s: html.escape(str(s or ""))  # noqa: E731
    vid, duration = d["video_id"], max(1, d["duration_seconds"])
    yt = f"https://www.youtube.com/watch?v={vid}"
    speaker = d.get("author") or "speaker"

    # transcript paragraphs, each anchored at its start second
    paras, cur, cur_t = [], [], None
    for start, text in d["transcript_lines"]:
        if cur_t is None:
            cur_t = int(start)
        if start - cur_t >= TRANSCRIPT_PARAGRAPH_SECONDS and cur:
            paras.append((cur_t, " ".join(cur)))
            cur, cur_t = [], int(start)
        cur.append(text.replace("\n", " "))
    if cur:
        paras.append((cur_t, " ".join(cur)))
    para_starts = [p[0] for p in paras]

    def para_for(t: int) -> int:
        best = 0
        for i, s in enumerate(para_starts):
            if s <= t:
                best = i
        return best

    def ts_link(t: int | None) -> str:
        if t is None:
            return '<span class="when"><span class="ts muted">—</span></span>'
        # one wrapper element: .row is a 2-column grid, so the timestamp and
        # its ↗ link must occupy a single cell or the text gets pushed down
        return (f'<span class="when"><a class="ts" href="#p{para_for(t)}" data-p="{para_for(t)}">{fmt(t)}</a>'
                f'<a class="yt" href="{yt}&t={t}s" target="_blank" rel="noopener" title="open at {fmt(t)} on YouTube">↗</a></span>')

    # located-moment timeline
    kinds = [("idea", d["ideas"], "name"), ("story", d["stories"], "title"),
             ("move", d["moves"], "statement"), ("quote", d["quotes"], "text")]
    dots = []
    for kind, items, label in kinds:
        for x in items:
            if x.get("t") is not None:
                pct = 100 * x["t"] / duration
                dots.append(f'<a class="dot {kind}" style="left:{pct:.2f}%" href="#p{para_for(x["t"])}" '
                            f'data-p="{para_for(x["t"])}" title="{fmt(x["t"])} · {kind}: {e(x[label][:90])}"></a>')
    step = 60 if duration <= 600 else 300 if duration <= 3600 else 1800
    ticks = "".join(f'<span class="tick" style="left:{100 * t / duration:.2f}%">{fmt(t)}</span>'
                    for t in range(0, duration, step))

    def section(sid: str, title: str, count: int | None, body: str) -> str:
        if not body.strip():
            return ""
        c = f' <span class="count">{count}</span>' if count is not None else ""
        return f'<section id="{sid}"><h2>{title}{c}</h2>{body}</section>'

    ideas = "".join(
        f'<div class="row">{ts_link(x["t"])}<div><div class="name">{e(x["name"])}'
        + (f' <span class="alias">as “{e(x["alias"])}”</span>' if x["alias"] and x["alias"].lower() != x["name"].lower() else "")
        + f'</div><div class="sub">Concept{(" · " + e(x["related"])) if x["related"] else ""}</div>'
        f'<p>{e(tidy(x["description"]))}</p></div></div>' for x in d["ideas"])
    stories = "".join(
        f'<div class="row">{ts_link(x["t"])}<div><div class="name">{e(x["title"])}</div><p>{e(tidy(x["summary"]))}</p>'
        + (f'<p class="pull">“{e(x["quote"])}”</p>' if x["quote"] else "") + "</div></div>" for x in d["stories"])
    works = "".join(f'<li><span class="name">{e(x["title"])}</span> <span class="badge">{e(x["kind"])}</span>'
                    f'<div class="sub">{e(tidy(x["description"]))}</div></li>' for x in d["works"])
    people = "".join(f'<li><span class="name">{e(x["name"])}</span> <span class="badge {e(x["sentiment"])}">'
                     f'{e(x["sentiment"])}</span><div class="sub">{e(tidy(x["description"]))}</div></li>' for x in d["people"])
    themes = "".join(f"<li>{e(t)}</li>" for t in d["themes"])
    moves = "".join(f'<div class="row">{ts_link(x["t"])}<div><p class="pull">“{e(x["statement"])}”</p>'
                    f'<p>{e(tidy(x["explanation"]))}</p></div></div>' for x in d["moves"])
    claims = "".join(f'<li><span class="verb {e(x["verb"])}">{e(x["verb"])}</span> {e(tidy(x["claim"]))}'
                     f' <span class="sub">— {e(x["topic"])}</span></li>' for x in d["claims"])
    quotes = "".join(f'<blockquote>“{e(x["text"])}”<footer>{ts_link(x["t"])} · {e(speaker)} · {e(x["topic"])}</footer>'
                     f"</blockquote>" for x in d["quotes"])
    related = "".join(f'<li><span class="sim">{r["similarity"]}%</span> <a href="{e(r["href"])}">{e(r["title"])}</a>'
                      f' <span class="sub">shares {e(", ".join(r["shared"]))}</span></li>' for r in find_related(d, folder))
    transcript = "".join(f'<div class="turn" id="p{i}"><div class="who">{e(speaker)} '
                         f'<a class="ts" href="{yt}&t={t}s" target="_blank" rel="noopener">{fmt(t)}</a></div>'
                         f"<p>{e(text)}</p></div>" for i, (t, text) in enumerate(paras))

    nav_items = [("ideas", d["ideas"]), ("stories", d["stories"]), ("works", d["works"]), ("people", d["people"]),
                 ("arguments", d["themes"]), ("moves", d["moves"]), ("claims", d["claims"]),
                 ("quotes", d["quotes"]), ("related", related), ("transcript", paras)]
    nav = " ".join(f'<a href="#{k}">{k}</a>' for k, v in nav_items if v)
    words = sum(len(t.split()) for _, t in d["transcript_lines"])
    meta = " · ".join(e(x) for x in [d["format"], d.get("author") or "YouTube", d.get("published_at") or "",
                                       fmt(d["duration_seconds"]), f"{words:,} words"] if x)
    located = len(dots)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(d["title"])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;1,6..72,400&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{--bg:#f6f3ec;--fg:#1d1b17;--muted:#6f6a5f;--rule:#d9d3c5;--accent:#9b2c1f;--chip:#ebe5d8;
--idea:#9b2c1f;--story:#2f5d8a;--move:#7a4f9a;--quote:#3c7a4a;--pos:#3c7a4a;--neg:#9b2c1f}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#161512;--fg:#e9e4d8;--muted:#9a9384;--rule:#34312a;
--accent:#e0705f;--chip:#24221d;--idea:#e0705f;--story:#79a7d6;--move:#b892d6;--quote:#7fbf8c;--pos:#7fbf8c;--neg:#e0705f}}}}
:root[data-theme="dark"]{{--bg:#161512;--fg:#e9e4d8;--muted:#9a9384;--rule:#34312a;--accent:#e0705f;--chip:#24221d;
--idea:#e0705f;--story:#79a7d6;--move:#b892d6;--quote:#7fbf8c;--pos:#7fbf8c;--neg:#e0705f}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:17px/1.6 Newsreader,Georgia,serif}}
.wrap{{max-width:860px;margin:0 auto;padding:0 16px 80px}}
a{{color:inherit}}
.top{{display:flex;justify-content:space-between;align-items:center;padding:14px 0;border-bottom:1px solid var(--rule);
font:13px "IBM Plex Mono",monospace}}
.top b{{letter-spacing:.02em}} .top button{{background:none;border:1px solid var(--rule);color:var(--fg);border-radius:4px;cursor:pointer;font:inherit;padding:2px 8px}}
.crumb{{font:12px "IBM Plex Mono",monospace;color:var(--muted);margin:22px 0 6px}}
h1{{font-size:clamp(30px,6vw,46px);line-height:1.1;font-weight:600;margin:0 0 12px;text-decoration:underline;
text-decoration-color:var(--accent);text-decoration-thickness:2px;text-underline-offset:7px}}
.meta{{font:13px "IBM Plex Mono",monospace;color:var(--muted)}}
.tags{{margin:12px 0 18px;display:flex;flex-wrap:wrap;gap:6px}}
.tags span{{font:12px "IBM Plex Mono",monospace;background:var(--chip);padding:2px 8px;border-radius:3px}}
.summary{{font-size:19px}}
.timeline{{margin:26px 0 8px}} .timeline .hint{{font:12px "IBM Plex Mono",monospace;color:var(--muted);margin-bottom:8px}}
.bar{{position:relative;height:30px;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}}
.dot{{position:absolute;top:7px;width:4px;height:16px;margin-left:-2px;border-radius:2px;opacity:.85}}
.dot:hover{{opacity:1;transform:scaleY(1.3)}}
.dot.idea{{background:var(--idea)}} .dot.story{{background:var(--story)}} .dot.move{{background:var(--move)}} .dot.quote{{background:var(--quote)}}
.ticks{{position:relative;height:18px;font:11px "IBM Plex Mono",monospace;color:var(--muted)}}
.tick{{position:absolute;transform:translateX(-2px)}}
.legend{{font:11px "IBM Plex Mono",monospace;color:var(--muted);display:flex;gap:14px;flex-wrap:wrap;margin-top:4px}}
.legend i{{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px}}
.embed{{margin:22px 0}} .embed .src{{font:12px "IBM Plex Mono",monospace;color:var(--muted);margin-bottom:6px}}
.poster{{position:relative;display:block;border-radius:4px;overflow:hidden;background:#000}}
.poster img{{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;opacity:.85}} .poster:hover img{{opacity:1}}
.play{{position:absolute;inset:0;margin:auto;width:64px;height:64px;border-radius:50%;background:rgba(0,0,0,.65);
color:#fff;display:flex;align-items:center;justify-content:center;font-size:26px;padding-left:4px}}
nav.jump{{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--rule);padding:10px 0;z-index:2;
font:13px "IBM Plex Mono",monospace;display:flex;gap:14px;overflow-x:auto;white-space:nowrap}}
nav.jump a{{text-decoration:none;color:var(--muted)}} nav.jump a:hover{{color:var(--accent)}}
section{{margin-top:40px}} h2{{font-size:24px;font-weight:600;margin:0 0 14px;border-bottom:1px solid var(--rule);padding-bottom:6px}}
.count{{font:14px "IBM Plex Mono",monospace;color:var(--muted);font-weight:400}}
.row{{display:grid;grid-template-columns:84px 1fr;gap:14px;align-items:baseline;padding:12px 0;border-bottom:1px dashed var(--rule)}}
.row>div>:first-child{{margin-top:0}} .row p{{margin:4px 0}}
.when{{white-space:nowrap;line-height:1.6}}
.ts{{font:13px "IBM Plex Mono",monospace;color:var(--accent);text-decoration:none}} .ts.muted{{color:var(--muted)}}
.yt{{font-size:12px;margin-left:4px;text-decoration:none;color:var(--muted)}}
.name{{font-weight:600}} .alias{{font-style:italic;font-weight:400;color:var(--muted)}}
.sub{{font-size:15px;color:var(--muted)}}
.pull{{font-style:italic}}
ul.plain{{list-style:none;padding:0;margin:0}} ul.plain li{{padding:8px 0;border-bottom:1px dashed var(--rule)}}
.badge{{font:11px "IBM Plex Mono",monospace;background:var(--chip);padding:1px 6px;border-radius:3px;margin-left:4px}}
.badge.positive{{color:var(--pos)}} .badge.negative{{color:var(--neg)}}
ul.themes{{padding-left:20px}}
.verb{{font:12px "IBM Plex Mono",monospace;color:var(--accent);display:inline-block;min-width:88px}}
blockquote{{margin:0 0 18px;padding:2px 0 2px 16px;border-left:3px solid var(--accent);font-size:19px}}
blockquote footer{{font:12px "IBM Plex Mono",monospace;color:var(--muted);margin-top:4px}}
.sim{{font:13px "IBM Plex Mono",monospace;color:var(--accent)}}
.turn{{padding:10px 0;border-bottom:1px dashed var(--rule);scroll-margin-top:60px}} .turn p{{margin:4px 0}}
.who{{font:12px "IBM Plex Mono",monospace;color:var(--muted)}}
.turn.flash{{background:var(--chip);transition:background .3s}}
.foot{{margin-top:50px;font:12px "IBM Plex Mono",monospace;color:var(--muted)}}
@media (max-width:560px){{.row{{grid-template-columns:1fr;gap:2px}}}}
blockquote footer .when{{display:inline}}
</style></head><body><div class="wrap">
<div class="top"><b>video archive</b><span>ideas · stories · works · people · quotes <button id="theme" title="toggle theme">◐</button></span></div>
<div class="crumb">archive / videos / {e(d["title"])}</div>
<h1>{e(d["title"])}</h1>
<div class="meta">{meta}</div>
<div class="tags">{"".join(f"<span>{e(t)}</span>" for t in d["tags"])}</div>
<p class="summary">{e(tidy(d["summary"]))}</p>
<div class="timeline"><div class="hint">every located moment in this recording ({located}) — click one to read it</div>
<div class="bar">{"".join(dots)}</div><div class="ticks">{ticks}</div>
<div class="legend"><span><i style="background:var(--idea)"></i>idea</span><span><i style="background:var(--story)"></i>story</span>
<span><i style="background:var(--move)"></i>move</span><span><i style="background:var(--quote)"></i>quote</span></div></div>
<div class="embed"><div class="src">video · YouTube <a href="{yt}" target="_blank" rel="noopener">watch page ↗</a></div>
<a class="poster" href="{yt}" target="_blank" rel="noopener" title="watch on YouTube">
<img src="https://i.ytimg.com/vi/{e(vid)}/hqdefault.jpg" alt="{e(d["title"])}" loading="lazy"><span class="play">▶</span></a></div>
<nav class="jump">{nav}</nav>
{section("ideas", "The ideas in it", len(d["ideas"]), ideas)}
{section("stories", "Stories it reaches for", len(d["stories"]), stories)}
{section("works", "Works invoked", len(d["works"]), f'<ul class="plain">{works}</ul>' if works else "")}
{section("people", "People invoked", len(d["people"]), f'<ul class="plain">{people}</ul>' if people else "")}
{section("arguments", "What it argues", None, f'<ul class="themes">{themes}</ul>' if themes else "")}
{section("moves", "Moves", len(d["moves"]), moves)}
{section("claims", "Claims", len(d["claims"]), f'<ul class="plain">{claims}</ul>' if claims else "")}
{section("quotes", "Quotes", len(d["quotes"]), quotes)}
{section("related", "Videos thinking the same things", None, f'<ul class="plain">{related}</ul>' if related else "")}
{section("transcript", "Transcript", len(paras), transcript)}
<div class="foot">extracted locally with {e(d["model"])} · {e(d["generated_at"][:10])} · timestamps shown only when their cited transcript line checks out</div>
</div>
<script>
(function(){{
  var root=document.documentElement;
  try{{var s=localStorage.getItem("dossier-theme");if(s)root.dataset.theme=s;}}catch(_){{}}
  document.getElementById("theme").onclick=function(){{
    var dark=root.dataset.theme?root.dataset.theme==="dark":matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme=dark?"light":"dark";try{{localStorage.setItem("dossier-theme",root.dataset.theme);}}catch(_){{}}
  }};
  document.addEventListener("click",function(ev){{
    var a=ev.target.closest("[data-p]");if(!a)return;
    var el=document.getElementById("p"+a.dataset.p);if(!el)return;
    el.classList.add("flash");setTimeout(function(){{el.classList.remove("flash")}},1600);
  }});
}})();
</script></body></html>"""


# --------------------------------------------------------------------------- main


def write_outputs(d: dict, json_path: str) -> str:
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    html_path = json_path[:-5] + ".html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(d, os.path.dirname(os.path.abspath(json_path))))
    return html_path


def main() -> None:
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--render":
        d = json.load(open(args[1], encoding="utf-8"))
        print(f"Rendered {write_outputs(d, args[1])}")
        return
    if len(args) not in (1, 2):
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    if len(args) == 2:
        base.OLLAMA_MODEL = args[1]

    video_id = extract_video_id(args[0])
    print(f"Fetching transcript for {video_id}... (model: {base.OLLAMA_MODEL})")
    metadata, lines, transcript_body = fetch(video_id)
    print(f"Title: {metadata.title} ({len(transcript_body)} chars)")

    model_slug = base.OLLAMA_MODEL.replace(":", "-").replace("/", "-")
    checkpoint = f"dossier_{video_id}_{model_slug}.calls.json"
    extracted = extract(transcript_body, checkpoint)
    d = {
        "video_id": video_id,
        "title": metadata.title,
        "author": metadata.author,
        "duration_seconds": int(lines[-1][0]) if lines else 0,
        **extracted,
        "transcript_lines": [[float(s), t] for s, t in lines],
        "model": base.OLLAMA_MODEL,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    html_path = write_outputs(d, f"dossier_{video_id}_{model_slug}.json")
    print(f"\n{len(d['ideas'])} ideas · {len(d['stories'])} stories · {len(d['works'])} works · "
          f"{len(d['people'])} people · {len(d['moves'])} moves · {len(d['claims'])} claims · {len(d['quotes'])} quotes")
    print(f"Saved dossier_{video_id}_{model_slug}.json and {html_path}")


if __name__ == "__main__":
    main()
