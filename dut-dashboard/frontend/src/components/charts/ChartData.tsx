type Props = {
  /** DOM id so a future Chart.js migration can read the series with zero backend change. */
  id: string;
  data: unknown;
};

/**
 * Emits the chart's source data as <script type="application/json"> alongside
 * the rendered SVG. Per the design system, this makes a later swap to Chart.js
 * possible without changing how the data is produced.
 */
export default function ChartData({ id, data }: Props) {
  return (
    <script
      id={id}
      type="application/json"
      // JSON is inert inside a application/json script tag; this is not HTML execution.
      dangerouslySetInnerHTML={{ __html: JSON.stringify(data) }}
    />
  );
}
