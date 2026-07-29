"""Reusable presentation primitives for the AIOS Streamlit dashboard.

The components in this module deliberately contain no investment or governance
logic.  They turn already-reviewed view-model values into a consistent visual
system while native Streamlit widgets retain their normal accessibility,
keyboard, and rerun behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
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


def apply_design_system() -> None:
    """Load the single dashboard stylesheet.

    Keeping CSS in a dedicated asset makes the token and component contract
    reviewable and prevents individual pages from inventing visual variants.
    """

    css_path = Path(__file__).with_name("dashboard.css")
    css = css_path.read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def page_header(
    eyebrow: str,
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
            <div class="aios-eyebrow">{escape(eyebrow)}</div>
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
    *,
    eyebrow: str | None = None,
) -> None:
    """Render a consistent section heading without creating another card."""

    eyebrow_html = f'<div class="aios-section-eyebrow">{escape(eyebrow)}</div>' if eyebrow else ""
    description_html = f"<p>{escape(description)}</p>" if description else ""
    st.markdown(
        '<div class="aios-section-header">'
        f"{eyebrow_html}<h2>{escape(title)}</h2>{description_html}"
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
            f'<div class="aios-metric-value {safe_tone}">{escape(value)}</div>'
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
            f"<strong>{escape(status.value)}</strong>"
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
            f"<strong>{escape(value)}</strong>"
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
            f'<div class="aios-step {state_class}">'
            '<div class="aios-step-marker" aria-hidden="true">'
            f"{index}"
            "</div>"
            '<div class="aios-step-copy">'
            f"<span>{escape(stage.label.split('·', 1)[-1].strip())}</span>"
            f"<strong>{escape(stage.value)}</strong>"
            f"<p>{escape(stage.detail)}</p>"
            "</div>"
            "</div>"
        )
    st.markdown(
        '<section class="aios-stepper" aria-label="Paper trial governance stages">'
        + "".join(items)
        + "</section>",
        unsafe_allow_html=True,
    )


def render_action_notice(
    *,
    label: str,
    title: str,
    detail: str,
    cta_label: str,
    on_click: Callable[..., Any],
    on_click_args: tuple[Any, ...] = (),
    tone: str = "info",
    technical_command: str | None = None,
    key: str = "primary_action",
) -> None:
    """Render the page's single primary action with a stable CTA position."""

    safe_tone = tone if tone in {"success", "warning", "danger", "info"} else "info"
    with st.container(key=key):
        st.markdown(
            f'<span class="aios-action-tone {safe_tone}" aria-hidden="true"></span>',
            unsafe_allow_html=True,
        )
        copy_col, cta_col = st.columns([5, 1.35], vertical_alignment="center")
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
