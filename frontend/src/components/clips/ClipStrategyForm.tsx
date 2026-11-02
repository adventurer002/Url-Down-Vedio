import type { ClipStrategy } from "../../types/clips";
import { clipStrategyValid } from "../../lib/clips";

export default function ClipStrategyForm({
  value,
  onChange,
  onQuote,
  disabled,
  quoting,
  textSource,
}: {
  value: ClipStrategy;
  onChange: (next: ClipStrategy) => void;
  onQuote: () => void;
  disabled: boolean;
  quoting: boolean;
  textSource: boolean;
}) {
  return (
    <form
      className="clip-strategy"
      onSubmit={(event) => {
        event.preventDefault();
        if (clipStrategyValid(value) && !disabled) onQuote();
      }}
    >
      <p className="clip-intro">
        从内容中找到能独立表达的精彩观点。开始前会显示本次额度。
      </p>
      <fieldset disabled={disabled}>
        <label className="clip-field">
          希望找到几段
          <select
            value={value.count}
            onChange={(event) =>
              onChange({ ...value, count: Number(event.target.value) })
            }
          >
            {[1, 2, 3, 4, 5].map((count) => (
              <option key={count} value={count}>
                {count} 段
              </option>
            ))}
          </select>
          <small>实际数量以内容完整性为准，可能少于设置值。</small>
        </label>
        <label className="clip-field">
          片段时长策略
          <select
            value={value.duration_mode || "manual"}
            onChange={(event) =>
              onChange(
                event.target.value === "auto"
                  ? {
                      ...value,
                      duration_mode: "auto",
                      min_seconds: null,
                      max_seconds: null,
                    }
                  : {
                      ...value,
                      duration_mode: "manual",
                      min_seconds: 30,
                      max_seconds: 180,
                    },
              )
            }
          >
            <option value="auto">AI 自动判断（推荐）</option>
            <option value="manual">自定义时长范围</option>
          </select>
          <small>
            AI
            按话题、论点和故事的完整性确定长短，不为凑秒数截断原句。实际数量可以少于设置值。
          </small>
        </label>
        {value.duration_mode !== "auto" && (
          <div className="clip-field">
            <span id="clip-duration-label">每段目标时长</span>
            <div
              className="clip-duration"
              role="group"
              aria-labelledby="clip-duration-label"
            >
              <input
                aria-label="最小时长（秒）"
                type="number"
                min={1}
                max={900}
                step={1}
                required
                value={
                  value.min_seconds == null || Number.isNaN(value.min_seconds)
                    ? ""
                    : value.min_seconds
                }
                onChange={(event) =>
                  onChange({
                    ...value,
                    min_seconds: event.target.valueAsNumber,
                  })
                }
              />
              <span>至</span>
              <input
                aria-label="最大时长（秒）"
                type="number"
                min={1}
                max={900}
                step={1}
                required
                value={
                  value.max_seconds == null || Number.isNaN(value.max_seconds)
                    ? ""
                    : value.max_seconds
                }
                onChange={(event) =>
                  onChange({
                    ...value,
                    max_seconds: event.target.valueAsNumber,
                  })
                }
              />
              <span>秒</span>
            </div>
            <small>
              仅在有指定长度需求时设置（1–900
              秒）。最小时长不能大于最大时长；完整原句不满足范围时不会被强行截断。
            </small>
          </div>
        )}
        <label className="clip-field">
          重点关注什么（可选）
          <textarea
            maxLength={200}
            value={value.focus}
            onChange={(event) =>
              onChange({ ...value, focus: event.target.value })
            }
            placeholder="例如：带具体方法，适合不了解前文的人阅读"
            rows={3}
          />
          <small>{value.focus.length}/200 字</small>
        </label>
      </fieldset>
      <div className="clip-form-actions">
        <p>
          {textSource
            ? "使用已有文稿，无需重新转写。"
            : "后台会先获取字幕或识别语音，再寻找精彩观点。"}
        </p>
        <button
          type="submit"
          className="button primary"
          disabled={disabled || !clipStrategyValid(value)}
        >
          {quoting ? "正在获取额度…" : "查看分析额度"}
        </button>
      </div>
    </form>
  );
}
