// The glTF loader (canvas-v2-phase3-4.md 3.7, D10): the parser loads textures with TextureLoader
// (an <img> from blob:, allowed by the CSP), never ImageBitmapLoader (fetch(blob:) is refused); a
// GLB asset parses into a model with its triangle count; loads are cached by asset name and only
// asset names are fetched.
import { afterEach, describe, expect, test, vi } from "vitest";
import { ImageBitmapLoader, TextureLoader } from "three";
import { _clearCache, loadGLB, makeLoader, parseGLB, textureLoaderPlugin } from "./gltf.js";

/** A GLB of one indexed triangle mesh: `tris` triangles over a unit box's corners. */
export function makeGLB(tris = 12) {
  const positions = new Float32Array([0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1]);
  const indices = new Uint16Array(tris * 3).map((_, i) => [0, 1, 2, 0, 2, 3, 4, 5, 6, 4, 6, 7][i % 12]);
  const bin = new Uint8Array(positions.byteLength + indices.byteLength + ((4 - (indices.byteLength % 4)) % 4));
  bin.set(new Uint8Array(positions.buffer), 0);
  bin.set(new Uint8Array(indices.buffer), positions.byteLength);
  const json = {
    asset: { version: "2.0" },
    scene: 0,
    scenes: [{ nodes: [0] }],
    nodes: [{ mesh: 0, name: "body" }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0 }, indices: 1 }] }],
    buffers: [{ byteLength: bin.byteLength }],
    bufferViews: [
      { buffer: 0, byteOffset: 0, byteLength: positions.byteLength },
      { buffer: 0, byteOffset: positions.byteLength, byteLength: indices.byteLength },
    ],
    accessors: [
      { bufferView: 0, componentType: 5126, count: 8, type: "VEC3", min: [0, 0, 0], max: [1, 1, 1] },
      { bufferView: 1, componentType: 5123, count: indices.length, type: "SCALAR" },
    ],
  };
  let text = new TextEncoder().encode(JSON.stringify(json));
  const pad = (4 - (text.length % 4)) % 4;
  text = new Uint8Array([...text, ...new Array(pad).fill(0x20)]);
  const total = 12 + 8 + text.length + 8 + bin.length;
  const out = new DataView(new ArrayBuffer(total));
  out.setUint32(0, 0x46546c67, true);
  out.setUint32(4, 2, true);
  out.setUint32(8, total, true);
  out.setUint32(12, text.length, true);
  out.setUint32(16, 0x4e4f534a, true);
  new Uint8Array(out.buffer, 20, text.length).set(text);
  out.setUint32(20 + text.length, bin.length, true);
  out.setUint32(24 + text.length, 0x004e4942, true);
  new Uint8Array(out.buffer, 28 + text.length, bin.length).set(bin);
  return out.buffer;
}

afterEach(() => _clearCache());

describe("the glTF loader", () => {
  test("the plugin makes the parser load textures with TextureLoader, not ImageBitmapLoader", async () => {
    const loader = makeLoader();
    let seen = null;
    loader.register((parser) => {
      seen = parser.textureLoader;
      return { name: "probe" };
    });
    await parseGLB(makeGLB(), loader);
    expect(seen).toBeInstanceOf(TextureLoader);
    expect(seen).not.toBeInstanceOf(ImageBitmapLoader);
    const parser = { options: { manager: undefined }, textureLoader: new ImageBitmapLoader() };
    expect(textureLoaderPlugin(parser).name).toBe("synapse_texture_loader");
    expect(parser.textureLoader).toBeInstanceOf(TextureLoader);
  });

  test("a GLB parses into a model with its triangle count", async () => {
    const model = await parseGLB(makeGLB(12));
    expect(model.userData.tris).toBe(12);
    expect(model.getObjectByName("body")).toBeTruthy();
  });

  test("loadGLB fetches assets/<name> once per name and refuses other names", async () => {
    const name = `${"a".repeat(32)}.glb`;
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, arrayBuffer: async () => makeGLB(4) }));
    const url = (n) => `/api/teams/t/assets/${n}`;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const [a, b] = await Promise.all([loadGLB(name, url, { fetchImpl, expectTris: 4 }), loadGLB(name, url, { fetchImpl, expectTris: 5 })]);
    expect(a).toBe(b);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl.mock.calls[0][0]).toBe(`/api/teams/t/assets/${name}`);
    expect(warn).toHaveBeenCalledTimes(1); // the second caller expected 5 triangles
    await expect(loadGLB("models/arm.glb", url, { fetchImpl })).rejects.toThrow(/not a GLB asset name/);
    await expect(loadGLB("../x.glb", url, { fetchImpl })).rejects.toThrow();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  test("a failed load is not cached", async () => {
    const name = `${"b".repeat(32)}.glb`;
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 404 }));
    await expect(loadGLB(name, (n) => n, { fetchImpl })).rejects.toThrow(/404/);
    await expect(loadGLB(name, (n) => n, { fetchImpl })).rejects.toThrow(/404/);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });
});
