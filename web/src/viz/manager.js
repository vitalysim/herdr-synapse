// Keeps live visuals cheap: frames off screen are paused, and of the frames on screen only
// one animates (the one the human last pointed at, else the first that became visible).
const frames = new Map();
let focused = null;

function reconcile() {
  const visible = [...frames.entries()].filter(([, frame]) => frame.visible && frame.running());
  let active = visible.find(([id]) => id === focused);
  if (!active) active = visible[0];
  for (const [id, frame] of frames) frame.setPaused(!active || id !== active[0]);
}

export function registerFrame(id, frame) {
  frames.set(id, { visible: false, ...frame });
  reconcile();
  return {
    setVisible(visible) {
      const entry = frames.get(id);
      if (entry && entry.visible !== visible) {
        entry.visible = visible;
        reconcile();
      }
    },
    focus() {
      focused = id;
      reconcile();
    },
    refresh: reconcile,
    unregister() {
      frames.delete(id);
      if (focused === id) focused = null;
      reconcile();
    },
  };
}
