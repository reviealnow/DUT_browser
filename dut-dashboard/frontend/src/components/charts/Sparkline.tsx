type Props = {
  /** Series values (e.g. CPU busy %). */
  values: number[];
  /** Y-axis max; values are clamped to [0, max]. */
  max?: number;
  /** Pixel height of the chart area. */
  height?: number;
  ariaLabel?: string;
};

const VIEW_W = 100;

/**
 * Zero-dependency inline-SVG area sparkline (offline-first; no Chart.js/CDN).
 * Stroke uses currentColor so the accent comes from CSS tokens.
 */
export default function Sparkline({ values, max = 100, height = 120, ariaLabel }: Props) {
  const n = values.length;
  const safeMax = max > 0 ? max : 1;
  const toX = (i: number) => (n <= 1 ? VIEW_W / 2 : (i / (n - 1)) * VIEW_W);
  const toY = (v: number) => {
    const clamped = Math.min(safeMax, Math.max(0, v));
    return height - (clamped / safeMax) * height;
  };

  const linePoints = values.map((v, i) => `${toX(i).toFixed(2)},${toY(v).toFixed(2)}`).join(" ");
  const areaPoints = `0,${height} ${linePoints} ${VIEW_W},${height}`;
  const last = values[n - 1] ?? 0;

  return (
    <svg
      className="spark"
      viewBox={`0 0 ${VIEW_W} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={ariaLabel}
    >
      {/* horizontal gridlines at 25/50/75% */}
      {[0.25, 0.5, 0.75].map((frac) => (
        <line
          key={frac}
          x1={0}
          x2={VIEW_W}
          y1={height - frac * height}
          y2={height - frac * height}
          className="spark-grid"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {n >= 1 ? (
        <>
          <polygon points={areaPoints} className="spark-area" />
          <polyline points={linePoints} className="spark-line" vectorEffect="non-scaling-stroke" fill="none" />
          <circle cx={toX(n - 1)} cy={toY(last)} r={2.5} className="spark-dot" vectorEffect="non-scaling-stroke" />
        </>
      ) : null}
    </svg>
  );
}
