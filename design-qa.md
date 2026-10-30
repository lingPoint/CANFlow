> 此文档记录 3.0.1 的历史设计验证。当前多窗口重构参见 [2026-10-30 验证记录](docs/design/2026-10-30/review.md)。

# CANFlow native desktop design QA

final result: passed

## Scope and visual truth

The approved images are `docs/design/2026-10-25/canflow-workbench-prototype.png`
and `docs/design/2026-10-25/canflow-configuration-prototype.png`.
This implementation modifies the existing PySide6 application, rather than building a web prototype.

Both source images are 1568 × 1003 pixels. Native captures are 1600 × 1024 and
1280 × 800 pixels at device pixel ratio 1. For comparison only, the source was
normalized to 1600 × 1024 (near-identical aspect ratio). No source or application
image has been altered for delivery. CSS dimensions do not apply to this native UI.

## Evidence

- Native screenshots: `docs/design/2026-10-25/implemented/waveform-1600.png`,
  `configuration-1600.png`, `waveform-1280.png`, `configuration-1280.png`.
- Full comparisons: `implemented/waveform-comparison.png` and
  `implemented/configuration-comparison.png` under the same design directory.
  Reference on the left; implementation on the right.
- Focused comparisons: `waveform-header-comparison.png`,
  `waveform-detail-comparison.png`, `configuration-header-comparison.png`,
  `configuration-detail-comparison.png`. These cover header spacing, track
  labels/scales, checkbox rows, message grouping, and metadata legibility.
- Screenshots come from real native controls and 301 generated, non-sensitive
  CAN frames, with decoded data and actual sample measurement. Reproduce using
  历史版本中的 `scripts.capture_redesign`（当前脚本已改为生成多窗口截图）.

## Findings and resolution

- Fixed header overlap introduced by an ignored size policy on the project
  selector. The regression test checks the project and save-button geometries.
- Fixed the initial capture's missing font registration. Capture uses the same
  installed Microsoft YaHei font loading as the application entry point.
- Removed full paths from visible DBC rows; full paths remain in tooltips.
- Replaced rotated track labels with horizontal name/channel/unit labels.
- Moved sampling-point thickness into display settings and exposed raw-message
  disclosure directly in the persistent footer.
- Preserved native checkbox input beneath message headings. A regression test
  verifies heading clicks do not select a signal and checkbox clicks do.

## Required fidelity surfaces

- Typography: installed Microsoft YaHei UI, 13px body, 19px section headings,
  24px product name; Chinese and signal names render without missing glyphs.
- Layout: compact shared header, cyan-underlined tabs, approximately 320px
  signal inspector, large waveform workspace, two-column configuration with
  independently grouped file and mapping sections. Both viewports fit without
  forced horizontal expansion or hidden persistent actions.
- Colors: dark navy surfaces, cyan primary action and active state, muted
  metadata, existing deterministic signal colors. Native disabled states remain
  distinct. Gradients from the generated mock are intentionally omitted.
- Assets: existing packaged vector CANFlow logo retained. Native Qt standard
  save icon retained; no decorative/generated image substitutes for live UI.
- Content: actual file dates, frame counts, DBC names, channel/message IDs,
  units, selected counts and measurement values are used. Demo data deliberately
  differs from the mock's illustrative numbers and its three-file/two-DBC setup.

## Native implementation adaptations

Signal rows use two-line technical metadata instead of the mock's wide column
table; this accommodates 1280px windows and long signal names. Message headings
are grouping labels, not new collapsible controls. Signal-group replacement,
append, save, import/export and deletion retain their existing behavior.

The waveform uses the existing precise independent auto-ranges, real sampling
markers, capture-clock wording, shared cursor and full-height A/B markers;
readings remain in the left inspector and detailed A/B values in the measurement
tooltip. These are intentional native-function adaptations rather than pixel
replication of generated data labels. Project menu and native status bar remain.

No actionable P0/P1/P2 issues remain in the inspected states. Minor P3 polish:
the standard Qt checkbox/save icons differ from the concept image, and track
grid/readout styling can be refined further.

## Verification and limits

Full regression: 81 passed, 3 skipped (opt-in large-file performance checks).
After the last footer relocation, all 28 UI tests passed again. Covered behavior
includes replay, raw replay without DBC, search/selection, measurement snapping,
project persistence, missing/changed DBC handling, signal visibility, zoom, and
running/backfill control states. Existing store and performance work was retained.

This is Qt offscreen rendering and automated native interaction evidence.
No browser or web console applies. Manual Windows multi-monitor/high-DPI checks,
screen-reader evaluation and real GiB-scale playback were not performed.

## Implementation checklist

- [x] Apply both approved visual directions to native pages.
- [x] Keep existing decoding, persistence and replay operations functional.
- [x] Compare actual captures with the approved references.
- [x] Fix header/font/input issues and recapture both supported viewports.
- [x] Run regressions and record limitations.
