# Bundled canvas fonts

The canvas measures, wraps and draws text with these files, so the Python
model (`herdr_team/canvas_text.py`), the whiteboard page and the agent's PNG
(resvg) agree on every line break. They are committed unmodified, each with
its licence (SIL Open Font License 1.1, which allows redistribution with the
licence and without renaming when the fonts are not modified).

| File | Font | Source | sha256 |
|---|---|---|---|
| `inter/Inter-Regular.ttf` | Inter 4.1, 400 | <https://github.com/rsms/inter/releases/tag/v4.1>, `Inter-4.1.zip` → `extras/ttf/` | `40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82` |
| `inter/Inter-Medium.ttf` | Inter 4.1, 500 | same | `97ad806f526e41546d46365bb3a393145f75b7b1568913db74549ad8b8dba872` |
| `inter/Inter-SemiBold.ttf` | Inter 4.1, 600 | same | `78a843fade9d4612a5567302fb595b56976eb5fcebf4fea5a5912d638bafcde3` |
| `inter/Inter-Bold.ttf` | Inter 4.1, 700 | same | `288316099b1e0a47a4716d159098005eef7c0066921f34e3200393dbdb01947f` |
| `inter/OFL.txt` | Inter's licence | `Inter-4.1.zip` → `LICENSE.txt` | `262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a` |
| `geist-mono/GeistMono-Regular.ttf` | Geist Mono 1.7.2, 400 | <https://github.com/vercel/geist-font/releases/tag/v1.7.2>, `geist-font-v1.7.2.zip` → `geist-font/GeistMono/ttf/` | `42d8ad2e610238e64e8abfcde3037c63f7850a73928742b7ab7229d897bcb155` |
| `geist-mono/OFL.txt` | Geist's licence | `geist-font-v1.7.2.zip` → `geist-font/OFL.txt` | `c683bfbcc7e087f5d37a54ef628f10387c451a83ddc459b151403a164ac46c90` |

Downloaded 2026-09-27. Release archives: `Inter-4.1.zip` sha256
`9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e`,
`geist-font-v1.7.2.zip` sha256
`7fc800d2ac6b92844895196e5041aca55d814c15db70c44f79b3b83ab82b04e2`.

Why these weights: the Phase 0 page draws canvas labels in one weight, Inter
Medium (500; design spec `canvas-v2-design.md` 9.2), and Python measures in
500 too (it is also wider than 400, so a page drawing 400 still fits). 400,
600 and 700 serve resvg, the page chrome and the v2 type roles. Italic
arrives with rich text.

## `font-metrics.json` (generated)

The advance widths of every face above, per codepoint, written by a
standard-library TrueType reader:

```bash
python3 -m herdr_team.canvas_fontgen --write   # after changing any font file
python3 -m herdr_team.canvas_fontgen --check   # tests/test_canvas_fontgen.py runs this
```

Each face records its file's sha256, so a font swapped without regenerating
fails the test suite.

## Calibration (2026-09-27, Chrome 153, `tests/fixtures/canvas-text-corpus.json`)

- Inter through the page's `measureText` is at most 1.021 times the sum of
  its advances (a run of capital W; hinting), so `canvas_text.SAFETY = 1.03`
  keeps every Python width at or above the page's, at 400 and at 500.
- Excalifont (legacy `font: hand`) is 0.78 to 1.131 times Inter Medium's
  advances; `canvas_text.HAND_SCALE = 1.14`.
- Cascadia Code (the Phase 0 page's `font: code`) is 0.586 em per character,
  Geist Mono (resvg) 0.6 em; `canvas_text.MONO_ADVANCE_EM = 0.6`.
- Chinese and Japanese are estimated at 1 em per character and draw at
  0.98; emoji at 1.25 em and draw narrower.
