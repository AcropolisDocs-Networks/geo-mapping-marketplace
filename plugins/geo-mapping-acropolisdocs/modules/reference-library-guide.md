---
name: reference-library-guide
description: >
  KNOWLEDGE MODULE - read by geo-find-data and reference-curate. How to read, write and review reference library entries: types and facets,
  use_when and avoid_when, licence classes, review cadence, statuses and the promotion and deprecation workflow, with the review checklist
  and the MAP-LIB lints.
---

# reference-library-guide

Read this before adding, editing, reviewing or retiring a library entry. The library lives in `library/entries/` (data, style, method), with
`types.yaml` (types, required facets, cadence), `tags.yaml` (the facet vocabulary), `index.yaml` and `CHANGELOG.md`. The lint is
`lints/library_lint.py` (MAP-LIB-*); a release refuses an overdue or malformed library (MAP-LIB-013).

## Entry anatomy

Every entry has the core fields `id, type, rev, status, title, summary, tags, use_when, avoid_when, license, maintenance`. Ids are
`ref.<type>.<geography>.<subject>[.<variant>]`, lower case, never reused. Data entries add a `source` (publisher, product, url) and a
`delivery` (bundled, fetch, service, derived, user-supplied); fetch and service entries name their requests, and a source that needs a key
names a `requires_credential` block (host, inject, kind), never a key.

## Facets (tags)

| Facet | Meaning | Write it as |
|---|---|---|
| geography | Where it covers | `us`, `us-az`, `world` |
| level | The unit | `state`, `county`, `tract`, `point`, `line` |
| use_for | The jobs it does | words from `tags.yaml`, e.g. `choropleth-base`, `site-context` |
| scale | Output scales it suits | `1:500k`, `1:24k` (generalization level) |
| audience | Who may see maps made with it | `internal`, `external`, `regulatory` |
| confidentiality | Handling | `public`, `client-confidential` |
| freshness | How current it must be | `static`, `annual`, `live` |
| license_class | The licence family | `public-domain`, `open-attribution`, `share-alike`, `restricted`, `proprietary` |

## use_when and avoid_when

Write them as decisions a mapmaker faces, each a full sentence a person could act on: "A county choropleth of the lower 48 at letter size."
"The map must show areas under one square mile: use the TIGER full-detail file." Name the alternative in `avoid_when`. No marketing words, no
restating the title. Three to six of each.

## Licence classes and what they oblige

| Class | Obliges | Lint |
|---|---|---|
| public-domain | Nothing; credit anyway | |
| open-attribution (CC BY, OGL) | The attribution text on every output | MAP-WEB-021, source note |
| share-alike (ODbL, CC BY-SA) | Attribution, and a data package that includes the data carries the same licence and its text | MAP-LIC-012 (0.15.0) |
| restricted | The terms in `license.note`: often no redistribution, no bulk, no print | `redistribute_ok`, `print_ok`, `bulk_ok` flags |
| proprietary | Internal use under the person's own agreement | audience internal |

## Review cadence and statuses

Cadence by type is in `types.yaml` (public services 3 months, infrastructure 6, boundaries and statistics 12, style 24). `maintenance.verify_by`
is never further than the cadence without a reason in the changelog (MAP-LIB-005). Statuses: `draft` (written, not checked) → `active`
(checked against the source, terms read) → `deprecated` (a better entry exists; names it in `superseded_by`) → `retired` (never selected,
kept so the id is never reused). `blocked` is for a source whose terms forbid use (the reason in `blocked.why`).

## Promotion checklist (draft to active)

1. The source URL opens and the product is the one described; record `url_verified_on`.
2. The licence was read at the source, not copied from another entry; class, flags and `attribution_text` match it.
3. A fetch or service request was run once (`ref_fetch`, `ogc_capabilities`) and answers as the entry says. A keyed source answers its own
   refusal for a made-up key (401 or 403, never 200 with an error body), which shows the address and the key's place are right; the person's
   own key is checked with `/geo-mapping-acropolisdocs:map-credentials check --probe` on first use.
4. Facets use only `tags.yaml` words; use_when and avoid_when are decisions, not descriptions.
5. `rev` raised, a changelog line written, `reviewed_on` today, `verify_by` within the cadence.
6. `library_lint.py` passes; `ref_release.py` bumps LIBRARY_VERSION and the CHANGELOG.

## Deprecation and retirement

Deprecate when a newer vintage or a better source exists: set `superseded_by`, keep it selectable with a warning for one cadence, then retire.
Retire when the source is gone or its terms changed against us. Never delete an entry: maps' provenance records cite entry ids and revisions.
