import { ChannelRecommendation } from "../api/rest";

/**
 * One per-band channel-recommendation pill: green ✓ when the current channel is
 * already optimal, amber ⚠ with `current → recommended` when a clearer channel
 * exists. Shared by the Site Survey card and the read-only Overview / Fleet
 * summaries so the three surfaces speak the same visual language.
 */
export function RecommendationPill({ rec }: { rec: ChannelRecommendation }) {
  const optimal = rec.recommended_channel === rec.current_channel;
  return (
    <span className={`pill ${optimal ? "ok" : "warn"}`} title={rec.reasoning}>
      {optimal ? "✓" : "⚠"} {rec.band}: ch {rec.current_channel}
      {optimal ? "" : ` → ${rec.recommended_channel}`}
    </span>
  );
}
