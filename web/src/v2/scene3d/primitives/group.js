// group: a container (layout row, stack, grid, ring or free). It draws nothing itself: its children
// are placed by their own solved positions, and its label (when it has one) sits above them.
import { Group } from "three";

export default {
  name: "group",
  container: true,
  build(obj) {
    const group = new Group();
    group.name = `${obj?.id ?? "group"}:group`;
    return group;
  },
};
