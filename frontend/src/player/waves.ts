// Waveform peaks (one JSON per media file), fetched once and shared by the timeline and the player.
export interface Wave { peaksPerSecond: number; peaks: number[] }

const waveCache = new Map<string, Promise<Wave>>();
export function loadWave(url: string): Promise<Wave> {
  if (!waveCache.has(url)) {
    waveCache.set(
      url,
      fetch(url).then((r) => {
        if (!r.ok) throw new Error(`waveform ${r.status}`);
        return r.json();
      }),
    );
  }
  return waveCache.get(url)!;
}
