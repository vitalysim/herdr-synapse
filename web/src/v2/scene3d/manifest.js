// Data only: the scene3d primitives and loaders this page bundles (canvas-v2-phase3-4.md D19).
// scripts/postbuild.mjs reads it into web/dist/charts.json "scene3d"; the Python test holds it equal
// to the canvas_scene3d registry, and a vitest holds it equal to primitives/index.js.
export const PRIMITIVES = ["arrow3d", "box", "cone", "cylinder", "gltf", "group", "plane", "sphere", "text3d"];
export const LOADERS = ["gltf"];
// The echarts-gl and ECharts modules the GL chart chunk (glCharts.js) registers: web/dist/charts.json
// "gl", which the Python test holds against every GL chart type's declared modules.
export const GL_MODULES = ["Bar3DChart", "Scatter3DChart", "SurfaceChart", "Grid3DComponent", "VisualMapComponent", "LegendComponent", "TooltipComponent", "AriaComponent"];
// The relations an object rests on or sits inside by (contact): a label may cover what holds its
// object up (labels.js). The Python test holds it equal to canvas_scene3d.relations' contact ones.
export const CONTACT = ["inside", "on"];
