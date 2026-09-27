// A tiny event bus: the one stream fans out to the tabs that care (views, activity, exports).
export function createBus() {
  const handlers = new Map();
  return {
    subscribe(name, fn) {
      if (!handlers.has(name)) handlers.set(name, new Set());
      handlers.get(name).add(fn);
      return () => handlers.get(name).delete(fn);
    },
    emit(name, data) {
      for (const fn of handlers.get(name) || []) fn(data);
    },
  };
}
