import { useCallback, useState, type PointerEvent as ReactPointerEvent } from "react";
import {
  EXPANDED_WIDTH,
  SIDEBAR_RAIL_ID,
  clampNavWidth,
} from "../sidebarNav";
import { beginPointerDragSession } from "../../hooks/usePointerDragSession";

const SIDEBAR_WIDTH_KEY = "octop:sidebar:width";

function getSavedNavWidth(): number {
  try {
    const saved = localStorage.getItem(SIDEBAR_WIDTH_KEY);
    if (saved !== null) {
      const parsed = Number.parseInt(saved, 10);
      if (Number.isFinite(parsed)) {
        return clampNavWidth(parsed, window.innerWidth);
      }
    }
  } catch {
    // localStorage may be unavailable (e.g. private browsing restrictions)
  }
  return EXPANDED_WIDTH;
}

/**
 * Drag the nav rail's right edge to resize it.
 *
 * Same shape as the chat dock resize: write the width straight to the DOM on
 * every move (rAF-batched by the drag session) so the nav tree is not
 * re-rendered per pointermove, then commit React state + localStorage once on
 * pointerup. ``resizing`` drops the width transition for the duration —
 * otherwise each frame would animate towards the pointer.
 *
 * Only wired up while the rail is expanded: a 56px rail has no width to
 * negotiate, and writing one would fight the collapsed width.
 */
export function useNavResize() {
  const [navWidth, setNavWidth] = useState(() => getSavedNavWidth());
  const [navResizing, setNavResizing] = useState(false);

  const persistNavWidth = useCallback((next: number) => {
    setNavWidth(next);
    try {
      localStorage.setItem(SIDEBAR_WIDTH_KEY, String(next));
    } catch {
      /* ignore */
    }
  }, []);

  const handleNavResizeStart = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();

      const handle = event.currentTarget;
      const rail = document.getElementById(SIDEBAR_RAIL_ID);
      const startX = event.clientX;
      const startWidth = rail?.offsetWidth ?? navWidth;
      let latest = startWidth;
      setNavResizing(true);

      beginPointerDragSession({
        pointerId: event.pointerId,
        target: handle,
        cursor: "col-resize",
        onMove: (clientX) => {
          latest = clampNavWidth(
            startWidth + (clientX - startX),
            window.innerWidth,
          );
          if (!rail) return;
          rail.style.width = `${latest}px`;
          rail.style.minWidth = `${latest}px`;
        },
        onEnd: () => {
          setNavResizing(false);
          persistNavWidth(latest);
        },
      });
    },
    [navWidth, persistNavWidth],
  );

  return { navWidth, navResizing, handleNavResizeStart };
}
