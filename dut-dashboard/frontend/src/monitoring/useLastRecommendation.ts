import { useEffect, useState } from "react";

import { getLastChannelRecommendation, LastChannelRecommendationResult } from "../api/rest";
import { useSurvey } from "./siteSurveyStore";

const EMPTY: LastChannelRecommendationResult = { recommendations: [], captured_at: null, cached: false };

/**
 * The selected DUT's last cached channel recommendation, for the Overview
 * mini-card. Reads the backend `/last` cache (no scan) so it works right after a
 * reload, and re-fetches whenever the shared siteSurveyStore produces a fresh
 * result for this DUT (the connect-time prescan or a Re-scan) so the card tracks
 * the Site Survey page live.
 */
export function useLastRecommendation(dutId: string): LastChannelRecommendationResult {
  const [data, setData] = useState<LastChannelRecommendationResult>(EMPTY);
  // A completed scan replaces this entry's `result` ref → triggers a re-fetch.
  const surveyResult = useSurvey(dutId).result;

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await getLastChannelRecommendation(dutId);
        if (!cancelled) {
          setData(res);
        }
      } catch {
        if (!cancelled) {
          setData(EMPTY); // backend unreachable — show the empty state, no error noise
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [dutId, surveyResult]);

  return data;
}

/**
 * Last cached recommendation for every DUT in the fleet, keyed by id. Polls the
 * read-only `/last` cache (cheap: no scan, no gate) on mount and every 30s so
 * the Fleet grid's per-DUT band badge stays roughly current without ever
 * triggering a survey. DUTs never surveyed simply return `cached: false`.
 */
export function useFleetRecommendations(dutIds: string[]): Map<string, LastChannelRecommendationResult> {
  const [map, setMap] = useState<Map<string, LastChannelRecommendationResult>>(new Map());
  // Stable dependency so the effect only resets when the set of ids changes.
  const key = dutIds.join(",");

  useEffect(() => {
    if (dutIds.length === 0) {
      return;
    }
    let cancelled = false;
    const load = async () => {
      const entries = await Promise.all(
        dutIds.map(async (id): Promise<[string, LastChannelRecommendationResult]> => {
          try {
            return [id, await getLastChannelRecommendation(id)];
          } catch {
            return [id, EMPTY];
          }
        }),
      );
      if (!cancelled) {
        setMap(new Map(entries));
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return map;
}
