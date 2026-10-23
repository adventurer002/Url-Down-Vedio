// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkspaceProvider } from "../hooks/useWorkspace";
import { createWorkspace } from "../stores/workspace";
import {
  authApi,
  authStore,
  type LoginSession,
  type LoginResult,
} from "../stores/auth";
import WeChatLoginModal from "../components/auth/WeChatLoginModal";
import { api } from "../api";
import { client, authenticatedUrl, get } from "../api/client";
const session: LoginSession = {
  scene_id: "scene-test",
  mode: "mock",
  qr_url: "data:image/svg+xml,test",
  verify_code: "123456",
  expires_in: 300,
};
const authenticated: LoginResult = {
  status: "authenticated",
  access_token: "token-a",
  user: {
    id: "user-a",
    nickname: "微信用户_0001",
    is_subscribed: true,
    role: "user",
    status: "active",
  },
};
beforeEach(() => {
  vi.useFakeTimers();
  const saved = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => saved.get(key) || null,
    setItem: (key: string, value: string) => saved.set(key, value),
    removeItem: (key: string) => saved.delete(key),
  });
  authStore.clear();
  authStore.close();
  vi.spyOn(authApi, "create").mockResolvedValue(session);
  vi.spyOn(authApi, "poll").mockResolvedValue({ status: "pending" });
  vi.spyOn(api, "history").mockResolvedValue({
    items: [],
    total: 0,
    offset: 0,
    limit: 20,
  });
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value() {
      this.setAttribute("open", "");
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value() {
      this.removeAttribute("open");
    },
  });
});
afterEach(() => {
  cleanup();
  authStore.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function mount() {
  const workspace = createWorkspace();
  const view = render(
    <WorkspaceProvider store={workspace}>
      <WeChatLoginModal />
    </WorkspaceProvider>,
  );
  await act(async () => {});
  return { workspace, ...view };
}
it("displays and copies the code, counts down and calls mock confirmation", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  vi.spyOn(authApi, "confirm").mockResolvedValue({});
  await mount();
  expect(screen.getByLabelText("六位登录口令").textContent).toBe("123456");
  fireEvent.click(screen.getByText("一键复制口令"));
  await act(async () => {});
  expect(writeText).toHaveBeenCalledWith("123456");
  fireEvent.click(screen.getByText("⚡ 本地模拟关注并发送口令"));
  await act(async () => {});
  expect(authApi.confirm).toHaveBeenCalledWith("scene-test");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  expect(screen.getByRole("status").textContent).toBe("299 秒后失效");
});
it("saves credentials, reloads history and closes after successful polling", async () => {
  vi.mocked(authApi.poll).mockResolvedValue(authenticated);
  authStore.open();
  const { workspace } = await mount();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  expect(authStore.getState().token).toBe("token-a");
  expect(authStore.getState().user?.id).toBe("user-a");
  expect(authStore.getState().modalOpen).toBe(false);
  expect(workspace.historyLoaded).toBe(true);
  expect(localStorage.getItem("videohub.auth.token")).toBe("token-a");
});
it("stops polling on expiry and cleans up on unmount", async () => {
  vi.mocked(authApi.poll).mockResolvedValue({ status: "expired" });
  const view = await mount();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  expect(screen.getByText("口令已过期，请刷新后重试")).toBeTruthy();
  const count = vi.mocked(authApi.poll).mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(authApi.poll).toHaveBeenCalledTimes(count);
  const signal = vi.mocked(authApi.create).mock.calls[0]![0]!;
  view.unmount();
  expect(signal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});
it("adds credentials to API and native stream/download URLs", async () => {
  authStore.accept(authenticated);
  const original = client.defaults.adapter;
  client.defaults.adapter = async (config) => {
    expect(config.withCredentials).toBe(true);
    expect(config.headers.Authorization).toBe("Bearer token-a");
    return {
      data: { data: { ok: true } },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    };
  };
  try {
    expect(await get("/auth/me")).toEqual({ ok: true });
  } finally {
    client.defaults.adapter = original;
  }
  for (const path of ["/tasks/test/events", "/files/test/download"])
    expect(new URL(authenticatedUrl(path)).searchParams.get("token")).toBe(
      "token-a",
    );
});
it("switches identity and ignores old pending history responses", async () => {
  const workspace = createWorkspace();
  workspace.switchUser("user-a");
  let resolve!: (value: Awaited<ReturnType<typeof api.history>>) => void;
  vi.mocked(api.history).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const pending = workspace.loadHistory();
  workspace.switchUser("user-b");
  resolve({ items: [], total: 500, offset: 0, limit: 20 });
  await pending;
  expect(workspace.historyTotal).toBe(0);
  expect(workspace.tasks).toEqual([]);
  expect(workspace.historyLoading).toBe(false);
});
it("hides the development mock action for real accounts", async () => {
  vi.mocked(authApi.create).mockResolvedValue({
    ...session,
    mode: "verify_code",
  });
  await mount();
  expect(screen.queryByText("⚡ 本地模拟关注并发送口令")).toBeNull();
});

it("keeps session bootstrap quiet and opens login for a protected action", async () => {
  const original = client.defaults.adapter;
  client.defaults.adapter = async (config) => {
    throw {
      isAxiosError: true,
      config,
      response: {
        status: 401,
        data: { error: { code: "AUTH_REQUIRED", message: "请登录" } },
      },
    };
  };
  try {
    await expect(get("/auth/me")).rejects.toHaveProperty(
      "code",
      "AUTH_REQUIRED",
    );
    expect(authStore.getState().modalOpen).toBe(false);
    await expect(get("/downloads")).rejects.toHaveProperty(
      "code",
      "AUTH_REQUIRED",
    );
    expect(authStore.getState().modalOpen).toBe(true);
  } finally {
    client.defaults.adapter = original;
  }
});

it("ignores a stale authentication failure from the previous account", async () => {
  authStore.accept(authenticated);
  const original = client.defaults.adapter;
  client.defaults.adapter = async (config) => {
    authStore.accept({
      ...authenticated,
      access_token: "token-b",
      user: { ...authenticated.user!, id: "user-b" },
    });
    throw {
      isAxiosError: true,
      config,
      response: {
        status: 401,
        data: { error: { code: "INVALID_TOKEN", message: "旧凭证过期" } },
      },
    };
  };
  try {
    await expect(get("/auth/me")).rejects.toHaveProperty(
      "code",
      "INVALID_TOKEN",
    );
    expect(authStore.getState().token).toBe("token-b");
    expect(authStore.getState().user?.id).toBe("user-b");
  } finally {
    client.defaults.adapter = original;
  }
});

it("opens the login card when the server reports an unsubscribed account", async () => {
  authStore.accept(authenticated);
  const original = client.defaults.adapter;
  client.defaults.adapter = async (config) => {
    throw {
      isAxiosError: true,
      config,
      response: {
        status: 403,
        data: {
          error: { code: "WECHAT_UNSUBSCRIBED", message: "请重新关注公众号" },
        },
      },
    };
  };
  try {
    await expect(get("/tasks")).rejects.toHaveProperty(
      "code",
      "WECHAT_UNSUBSCRIBED",
    );
    expect(authStore.getState().user).toBeNull();
    expect(authStore.getState().modalOpen).toBe(true);
  } finally {
    client.defaults.adapter = original;
    authStore.close();
  }
});
