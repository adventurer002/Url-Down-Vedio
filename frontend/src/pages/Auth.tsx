import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiRequestError } from "../api/client";
import { ErrorNote } from "../components/VideoCard";
import { useLogin, useRegister } from "../hooks/useAuth";

function Shell({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto max-w-sm pt-14">
      <h1 className="text-center text-2xl font-bold tracking-tight">{title}</h1>
      <div className="card mt-6 space-y-4 p-6">{children}</div>
    </div>
  );
}

export function LoginPage() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const login = useLogin(setCode);

  return (
    <Shell title="登录">
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate(
            { email, password },
            { onSuccess: () => navigate("/") },
          );
        }}
      >
        <div className="space-y-1.5">
          <label className="label" htmlFor="email">邮箱</label>
          <input
            id="email"
            className="field"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </div>
        <div className="space-y-1.5">
          <label className="label" htmlFor="password">密码</label>
          <input
            id="password"
            className="field"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </div>
        {login.error instanceof ApiRequestError && (
          <ErrorNote code={login.error.code} message={login.error.message} />
        )}
        <button className="btn btn-primary w-full" disabled={login.isPending}>
          {login.isPending ? "登录中…" : "登录"}
        </button>
      </form>
      <p className="text-center text-sm text-muted">
        还没有账号？
        <Link to="/register" className="ml-1 underline underline-offset-4 hover:text-ink">
          注册
        </Link>
      </p>
      {code === "rate_limited" && (
        <p className="text-center font-mono text-xs text-muted">请求太频繁，请稍后再试</p>
      )}
    </Shell>
  );
}

export function RegisterPage() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [nickname, setNickname] = useState("");
  const register = useRegister(() => {});

  return (
    <Shell title="注册">
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          register.mutate(
            { email, password, nickname: nickname || undefined },
            { onSuccess: () => navigate("/") },
          );
        }}
      >
        <div className="space-y-1.5">
          <label className="label" htmlFor="email">邮箱</label>
          <input
            id="email"
            className="field"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </div>
        <div className="space-y-1.5">
          <label className="label" htmlFor="nickname">昵称（可选）</label>
          <input
            id="nickname"
            className="field"
            value={nickname}
            onChange={(e) => setNickname(e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <label className="label" htmlFor="password">密码（至少 8 位）</label>
          <input
            id="password"
            className="field"
            type="password"
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </div>
        {register.error instanceof ApiRequestError && (
          <ErrorNote code={register.error.code} message={register.error.message} />
        )}
        <button className="btn btn-primary w-full" disabled={register.isPending}>
          {register.isPending ? "注册中…" : "注册"}
        </button>
      </form>
      <p className="text-center text-sm text-muted">
        已有账号？
        <Link to="/login" className="ml-1 underline underline-offset-4 hover:text-ink">
          登录
        </Link>
      </p>
    </Shell>
  );
}
