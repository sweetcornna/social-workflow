"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { BrandMark, IconArrowRight, IconKey } from "@/components/icons";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FieldLabel, Input } from "@/components/ui/field";
import { Panel } from "@/components/ui/panel";
import { apiFetch, ApiFailure, describeError, getToken, setToken } from "@/lib/api";
import type { AuthProbe } from "@/lib/types";

/**
 * 登录页。
 *
 * 探针是 `POST /api/v1/auth/login`——它是 token 模式下唯一不鉴权的端点。
 * `auth_required: false` 表示这个实例根本没开认证，直接放行回工作台
 * （别让人对着一个没用的输入框发呆）。
 */
export default function LoginPage() {
  const router = useRouter();
  const [token, setTokenValue] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [probe, setProbe] = React.useState<AuthProbe | null>(null);
  const [error, setError] = React.useState("");
  const [shakeKey, setShakeKey] = React.useState(0);

  // 首屏用已存的 token 探一次：没开认证就直接走，存的 token 还有效也直接走
  React.useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await apiFetch<AuthProbe>("/auth/login", {
          method: "POST",
          body: { token: getToken() },
          silent401: true,
        });
        if (cancelled) return;
        setProbe(res);
        if (!res.auth_required || res.ok) router.replace("/");
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiFailure && e.status === 401) {
          setProbe({ ok: false, auth_required: true, message: "需要 token" });
        } else {
          setError(describeError(e));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await apiFetch<AuthProbe>("/auth/login", {
        method: "POST",
        body: { token },
        silent401: true,
      });
      setToken(token);
      router.replace("/");
    } catch (err) {
      setToken("");
      setError(describeError(err));
      setShakeKey((k) => k + 1);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="absolute right-4 top-4">
        <ThemeToggle />
      </div>

      <Panel variant="pop" className="w-full max-w-[420px] px-7 py-8">
        <div className="flex items-center gap-3">
          <BrandMark size={34} />
          <div>
            {/* 登录页 wordmark：与侧栏并列的两处 Caprasimo 之一 */}
            <div className="font-display text-[17px] leading-tight text-fg">social_workflow</div>
            <div className="sw-num text-[10.5px] text-fg-4">运营工作台</div>
          </div>
        </div>

        {/* 中文大标题拿不到 Caprasimo 的字形，按 porting-notes 的处方落系统栈 +
            600 补厚度；陶土只染"正身"两个字，用 700 深阶保住正文级对比度 */}
        <h1 className="mt-6 text-[30px] font-semibold leading-[1.18] text-fg">
          先验明 <span className="text-primary-deep">正身</span>。
        </h1>
        <p className="mt-2 text-[12.5px] leading-relaxed text-fg-3">
          这台实例开了 <code className="sw-num text-fg-2">SW_UI_TOKEN</code>。
          把那串随机码贴进来——它只存在你这台浏览器的 localStorage 里，
          请求时放在 Authorization 头，不会出现在 URL 上。
        </p>

        <form onSubmit={submit} className="mt-6" key={shakeKey}>
          <FieldLabel htmlFor="ui-token" className="flex items-center gap-1.5">
            <IconKey size={12} />
            SW_UI_TOKEN
          </FieldLabel>
          <Input
            id="ui-token"
            type="password"
            autoFocus
            autoComplete="off"
            value={token}
            onChange={(e) => setTokenValue(e.target.value)}
            placeholder="粘贴 token"
            data-testid="token-input"
            // 出错时输入井自己转成陶红 tint（去描边之后没有边框可染）
            className={error ? "bg-err-soft" : undefined}
            style={error ? { animation: "login-shake 320ms ease-in-out" } : undefined}
          />
          {error ? (
            <p className="mt-2 text-[11.5px] text-err" role="alert" data-testid="token-error">
              {error}
            </p>
          ) : null}

          <Button
            type="submit"
            variant="primary"
            className="mt-4 w-full"
            loading={busy}
            disabled={!token}
            data-testid="token-submit"
          >
            进入工作台
            <IconArrowRight size={13} />
          </Button>
        </form>

        <div className="mt-6 border-t border-line pt-4">
          {probe && !probe.auth_required ? (
            <Badge tone="ok">本实例未开启 token 认证，正在放行</Badge>
          ) : (
            <p className="text-[11px] leading-relaxed text-fg-4">
              忘了 token？它在 core 那台机器的环境变量里（`.env` 的{" "}
              <code className="sw-num">SW_UI_TOKEN</code>）。
              没设这个变量时工作台默认不鉴权，这一页也就不会出现。
            </p>
          )}
        </div>
      </Panel>
    </main>
  );
}
