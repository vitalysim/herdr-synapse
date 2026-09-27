/*
 * synapse-viz.js: the runtime inside a sealed live-visual frame (herdr-synapse whiteboard).
 *
 * The frame is <iframe sandbox="allow-scripts"> with an opaque origin and a CSP that
 * forbids every network request, so the agent's code can only talk to the page through
 * postMessage. This file gives it:
 *   window.synapse = { data, paused, width, height, onData(fn), onResize(fn) }
 * width and height are the frame's size (the viz element's w and h on the canvas). A lone
 * top-level <canvas> or <svg> is marked data-synapse-fit, which the document's stylesheet
 * scales to the frame with its aspect kept; an svg with a numeric width and height and no
 * viewBox gets one, so its drawing scales rather than its viewport. It also speaks this protocol with the page (the parent):
 *   frame -> page  {type: "synapse:ready"} once, {type: "synapse:beat"} every 500 ms,
 *                  {type: "synapse:still", png: dataURL} after a capture request,
 *                  {type: "synapse:error", message} for uncaught errors
 *   page -> frame  {type: "synapse:data", data}, {type: "synapse:pause"},
 *                  {type: "synapse:resume"}, {type: "synapse:capture"}
 * While paused, requestAnimationFrame callbacks are held, so animations stop costing CPU.
 */
(function () {
  "use strict";
  var parentWindow = window.parent;
  var listeners = [];
  var paused = false;
  var held = [];
  var heldCounter = 0;
  var nativeRequest = window.requestAnimationFrame.bind(window);
  var nativeCancel = window.cancelAnimationFrame.bind(window);

  function post(message) {
    try {
      parentWindow.postMessage(message, "*");
    } catch (err) {
      /* the page went away */
    }
  }

  function report(err) {
    post({ type: "synapse:error", message: String((err && err.message) || err).slice(0, 300) });
  }

  window.requestAnimationFrame = function (callback) {
    if (paused) {
      heldCounter -= 1;
      held.push({ id: heldCounter, callback: callback });
      return heldCounter;
    }
    return nativeRequest(callback);
  };

  window.cancelAnimationFrame = function (id) {
    if (id < 0) {
      held = held.filter(function (item) {
        return item.id !== id;
      });
      return;
    }
    nativeCancel(id);
  };

  var resizeListeners = [];

  var synapse = {
    data: null,
    paused: false,
    get width() {
      return window.innerWidth;
    },
    get height() {
      return window.innerHeight;
    },
    onData: function (fn) {
      if (typeof fn !== "function") return;
      listeners.push(fn);
      if (synapse.data !== null) {
        try {
          fn(synapse.data);
        } catch (err) {
          report(err);
        }
      }
    },
    onResize: function (fn) {
      if (typeof fn === "function") resizeListeners.push(fn);
    },
  };
  Object.defineProperty(window, "synapse", { value: synapse, writable: false, configurable: false });

  window.addEventListener("resize", function () {
    resizeListeners.slice().forEach(function (fn) {
      try {
        fn(window.innerWidth, window.innerHeight);
      } catch (err) {
        report(err);
      }
    });
  });

  // Agents often draw at a fixed size (a 300x150 canvas, <svg width=460 height=320>) in a frame
  // of another size, which left the drawing in a corner. A lone canvas or svg is fitted instead.
  var PLAIN_SIZE = /^\s*\d+(\.\d+)?(px)?\s*$/;
  var IGNORED = { SCRIPT: true, STYLE: true, LINK: true, TEMPLATE: true, NOSCRIPT: true };
  var fitted = null;
  var fittedViewBox = false;
  var sizeWatcher = null;

  function loneDrawing() {
    var found = null;
    for (var el = document.body && document.body.firstElementChild; el; el = el.nextElementSibling) {
      if (IGNORED[el.tagName.toUpperCase()]) continue;
      var tag = el.tagName.toLowerCase();
      if (found || (tag !== "canvas" && tag !== "svg")) return null;
      found = el;
    }
    return found;
  }

  function fitViewBox(svg) {
    var width = svg.getAttribute("width");
    var height = svg.getAttribute("height");
    if (svg.hasAttribute("viewBox") && !fittedViewBox) return;
    if (!PLAIN_SIZE.test(width || "") || !PLAIN_SIZE.test(height || "")) return;
    svg.setAttribute("viewBox", "0 0 " + parseFloat(width) + " " + parseFloat(height));
    fittedViewBox = true;
  }

  function fit() {
    var el = loneDrawing();
    if (el === fitted) return;
    if (fitted) fitted.removeAttribute("data-synapse-fit");
    if (sizeWatcher) sizeWatcher.disconnect();
    fitted = el;
    fittedViewBox = false;
    if (!el) return;
    el.setAttribute("data-synapse-fit", "");
    if (el.tagName.toLowerCase() === "svg") {
      fitViewBox(el);
      // Code that later sets the svg's width and height moves the viewBox it was given with them.
      sizeWatcher = new MutationObserver(function () {
        fitViewBox(el);
      });
      sizeWatcher.observe(el, { attributes: true, attributeFilter: ["width", "height"] });
    }
  }

  function startFitting() {
    fit();
    new MutationObserver(fit).observe(document.body, { childList: true });
  }

  function capture() {
    var canvas = document.querySelector("canvas");
    if (canvas) {
      try {
        post({ type: "synapse:still", png: canvas.toDataURL("image/png") });
        return;
      } catch (err) {
        /* a tainted or lost canvas: try an svg */
      }
    }
    var svg = document.querySelector("svg");
    if (svg) {
      try {
        var clone = svg.cloneNode(true);
        clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
        var box = svg.getBoundingClientRect();
        if (!clone.getAttribute("width")) clone.setAttribute("width", String(Math.round(box.width) || 300));
        if (!clone.getAttribute("height")) clone.setAttribute("height", String(Math.round(box.height) || 150));
        var text = new XMLSerializer().serializeToString(clone);
        post({ type: "synapse:still", png: "data:image/svg+xml;charset=utf-8," + encodeURIComponent(text) });
        return;
      } catch (err) {
        report(err);
      }
    }
    post({ type: "synapse:still", png: null });
  }

  window.addEventListener("message", function (event) {
    if (event.source !== parentWindow) return;
    var message = event.data;
    if (!message || typeof message.type !== "string") return;
    if (message.type === "synapse:data") {
      synapse.data = message.data === undefined ? null : message.data;
      listeners.slice().forEach(function (fn) {
        try {
          fn(synapse.data);
        } catch (err) {
          report(err);
        }
      });
    } else if (message.type === "synapse:pause") {
      paused = true;
      synapse.paused = true;
    } else if (message.type === "synapse:resume") {
      paused = false;
      synapse.paused = false;
      var queued = held;
      held = [];
      queued.forEach(function (item) {
        nativeRequest(item.callback);
      });
    } else if (message.type === "synapse:capture") {
      capture();
    }
  });

  window.addEventListener("error", function (event) {
    report(event.message || "error");
  });

  setInterval(function () {
    post({ type: "synapse:beat" });
  }, 500);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      startFitting();
      post({ type: "synapse:ready" });
    });
  } else {
    startFitting();
    post({ type: "synapse:ready" });
  }
})();
