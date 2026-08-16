"""Reusable presentation primitives for the AIOS Streamlit dashboard.

The components in this module deliberately contain no investment or governance
logic.  They turn already-reviewed view-model values into a consistent visual
system while native Streamlit widgets retain their normal accessibility,
keyboard, and rerun behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any, Protocol

import streamlit as st


class StatusLike(Protocol):
    """Small structural contract shared by home and paper status models."""

    label: str
    value: str
    detail: str
    tone: str


_TONE_WORDS = {
    "success": "Good",
    "warning": "Needs attention",
    "danger": "Critical",
    "info": "Informational",
}
_STEP_STATE_WORDS = {
    "complete": "Complete",
    "warning": "Needs attention",
    "danger": "Critical",
    "info": "In progress",
}


def _sr_state(word_map: dict[str, str], key: str) -> str:
    """Visually-hidden state word so tone is never color-only for assistive tech."""
    word = word_map.get(key)
    return f'<span class="sr-only"> — {escape(word)}</span>' if word else ""


def apply_design_system() -> None:
    """Load the single dashboard stylesheet.

    Keeping CSS in a dedicated asset makes the token and component contract
    reviewable and prevents individual pages from inventing visual variants.
    """

    css_path = Path(__file__).with_name("dashboard.css")
    css = css_path.read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def page_header(
    title: str,
    purpose: str,
    *,
    metadata: Sequence[tuple[str, str]] = (),
) -> None:
    """Render the shared page header and visible operating context."""

    metadata_html = "".join(
        '<span class="aios-header-meta">'
        f'<span class="aios-status-dot {escape(tone)}" aria-hidden="true"></span>'
        f"{escape(label)}"
        "</span>"
        for label, tone in metadata
    )
    st.markdown(
        f"""
        <header class="aios-page-header" id="aios-main">
          <div class="aios-page-heading">
            <h1>{escape(title)}</h1>
            <p>{escape(purpose)}</p>
          </div>
          <div class="aios-header-metadata">{metadata_html}</div>
        </header>
        """,
        unsafe_allow_html=True,
    )


def section_header(
    title: str,
    description: str | None = None,
) -> None:
    """Render a consistent section heading without creating another card."""

    description_html = f"<p>{escape(description)}</p>" if description else ""
    st.markdown(
        '<div class="aios-section-header">'
        f"<h2>{escape(title)}</h2>{description_html}"
        "</div>",
        unsafe_allow_html=True,
    )


def render_metric_strip(
    metrics: Sequence[tuple[str, str, str, str]],
) -> None:
    """Render two to four comparable headline facts as one visual unit."""

    items = []
    for label, value, detail, tone in metrics:
        safe_tone = tone if tone in {"success", "warning", "danger", "info"} else ""
        items.append(
            '<div class="aios-metric-item">'
            f'<div class="aios-metric-label">{escape(label)}</div>'
            f'<div class="aios-metric-value {safe_tone}">{escape(value)}'
            f"{_sr_state(_TONE_WORDS, safe_tone)}</div>"
            f'<div class="aios-metric-detail">{escape(detail)}</div>'
            "</div>"
        )
    st.markdown(
        '<section class="aios-metric-strip" aria-label="Key dashboard measures">'
        + "".join(items)
        + "</section>",
        unsafe_allow_html=True,
    )


def render_status_rail(statuses: Iterable[StatusLike]) -> None:
    """Render scoped statuses compactly without turning each into a large card."""

    items = []
    for status in statuses:
        safe_tone = status.tone if status.tone in {"success", "warning", "danger", "info"} else ""
        items.append(
            f'<div class="aios-status-item {safe_tone}">'
            '<div class="aios-status-heading">'
            f'<span class="aios-status-dot {safe_tone}" aria-hidden="true"></span>'
            f"<span>{escape(status.label)}</span>"
            "</div>"
            f"<strong>{escape(status.value)}{_sr_state(_TONE_WORDS, safe_tone)}</strong>"
            f"<p>{escape(status.detail)}</p>"
            "</div>"
        )
    st.markdown(
        '<section class="aios-status-rail" aria-label="Scoped system status">'
        + "".join(items)
        + "</section>",
        unsafe_allow_html=True,
    )


def render_control_list(
    rows: Sequence[tuple[str, str, str, str]],
) -> None:
    """Render operational safeguards as dense, readable status rows."""

    items = []
    for label, value, detail, tone in rows:
        safe_tone = tone if tone in {"success", "warning", "danger", "info"} else ""
        items.append(
            f'<div class="aios-control-row {safe_tone}">'
            '<div class="aios-control-copy">'
            '<div class="aios-control-label">'
            f'<span class="aios-status-dot {safe_tone}" aria-hidden="true"></span>'
            f"<span>{escape(label)}</span>"
            "</div>"
            f"<p>{escape(detail)}</p>"
            "</div>"
            f"<strong>{escape(value)}{_sr_state(_TONE_WORDS, safe_tone)}</strong>"
            "</div>"
        )
    st.markdown(
        '<div class="aios-control-list">' + "".join(items) + "</div>",
        unsafe_allow_html=True,
    )


def render_pipeline_stepper(stages: Iterable[StatusLike]) -> None:
    """Render the fixed governed sequence with explicit text for every stage."""

    items = []
    for index, stage in enumerate(stages, start=1):
        safe_tone = stage.tone if stage.tone in {"success", "warning", "danger", "info"} else ""
        state_class = "complete" if safe_tone == "success" else safe_tone
        items.append(
            f'<div class="aios-step {state_class}" role="listitem">'
            '<div class="aios-step-marker" aria-hidden="true">'
            f"{index}"
            "</div>"
            '<div class="aios-step-copy">'
            f"<span>{escape(stage.label.split('·', 1)[-1].strip())}</span>"
            f"<strong>{escape(stage.value)}{_sr_state(_STEP_STATE_WORDS, state_class)}</strong>"
            f"<p>{escape(stage.detail)}</p>"
            "</div>"
            "</div>"
        )
    st.markdown(
        '<section class="aios-stepper" aria-label="Paper trial governance stages" role="list">'
        + "".join(items)
        + "</section>",
        unsafe_allow_html=True,
    )


def render_action_notice(
    *,
    label: str,
    title: str,
    detail: str,
    cta_label: str | None = None,
    on_click: Callable[..., Any] | None = None,
    on_click_args: tuple[Any, ...] = (),
    tone: str = "info",
    technical_command: str | None = None,
    key: str = "primary_action",
) -> None:
    """Render the page's single primary action, or a plain state notice with no CTA.

    Omit `cta_label`/`on_click` for a page whose most material condition has no
    single destination to send the reader to (e.g. it names a condition on the
    current page itself) — the card still gets the same visual weight as a page
    with an action, so "nothing to click" is never confused with "nothing to see".
    """

    safe_tone = tone if tone in {"success", "warning", "danger", "info"} else "info"
    has_cta = cta_label is not None and on_click is not None
    with st.container(key=key):
        st.markdown(
            f'<span class="aios-action-tone {safe_tone}" aria-hidden="true"></span>',
            unsafe_allow_html=True,
        )
        copy_col, cta_col = (
            st.columns([5, 1.35], vertical_alignment="center")
            if has_cta
            else (st.container(), None)
        )
        with copy_col:
            st.markdown(
                '<div class="aios-action-copy">'
                f'<div class="aios-action-label">{escape(label)}</div>'
                f'<div class="aios-action-title">{escape(title)}</div>'
                f'<div class="aios-action-detail">{escape(detail)}</div>'
                "</div>",
                unsafe_allow_html=True,
            )
            if technical_command:
                with st.popover(
                    "Inspection command",
                    type="tertiary",
                    icon=":material/terminal:",
                    key=f"{key}_technical_detail",
                ):
                    st.code(technical_command, language="bash")
        if has_cta:
            with cta_col:
                st.button(
                    cta_label,
                    key=f"{key}_cta",
                    type="primary",
                    width="stretch",
                    on_click=on_click,
                    args=on_click_args,
                )


def key_value_list(rows: Sequence[tuple[str, object]]) -> str:
    """Return safe HTML for a compact, readable evidence list."""

    items = "".join(
        '<div class="aios-key-row">'
        f"<span>{escape(str(label))}</span><strong>{escape(str(value))}</strong>"
        "</div>"
        for label, value in rows
    )
    return f'<div class="aios-key-list">{items}</div>'


def evidence_list(rows: Sequence[tuple[str, object]]) -> str:
    """Return safe HTML for evidence-name/value pairs."""

    items = "".join(
        '<div class="aios-source-item">'
        f"<span>{escape(str(label))}</span><strong>{escape(str(value))}</strong>"
        "</div>"
        for label, value in rows
    )
    return f'<div class="aios-source-list">{items}</div>'


def render_specimen_list(rows: Sequence[Mapping[str, object]]) -> None:
    """Render research targets as determination-stamped specimen rows.

    Each row carries a rank, company name (set like a specimen binomial),
    symbol, sector, target weight, and score — the score renders as an ink
    stamp rather than a plain table cell, matching the rest of the app's
    determination language.
    """

    items = []
    for row in rows:
        rank = escape(str(row["rank"]))
        name = escape(str(row["name"]))
        symbol = escape(str(row["symbol"]))
        sector = escape(str(row["sector"]))
        target = escape(str(row["target"]))
        score = row.get("score")
        score_html = (
            f'<span class="aios-specimen-stamp">Score {escape(str(score))}</span>'
            if score is not None
            else '<span class="aios-specimen-stamp aios-specimen-stamp-pending">Pending</span>'
        )
        items.append(
            '<div class="aios-specimen-row">'
            f'<span class="aios-specimen-rank">No.{rank}</span>'
            '<span class="aios-specimen-name">'
            f"{name} <span class=\"aios-specimen-symbol\">{symbol}</span>"
            "</span>"
            f'<span class="aios-specimen-sector">{sector}</span>'
            f'<span class="aios-specimen-target">{target}</span>'
            f"{score_html}"
            "</div>"
        )
    st.markdown(
        '<div class="aios-specimen-list" role="list">' + "".join(items) + "</div>",
        unsafe_allow_html=True,
    )
