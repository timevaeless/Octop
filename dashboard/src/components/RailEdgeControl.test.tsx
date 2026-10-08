import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import RailEdgeControl from "./RailEdgeControl";

describe("RailEdgeControl", () => {
  it("stays a plain collapse control when no resize handler is given", () => {
    // The chat history rail shares this component and must not grow a drag handle.
    const onToggle = vi.fn();
    render(<RailEdgeControl expanded onToggle={onToggle} />);

    expect(screen.queryByRole("separator")).toBeNull();

    fireEvent.click(screen.getByRole("button"));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("exposes a drag handle when the caller opts in", () => {
    const onResizeStart = vi.fn();
    render(
      <RailEdgeControl
        expanded
        onToggle={() => undefined}
        onResizeStart={onResizeStart}
      />,
    );

    const handle = screen.getByRole("separator");
    expect(handle).toHaveAttribute("aria-orientation", "vertical");

    fireEvent.pointerDown(handle, { button: 0, clientX: 240, pointerId: 1 });
    expect(onResizeStart).toHaveBeenCalledTimes(1);
  });
});
