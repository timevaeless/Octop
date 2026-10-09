import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  loadLayoutMode,
  saveLayoutMode,
  type LayoutMode,
  type MinimalNavPane,
} from "../layouts/layoutModeStorage";

interface LayoutModeContextValue {
  layoutMode: LayoutMode;
  setLayoutMode: (mode: LayoutMode) => void;
  /** Lower pane in minimal layout (settings vs records). Ignored in classic. */
  minimalPane: MinimalNavPane;
  setMinimalPane: (pane: MinimalNavPane) => void;
}

const LayoutModeContext = createContext<LayoutModeContextValue>({
  layoutMode: "minimal",
  setLayoutMode: () => {},
  minimalPane: "records",
  setMinimalPane: () => {},
});

export function LayoutModeProvider({ children }: { children: ReactNode }) {
  const [layoutMode, setLayoutModeState] = useState<LayoutMode>(() =>
    loadLayoutMode(),
  );
  // Session-only: the pane always starts at records (tasks) on a fresh load,
  // so switching to settings never survives a refresh.
  const [minimalPane, setMinimalPaneState] =
    useState<MinimalNavPane>("records");

  const setLayoutMode = useCallback((mode: LayoutMode) => {
    setLayoutModeState(mode);
    saveLayoutMode(mode);
  }, []);

  const setMinimalPane = useCallback((pane: MinimalNavPane) => {
    setMinimalPaneState(pane);
  }, []);

  const value = useMemo(
    () => ({ layoutMode, setLayoutMode, minimalPane, setMinimalPane }),
    [layoutMode, setLayoutMode, minimalPane, setMinimalPane],
  );

  return (
    <LayoutModeContext.Provider value={value}>
      {children}
    </LayoutModeContext.Provider>
  );
}

export function useLayoutMode(): LayoutModeContextValue {
  return useContext(LayoutModeContext);
}
