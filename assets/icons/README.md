# Canvas icons

The canvas draws icons from [Lucide](https://lucide.dev), turned into display-list
paths by `herdr_team/canvas_icons.py` (canvas v2 phase 2, 4.2). The page has no
icon code: every icon arrives as `path` primitives, and the agent's picture draws
the same paths.

| Field | Value |
|---|---|
| Source | `npm pack @iconify-json/lucide@1.2.137` (Lucide in the Iconify JSON format) |
| Licence | ISC (Lucide Contributors); the icons derived from Feather are MIT (Cole Bemis). See `lucide/LICENSE`. |
| Vendored on | 2026-09-28 |
| Icons | 1928 icons, 219 Lucide aliases, a 24x24 box, stroke 2 |

`lucide/icons.json` and `lucide/info.json` are the package's own files, unmodified.
The package ships no licence file, so `lucide/LICENSE` is Lucide's own licence, unmodified,
from `npm pack lucide-static@1.48.0`.

`aliases.json` is ours: words agents use (`db`, `k8s`, `person`) mapped to Lucide names.
`python3 -m herdr_team.canvas_icons --check` fails when a vendored file differs from the
checksum below or an alias names no icon.

| File | sha256 |
|---|---|
| `lucide/icons.json` | `d7f318d772fabb60549c70939f9b05661afb34ca5aef1b2fa0bc35751219714c` |
| `lucide/info.json` | `ee66aa19c0ec61afb1d37ad58715beaeff941b9e804e534f14724b908f46ff35` |
| `lucide/LICENSE` | `b495047bd93a9b06913511076f504daba17d5bbeb3e0650f3bb53a4220329c57` |

## Refreshing

1. `npm pack @iconify-json/lucide@<version>` and unpack it; copy `icons.json` and
   `info.json` into `lucide/` unchanged, and `LICENSE` from the matching `lucide-static`.
2. Update the version, date, counts and checksums above.
3. Run `python3 -m herdr_team.canvas_icons --check`, fix any alias that no longer resolves,
   and run `tests/test_canvas_icons.py`.
