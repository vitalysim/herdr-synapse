// The style bar for the selection (writable pages): the eight tones as swatches, then variant,
// size, dash and font. Each choice is one restyle op (ops.buildRestyle).
import React from "react";
import { DASHES, FONTS, SIZES, TONES, VARIANTS } from "./ops.js";

// What each choice reads as, per field (the op values stay the server's names).
const LABELS = {
  variant: { soft: "Soft", solid: "Solid", outline: "Outline" },
  size: { s: "S", m: "M", l: "L", xl: "XL" },
  dash: { solid: "Line", dashed: "Dashed", dotted: "Dotted" },
  font: { normal: "Sans", hand: "Hand", code: "Mono" },
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
// highlight only. `swatch(tone)`: the tone's colour in the current theme.
export default function StyleBar({ count, current = {}, swatch, onPick }) {
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
    </div>
  );
}
