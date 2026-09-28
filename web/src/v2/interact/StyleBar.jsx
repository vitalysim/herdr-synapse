// The style bar for the selection (writable pages): the eight tones as swatches, then variant,
// size, dash and font. Each choice is one restyle op (ops.buildRestyle). With arrows selected it
// offers their route (straight, elbow, curve: ops.buildRestyleRoute), and Pin / Unpin when the
// selection can take them (ops.buildPin, buildUnpin; canvas-v2-phase2.md 6.3 W-b, W-c).
import React from "react";
import { DASHES, FONTS, ROUTES, SIZES, TONES, VARIANTS } from "./ops.js";

// What each choice reads as, per field (the op values stay the server's names).
const LABELS = {
  variant: { soft: "Soft", solid: "Solid", outline: "Outline" },
  size: { s: "S", m: "M", l: "L", xl: "XL" },
  dash: { solid: "Line", dashed: "Dashed", dotted: "Dotted" },
  font: { normal: "Sans", hand: "Hand", code: "Mono" },
  route: { straight: "Straight", orthogonal: "Elbow", curved: "Curve" },
};

function Choice({ field, values, current, onPick }) {
  return (
    <div className="v2-style-group" role="group" aria-label={field}>
      {values.map((value) => (
        <button
          key={value}
          type="button"
          data-style={`${field}:${value}`}
          className={current === value ? "on" : ""}
          title={`${field} ${value}`}
          onClick={() => onPick({ [field]: value })}
        >
          {(LABELS[field] && LABELS[field][value]) || value}
        </button>
      ))}
    </div>
  );
}

// `current`: the first selected element's style ({tone, variant, size, dash, font}), for the
// highlight only. `swatch(tone)`: the tone's colour in the current theme. `route`: the first
// selected arrow's route, or null when no arrow is selected (no route group). `pins`: {pin, unpin},
// which of the two buttons the selection offers.
export default function StyleBar({ count, current = {}, swatch, onPick, route = null, onRoute = null, pins = null, onPin = null, onUnpin = null }) {
  if (!count) return null;
  return (
    <div className="v2-stylebar" role="toolbar" aria-label="Style">
      <div className="v2-style-group" role="group" aria-label="tone">
        {TONES.map((tone) => (
          <button
            key={tone}
            type="button"
            data-style={`tone:${tone}`}
            className={current.tone === tone ? "v2-swatch on" : "v2-swatch"}
            title={tone}
            aria-label={`tone ${tone}`}
            style={{ background: swatch(tone) }}
            onClick={() => onPick({ tone })}
          />
        ))}
      </div>
      <Choice field="variant" values={VARIANTS} current={current.variant} onPick={onPick} />
      <Choice field="size" values={SIZES} current={current.size} onPick={onPick} />
      <Choice field="dash" values={DASHES} current={current.dash} onPick={onPick} />
      <Choice field="font" values={FONTS} current={current.font} onPick={onPick} />
      {route !== null && onRoute ? <Choice field="route" values={ROUTES} current={route} onPick={(patch) => onRoute(patch.route)} /> : null}
      {pins && (pins.pin || pins.unpin) ? (
        <div className="v2-style-group" role="group" aria-label="pin">
          {pins.pin && onPin ? (
            <button type="button" data-style="pin" title="Pin: keep it where it is (no layout moves it)" onClick={onPin}>
              Pin
            </button>
          ) : null}
          {pins.unpin && onUnpin ? (
            <button type="button" data-style="unpin" title="Unpin: let its block lay it out again" onClick={onUnpin}>
              Unpin
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
