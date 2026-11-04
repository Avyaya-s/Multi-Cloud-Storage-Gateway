import { useEffect, useRef } from "react";

/** Run `fn` now and then every `ms` after the previous run finishes (never overlapping). */
export function usePoll(fn: () => Promise<void>, ms: number): void {
  const latest = useRef(fn);
  latest.current = fn;

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        await latest.current();
      } finally {
        if (!stopped) timer = setTimeout(tick, ms);
      }
    };
    tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [ms]);
}
