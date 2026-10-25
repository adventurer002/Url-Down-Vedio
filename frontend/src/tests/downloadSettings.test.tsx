// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import DownloadSettings from "../components/DownloadSettings";

const options = [
  {
    id: "high_mp4",
    label: "1080P 高清",
    container: "mp4",
    height: 1080,
    estimated_size_bytes: null,
  },
  {
    id: "low_mp4",
    label: "720P 标清",
    container: "mp4",
    height: 720,
    estimated_size_bytes: 10000000,
  },
  {
    id: "high_webm",
    label: "1080P WebM",
    container: "webm",
    height: 1080,
    estimated_size_bytes: null,
  },
];
afterEach(cleanup);

it("selects real presets without creating a task until download is clicked", () => {
  const create = vi.fn();
  render(
    <DownloadSettings
      options={options}
      downloading={false}
      extracting={false}
      onCreate={create}
    />,
  );
  fireEvent.click(screen.getByRole("radio", { name: /720P/ }));
  expect(create).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "下载视频" }));
  expect(create).toHaveBeenLastCalledWith("download", "720P 标清", "low_mp4");
  fireEvent.change(screen.getByLabelText("格式"), {
    target: { value: "webm" },
  });
  expect(screen.queryByRole("radio", { name: /720P/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "下载视频" }));
  expect(create).toHaveBeenLastCalledWith(
    "download",
    "1080P WebM",
    "high_webm",
  );
  fireEvent.change(screen.getByLabelText("格式"), {
    target: { value: "audio:mp3" },
  });
  expect(screen.queryByRole("radio")).toBeNull();
  fireEvent.change(screen.getByLabelText("格式"), { target: { value: "mp4" } });
  expect(
    (screen.getByRole("radio", { name: /720P/ }) as HTMLInputElement).checked,
  ).toBe(true);
});

it("disables missing video presets while keeping the separate audio workflow", () => {
  const create = vi.fn();
  render(
    <DownloadSettings
      options={[]}
      downloading={false}
      extracting={false}
      onCreate={create}
    />,
  );
  expect(
    (
      screen.getByRole("button", {
        name: "下载视频",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  fireEvent.change(screen.getByLabelText("格式"), {
    target: { value: "audio:mp3" },
  });
  expect(screen.queryByRole("group", { name: "清晰度" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "提取音频" }));
  expect(create).toHaveBeenCalledWith("audio_extraction", "纯音频 MP3");
});
