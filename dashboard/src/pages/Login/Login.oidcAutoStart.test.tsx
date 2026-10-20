import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../context/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

const { startOauth, oauthProviders } = vi.hoisted(() => ({
  startOauth: vi.fn(),
  oauthProviders: { current: [] as Array<Record<string, unknown>> },
}));

vi.mock("../../api/modules/auth", () => ({
  authApi: {
    getAuthStatus: () => Promise.resolve({ setup_required: false }),
    getOauthStatus: () =>
      Promise.resolve({ providers: oauthProviders.current }),
    getLdapStatus: () => Promise.resolve({ enabled: false }),
    getCaptcha: () => Promise.resolve({ provider: "none" }),
    startOauth: (...args: unknown[]) => startOauth(...args),
  },
}));

vi.mock("../../api", () => ({
  clearAuthToken: vi.fn(),
  setAuthToken: vi.fn(),
  setRememberLoginPreference: vi.fn(),
}));

vi.mock("../../utils/locale", () => ({
  applyGuestLocale: () => Promise.resolve(),
  applyUserLocale: () => Promise.resolve(),
}));

vi.mock("./CaptchaField", () => ({
  default: () => null,
}));

import LoginPage from "./index";

const oidcProvider = { kind: "oidc", enabled: true, display_name: "门户 SSO" };

function renderLogin(search = "") {
  return render(
    <MemoryRouter initialEntries={[`/login${search}`]}>
      <LoginPage />
    </MemoryRouter>,
  );
}

describe("Login OIDC auto-start", () => {
  beforeEach(() => {
    // startOauth is a file-level hoisted mock: without clearing, call counts
    // leak across tests and the "not called" assertions see earlier tests.
    startOauth.mockClear();
  });

  it("auto-starts SSO on explicit ?sso=auto when OIDC is the only enabled provider", async () => {
    oauthProviders.current = [oidcProvider];
    startOauth.mockResolvedValue({
      authorization_url: "https://idp.example/authorize?x=1",
    });
    renderLogin("?sso=auto");
    await waitFor(() => expect(startOauth).toHaveBeenCalledTimes(1));
    expect(startOauth).toHaveBeenCalledWith("oidc", "/chat");
  });

  it("does not auto-start on bare /login (local admin form stays)", async () => {
    oauthProviders.current = [oidcProvider];
    renderLogin();
    await waitFor(() =>
      expect(
        screen.getByPlaceholderText(
          /login\.username|用户名或邮箱|username or email/i,
        ),
      ).toBeInTheDocument(),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(startOauth).not.toHaveBeenCalled();
  });

  it("does not auto-start when arriving with oidc_error", async () => {
    oauthProviders.current = [oidcProvider];
    renderLogin("?oidc_error=invalid_token");
    await waitFor(() =>
      expect(
        screen.getByPlaceholderText(
          /login\.username|用户名或邮箱|username or email/i,
        ),
      ).toBeInTheDocument(),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(startOauth).not.toHaveBeenCalled();
  });

  it("does not auto-start with ?sso=0", async () => {
    oauthProviders.current = [oidcProvider];
    renderLogin("?sso=0");
    await waitFor(() =>
      expect(
        screen.getByPlaceholderText(
          /login\.username|用户名或邮箱|username or email/i,
        ),
      ).toBeInTheDocument(),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(startOauth).not.toHaveBeenCalled();
  });

  it("does not auto-start when multiple providers are enabled", async () => {
    oauthProviders.current = [
      oidcProvider,
      { kind: "wecom", enabled: true, display_name: "企业微信" },
    ];
    renderLogin("?sso=auto");
    await waitFor(() =>
      expect(
        screen.getByPlaceholderText(
          /login\.username|用户名或邮箱|username or email/i,
        ),
      ).toBeInTheDocument(),
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(startOauth).not.toHaveBeenCalled();
  });
});
