# AIOS UI Design System

Status: normative product and implementation guidance
Last reviewed: 2026-07-28

This document defines the shared visual language and interaction contract for
the AI Investment OS dashboard. It applies to every workspace. Page-specific
CSS, arbitrary colors, and one-off component variants are out of scope.

## Product principles

1. **Decisions before diagnostics.** Lead with what changed, what is safe, and
   the next permitted action. Put technical traces after decision evidence.
2. **Evidence at first glance.** Material status, candidate results, blocking
   conditions, and data readiness remain visible without expanding a panel.
3. **One page, one primary outcome.** A page may have at most one emphasized
   action. Secondary controls must not compete with it.
4. **Hierarchy over card volume.** Use scale, position, and whitespace to rank
   information. Do not turn every sentence or value into a card.
5. **Color has one meaning.** Near-black marks the primary action, clay marks
   product identity or selection, and blue marks information. Green, amber,
   and red are reserved for semantic state.
6. **Comfortable by default.** Readability is the default. Compact density may
   be offered globally for data-intensive work, never applied ad hoc.
7. **Progressive disclosure is selective.** Hide expert detail, not information
   needed to make the current decision.
8. **Responsive means prioritized.** On small screens retain the decision,
   status, and action; do not merely squeeze the desktop layout.

These principles are grounded in the official
[Carbon dashboard guidance](https://carbondesignsystem.com/data-visualization/dashboards/),
[Cloudscape static-dashboard pattern](https://cloudscape.design/patterns/general/service-dashboard/static-dashboard/),
[Claude interface guidance](https://claude.com/docs/connectors/building/mcp-apps/design-guidelines),
and [Goldman Sachs Design System engineering principles](https://developer.gs.com/blog/posts/engineering-principles-of-the-gs-design-system).

## Color tokens

The palette is warm-neutral and institutional. Structural colors should be
quiet so evidence and state carry the visual emphasis.

| Token | Value | Use |
|---|---:|---|
| `canvas` | `#FAF9F5` | Application background |
| `surface` | `#FFFFFF` | Primary panels and tables |
| `surface-muted` | `#F5F4ED` | Grouped controls and secondary regions |
| `surface-raised` | `#FBFAF7` | Elevated or focused surface |
| `sidebar` | `#F5F4ED` | Global navigation shell |
| `sidebar-hover` | `#EEECE4` | Navigation hover |
| `sidebar-active` | `#FFFFFF` | Active navigation item |
| `text-primary` | `#141413` | Headings and primary copy |
| `text-secondary` | `#3D3D3A` | Supporting copy |
| `text-tertiary` | `#73726C` | Metadata and captions |
| `text-inverse` | `#FAF9F5` | Text on dark surfaces |
| `border` | `#D9D6CC` | Standard boundaries |
| `border-strong` | `#B9B5AA` | Focused grouping and table header rules |
| `brand` | `#C96442` | Identity and selected state |
| `brand-strong` | `#A84F30` | Accessible link and hover foreground |
| `brand-soft` | `#F4E4DC` | Selected-state background |
| `information` | `#3266AD` | Informational foreground |
| `primary-action` | `#141413` | Primary button background |
| `success` | `#437426` | Success foreground |
| `success-soft` | `#E9F1DC` | Success background |
| `warning` | `#805C1F` | Warning foreground |
| `warning-soft` | `#F6EEDF` | Warning background |
| `danger` | `#A73D39` | Critical foreground |
| `danger-soft` | `#F7ECEC` | Critical background |

Rules:

- Do not use gradients.
- Routine surfaces use borders, not decorative shadows. A focused overlay may
  use one restrained elevation token.
- Do not use semantic colors for factor scores, navigation, or decoration.
- Charts use a stable non-semantic categorical palette. Reserve red and green
  for true loss/gain or failure/success meaning.
- All text and controls must meet WCAG AA contrast.

The semantic tones adapt the official
[Claude style variables](https://claude.com/docs/connectors/building/mcp-apps/design-guidelines#style-variables)
to the AIOS brand.

## Typography

Use one legible sans-serif product family. Prefer `Inter`, `IBM Plex Sans`, or
the platform sans-serif fallback. Financial numbers use tabular figures.

| Role | Size / line height | Weight |
|---|---:|---:|
| Page title | `29px / 35px` | `700` |
| Section title | `20px / 28px` | `650` |
| Component title | `18px / 24px` | `650` |
| Body | `16px / 24px` | `400` |
| Secondary body | `14px / 20px` | `400` |
| Button and control | `15px / 20px` | `600` |
| Metric | `28–30px / 36px` | `650` |
| Table | `14–15px / 20px` | `400` |
| Caption, minimum | `14px / 20px` | `400` |

Rules:

- Normal explanatory text must never be smaller than `14px`.
- Use `13px` only for terse metadata; avoid `12px` except legal or code detail.
- Use sentence case. Reserve uppercase for short eyebrows only.
- Different heading levels must have visibly different sizes.
- Use `font-variant-numeric: tabular-nums` in metrics and numeric columns.
- Keep prose lines near 60–80 characters where reading is the primary task.

See [Atlassian typography guidance](https://atlassian.design/foundations/typography/applying-typography)
and [Carbon table typography](https://carbondesignsystem.com/components/data-table/style/).

## Spacing and layout

Use an 8px base rhythm:

| Token | Value | Typical use |
|---|---:|---|
| `space-1` | `4px` | Optical correction only |
| `space-2` | `8px` | Icon/text gap |
| `space-3` | `12px` | Compact internal gap |
| `space-4` | `16px` | Control and cell padding |
| `space-5` | `24px` | Card padding and grid gap |
| `space-6` | `32px` | Section separation and page gutter |
| `space-8` | `48px` | Major page-region separation |
| `space-10` | `64px` | Rare editorial separation |

Desktop layout:

- Sidebar: `240–256px`; global navigation only.
- Main content: fluid with `32px` gutters and a `1520–1560px` maximum.
- Grid: 12 columns with `24px` gutters.
- Standard panel padding: `24px`.
- Standard radius: `8px`; raised or modal radius: `12px`.
- Standard control height: `44px`; major primary action: `48px`.
- Standard table row: `48px` comfortable, `40px` compact.

Breakpoints:

- `>= 1200px`: 12-column desktop grid.
- `768–1199px`: two-column regions where content permits.
- `< 768px`: single column, `16px` gutters, navigation drawer.
- On mobile, prioritize essential columns and expose secondary row data as
  labeled details rather than horizontal overflow.

Spacing follows the official
[Atlassian 8px system](https://atlassian.design/foundations/spacing/).
Density behavior follows
[Cloudscape content-density guidance](https://cloudscape.design/foundation/visual-foundation/content-density/).

## Reusable component contract

Pages must compose the following primitives. Components consume shared tokens;
they do not accept arbitrary color, radius, or spacing values.

### `PageHeader`

- Eyebrow, title, one-sentence purpose, metadata, optional primary action.
- Metadata is visible and wraps; it is not a row of decorative badges.
- At most one emphasized action, aligned with the title region.

### `ActionNotice`

- Tone: `info`, `warning`, `danger`, or `success`.
- Contains icon, specific heading, concise consequence, next step, and optional
  action.
- Semantic background remains subtle. The page's single primary CTA uses
  near-black with an ivory label.

### `Button`

- Variants: `primary`, `secondary`, `tertiary`, `danger`.
- Sizes: `medium` (`44px`) and `large` (`48px`).
- Labels begin with a specific action verb.
- Buttons trigger actions; links perform navigation.
- All states include hover, active, disabled, loading, and visible focus.

### `StatusBadge`

- Short state only, never a sentence.
- Semantic tone must match actual state.
- A badge supplements text; color is not the sole signal.

### `MetricTile`

- Label, prominent value, optional unit, and one line of context or comparison.
- No more than four headline metrics in one row.
- A narrative such as an economic regime is context, not a metric tile.

### `SectionCard`

- Header with title, optional supporting text, and optional local action.
- One coherent content goal per card.
- Avoid nested cards. Use dividers or grouped rows inside a section.

### `FilterBar`

- Visible, page-level controls for the current workspace.
- Standard order: primary scope, model/view, search, secondary filters, reset.
- Applied state is URL-backed and survives refresh.
- Use visible tabs or segmented controls for 2–4 mutually exclusive options.

### `DataTable`

- Visible title/description, search and filters, sticky header, sortable columns,
  row hover/focus, pagination, loading, empty, and error states.
- Text aligns left; comparable numbers align right with tabular figures.
- A row navigates via a real link. Selection controls appear only when a batch
  action exists.
- Long evidence detail becomes a concise status/count with details in the row or
  destination page.

### `PipelineStepper`

- Shows completed, current, blocked, and future stages without relying on color.
- Current blocker and permitted next action remain visible.

### Supporting primitives

`KeyValueList`, `SectionHeader`, `InlineNotice`, `EmptyState`, `Skeleton`,
`Pagination`, and `DensityControl` use the same tokens and interaction states.

## CTA rules

1. Use no more than one primary CTA per page.
2. Place the primary CTA in the page header or beside the condition it resolves.
3. Use a semantic danger button only for a destructive or irreversible action.
4. Navigating to System Health or Company Detail is a link styled appropriately,
   not a fake form action.
5. Secondary actions use a bordered button; tertiary actions use a text link.
6. Keep action placement consistent and button widths content-led. Use full
   width only on narrow mobile layouts.
7. Minimum target size is `44 × 44px`, with a visible keyboard focus state.
8. Loading actions retain their label context and prevent duplicate submission.

See [Shopify Polaris button guidance](https://polaris-react.shopify.com/components/actions/button)
and [SAP Fiori action placement](https://experience.sap.com/fiori-design-web/action-placement/).

## Disclosure policy

Show by default:

- Material status and blocking conditions.
- The next permitted action and its consequence.
- Top candidates or proposal targets.
- Current paper-trial stage.
- Critical incidents and data-readiness summary.
- Definitions needed to interpret a visible metric.

Collapse or move to a dedicated detail view:

- Raw shell, SQL, and inspection commands.
- Checksums, provider traces, and long provenance records.
- Complete methodology and formula derivations.
- Historical run logs beyond the recent operational summary.

Never use an expander merely to shorten the page. Routine choices should be
visible controls; routine evidence should be visible content. This follows
Claude's guidance to prefer
[visible controls over hidden menus](https://claude.com/docs/connectors/building/mcp-apps/design-guidelines#visible-controls-over-hidden-menus).

## Page blueprints

### Today — Investment Command Center

1. `PageHeader`: reviewed date, certification state, optional single action.
2. `ActionNotice`: most material operating or governance condition.
3. Four-metric strip: research state, score coverage, paper stage, system health.
4. `8/4` grid: top five research candidates and paper-trial stepper.
5. `8/4` grid: data readiness and open incidents.
6. Links to complete Research, Paper Trial, and System Health views.

Do not repeat the same state in status cards, badges, and a second banner.

### Research

1. `PageHeader`: research purpose and concise model/date context.
2. Persistent `FilterBar`: date, model, company search, grade, evidence state.
3. Visible view tabs: Ranked List, Opportunity Map, Data Coverage.
4. Four metrics: universe, scored, coverage percentage, withheld.
5. Economic backdrop as a context strip.
6. `DataTable` as the dominant page surface.

Company is the row link. Do not show checkboxes without batch actions.

### Company Detail

1. Security hero: company, symbol, grade, overall score, reviewed date.
2. Four headline measures: overall, quality, value, and evidence coverage.
3. Factor-profile visualization and concise interpretation.
4. Business and valuation evidence in aligned sections.
5. Risks, missing evidence, market context, and provenance.

Do not render a wall of small metric cards.

### Paper Trial

1. Clear “simulation only” context in the page header.
2. Persistent proposal-to-record `PipelineStepper`.
3. Current blocking condition and one permitted next action.
4. Proposal targets and sizing in a visible table.
5. Timing, checksum, and governance evidence.
6. Historical simulations and technical trace after current-workflow evidence.

### System Health

1. Critical incident summary and direct resolution path.
2. Headline controls: unresolved incidents, failed jobs, freshness, coverage.
3. Open incidents, ordered failure-first.
4. Control matrix and reviewed-data coverage.
5. Recent run history and inspection commands.

Critical incidents must never be hidden by a success-colored aggregate.

### How It Works

1. Reading width of `760–840px`.
2. `18px` explanatory body copy.
3. Architecture flow, evidence lifecycle, and governance timeline.
4. Plain-language definitions followed by technical references.

This page is documentation, not a dense dashboard.

## Definition of done

A UI change is complete only when:

- All pages use the shared tokens and component primitives.
- No normal body text is below `14px`.
- Material evidence is available without an unnecessary disclosure click.
- Each page has at most one primary CTA.
- Keyboard focus, contrast, loading, empty, error, and long-content states work.
- URL state reflects active workspace, filters, tabs, and selected entity.
- Desktop, tablet, and mobile screenshots show no clipped text or horizontal
  page overflow.
- Tables remain usable in comfortable and compact density.
- Visual QA is performed at approximately `1920×1080`, `1280×800`, and
  `390×844`.

Additional interaction checks follow the
[Vercel Web Interface Guidelines](https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md).
