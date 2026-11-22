import { useState } from "react";
import { type Period, periods } from "./fixture";
import { own, places, selection, type PlaceId } from "./network";

const MODES = ["Одна точка", "Моя сеть", "Сравнение"] as const;

export type AtlasMode = (typeof MODES)[number];
export type OwnId = Exclude<PlaceId, "ray">;
const OWN_IDS: OwnId[] = ["arc", "line", "cape"];

export function useAtlasState() {
  const [mode, setMode] = useState<AtlasMode>("Одна точка");
  const [period, setPeriod] = useState<Period>("sep");
  const [point, setPoint] = useState<PlaceId>("arc");
  const [side, setSide] = useState<PlaceId | "all">("arc");

  function changeMode(next: AtlasMode) {
    setMode(next);
    setSide(next === "Моя сеть" ? "all" : point);
  }

  function choosePlace(next: PlaceId) {
    if (next !== "ray") setPoint(next);
    setSide(next);
  }

  const visible = mode === "Сравнение" ? places : own;
  const visibleIds = visible.map((item) => item.id);
  const selected = side === "all" ? "" : side;
  const ids = side === "all" ? OWN_IDS : [side];
  const summary = selection(ids, period);
  const name = places.find((item) => item.id === side)?.name ?? "Вся сеть";
  const breakdown = OWN_IDS.reduce(
    (acc, id) => {
      acc[id] = selection([id], period);
      return acc;
    },
    {} as Record<OwnId, ReturnType<typeof selection>>,
  );
  const comparison = mode === "Сравнение" ? selection([side === "ray" ? point : "ray"], period) : null;
  const ownName = places.find((item) => item.id === point)?.name ?? "Арка";
  const otherName = side === "ray" ? `Своя точка · ${ownName}` : "Другая компания · Луч";
  const sideName = side === "all" ? "Вся сеть" : name;
  const comparisonName = side === "ray" ? "Другая компания · Луч" : `Своя точка · ${name}`;
  const periodLabel = periods[period];

  return {
    mode,
    period,
    periodLabel,
    point,
    side,
    modes: MODES,
    visible,
    visibleIds,
    selected,
    name: sideName,
    summary,
    breakdown,
    comparison,
    comparisonName,
    ownName,
    otherName,
    changeMode,
    changePeriod: setPeriod,
    choosePlace,
    backToNetwork: () => setSide("all"),
  };
}

export type AtlasState = ReturnType<typeof useAtlasState>;
