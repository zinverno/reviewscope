import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { reviews, type Period, type Stance } from "./fixture";

export type Context = {
  id: string;
  other: string;
  query: string;
  stance: Stance | "";
  period: Period;
};
export const fresh = (period: Period = "sep"): Context => ({
  id: period === "sep" ? "01" : "07",
  other: "",
  query: "",
  stance: "",
  period,
});
function destination(hash: string, context: Context) {
  const [page, params] = hash.replace(/^#/, "").split("?");
  const id = new URLSearchParams(params).get("review");
  const review = page === "research" && reviews.find((r) => r.review_id === id);
  return {
    key: crypto.randomUUID(),
    page: page || "home",
    context: review ? { ...fresh(review.period), id: review.review_id } : context,
  };
}
type Visit = ReturnType<typeof destination>;
export function useNavigation() {
  const [visit, setVisit] = useState<Visit>(
    () => history.state?.reviewVisit ?? destination(location.hash, fresh()),
  );
  const latest = useRef(visit),
    positions = useRef(new Map<string, number>()),
    first = useRef(true);
  useLayoutEffect(() => {
    latest.current = visit;
    history.replaceState({ ...history.state, reviewVisit: visit }, "");
  }, [visit]);
  const leave = useCallback(() => {
    positions.current.set(latest.current.key, window.scrollY);
  }, []);
  const navigate = useCallback(
    (hash: string) => {
      if (hash === location.hash) return;
      leave();
      const next = destination(hash, latest.current.context);
      history.pushState({ reviewVisit: next }, "", hash);
      latest.current = next;
      setVisit(next);
    },
    [leave],
  );
  useEffect(() => {
    const initial = history.scrollRestoration;
    history.scrollRestoration = "manual";
    function follow() {
      const saved = history.state?.reviewVisit as Visit | undefined;
      if (saved?.key === latest.current.key) return;
      leave();
      const next = saved ?? destination(location.hash, latest.current.context);
      latest.current = next;
      setVisit(next);
    }
    function link(e: MouseEvent) {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)
        return;
      const anchor = (e.target as Element).closest<HTMLAnchorElement>('a[href^="#"]');
      if (anchor) {
        e.preventDefault();
        navigate(anchor.hash);
      }
    }
    document.addEventListener("click", link);
    window.addEventListener("popstate", follow);
    window.addEventListener("hashchange", follow);
    return () => {
      document.removeEventListener("click", link);
      window.removeEventListener("popstate", follow);
      window.removeEventListener("hashchange", follow);
      history.scrollRestoration = initial;
    };
  }, [leave, navigate]);
  const { key, page } = visit;
  useLayoutEffect(() => {
    const title: Record<string, string> = {
      research: "Исследование",
      changes: "Изменения",
      import: "Импорт",
      report: "Отчёт",
    };
    document.title = `${title[page] ?? "Исследование отзывов"} · ReviewScope`;
    const main = document.getElementById(title[page] ? "main" : "landing");
    const saved = positions.current.get(key);
    if (saved !== undefined) window.scrollTo(0, saved);
    else if (page === "features" || page === "how") document.getElementById(page)?.scrollIntoView();
    else window.scrollTo(0, 0);
    if (!first.current) main?.focus({ preventScroll: true });
    first.current = false;
  }, [key, page]);
  function update(patch: Partial<Context>) {
    setVisit((v) => ({ ...v, context: { ...v.context, ...patch } }));
  }
  return { visit, navigate, update };
}
