// The preview's resolved tracking channels (follow.ts resolve()), by clip id. transformAt reads a clip's
// own `autoMotion` when the object carries one (resolve() passes explicit copies), else this registry.
import type { AutoMotion, AutoRegion } from "./types";

let current = new Map<string, { autoMotion?: AutoMotion; autoRegion?: AutoRegion }>();

export function setResolved(m: Map<string, { autoMotion?: AutoMotion; autoRegion?: AutoRegion }>) {
  current = m;
}

export function autoMotionOf(c: { id: string; autoMotion?: unknown }): AutoMotion | null {
  if ("autoMotion" in c) return (c.autoMotion as AutoMotion | null | undefined) ?? null;
  return current.get(c.id)?.autoMotion ?? null;
}

export function autoRegionOf(id: string): AutoRegion | null {
  return current.get(id)?.autoRegion ?? null;
}
