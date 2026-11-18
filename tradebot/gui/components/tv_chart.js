// TradingView Lightweight Charts (v5, Apache-2.0) wrapper for NiceGUI.
// Data is pushed from Python via `setData(payload)`; see tv_chart.py for the payload format.
import {
  createChart, CandlestickSeries, LineSeries, HistogramSeries, AreaSeries,
  createSeriesMarkers, ColorType, CrosshairMode, LineStyle,
} from "tb-lwc";

const STYLES = { solid: LineStyle.Solid, dotted: LineStyle.Dotted, dashed: LineStyle.Dashed };

export default {
  template: `<div style="position:relative;min-height:200px"></div>`,
  props: { theme: String, payload: Object },
  mounted() {
    this.series = [];
    this.chart = createChart(this.$el, this.chartOptions());
    this.resizeObserver = new ResizeObserver(() => {
      this.chart.applyOptions({ width: this.$el.clientWidth, height: this.$el.clientHeight });
    });
    this.resizeObserver.observe(this.$el);
    if (this.payload) this.setData(this.payload);
  },
  unmounted() {
    this.resizeObserver?.disconnect();
    this.chart?.remove();
  },
  watch: {
    theme() { this.chart.applyOptions(this.chartOptions()); },
  },
  methods: {
    colors() {
      const dark = this.theme !== "light";
      return dark
        ? { bg: "#0f1218", text: "#9aa4b2", grid: "#1c222c", border: "#262d38" }
        : { bg: "#ffffff", text: "#4b5563", grid: "#eef1f5", border: "#dfe3ea" };
    },
    chartOptions() {
      const c = this.colors();
      return {
        autoSize: false,
        width: this.$el.clientWidth,
        height: this.$el.clientHeight,
        layout: {
          background: { type: ColorType.Solid, color: c.bg },
          textColor: c.text,
          fontFamily: "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
          fontSize: 12,
          attributionLogo: true,
          panes: { separatorColor: c.border, separatorHoverColor: c.grid, enableResize: true },
        },
        grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: { borderColor: c.border },
        timeScale: { borderColor: c.border, timeVisible: true, secondsVisible: false },
        localization: { locale: "de-DE" },
      };
    },
    clear() {
      for (const s of this.series) this.chart.removeSeries(s);
      this.series = [];
      this.markers = null;
      // remove now-empty panes (keep main pane)
      const panes = this.chart.panes();
      for (let i = panes.length - 1; i > 0; i--) this.chart.removePane(i);
    },
    add(type, options, pane) {
      const s = this.chart.addSeries(type, options, pane || 0);
      this.series.push(s);
      return s;
    },
    // payload: {candles, volume, overlays, panes, markers, price_lines, line, fit}
    setData(p) {
      if (!this.chart) return;
      const keepRange = p.keep_range ? this.chart.timeScale().getVisibleLogicalRange() : null;
      this.clear();
      let main = null;
      if (p.candles) {
        main = this.add(CandlestickSeries, {
          upColor: "#16c784", downColor: "#ea3943", borderVisible: false,
          wickUpColor: "#16c784", wickDownColor: "#ea3943",
          priceFormat: { type: "price", precision: p.precision ?? 2, minMove: Math.pow(10, -(p.precision ?? 2)) },
        });
        main.setData(p.candles);
      }
      for (const l of p.lines || []) {  // plain line / area series on the main pane (e.g. equity curve)
        const s = this.add(l.area ? AreaSeries : LineSeries, l.area
          ? { lineColor: l.color, topColor: l.color + "55", bottomColor: l.color + "05", lineWidth: 2, title: l.title || "" }
          : { color: l.color, lineWidth: l.width || 2, title: l.title || "", lineStyle: STYLES[l.style] ?? LineStyle.Solid,
              priceLineVisible: false, lastValueVisible: l.last_value ?? true });
        s.setData(l.data);
        if (!main) main = s;
      }
      if (p.volume) {
        const v = this.add(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
        v.priceScale().applyOptions({ scaleMargins: { top: 0.86, bottom: 0 } });
        v.setData(p.volume);
      }
      for (const o of p.overlays || []) {
        const s = this.add(LineSeries, {
          color: o.color, lineWidth: o.width || 1.5, title: "",
          lineStyle: STYLES[o.style] ?? LineStyle.Solid,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        s.setData(o.data);
      }
      (p.panes || []).forEach((pane, idx) => {
        const paneIndex = idx + 1;
        let first = null;
        for (const sd of pane.series) {
          const s = sd.type === "histogram"
            ? this.add(HistogramSeries, { color: sd.color, title: sd.title || "", priceLineVisible: false }, paneIndex)
            : this.add(LineSeries, { color: sd.color, lineWidth: sd.width || 1.5, title: sd.title || "",
                                     priceLineVisible: false, lastValueVisible: true,
                                     lineStyle: STYLES[sd.style] ?? LineStyle.Solid }, paneIndex);
          s.setData(sd.data);
          first = first || s;
        }
        if (first) for (const lvl of pane.levels || []) {
          first.createPriceLine({ price: lvl, color: "#6b7280", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false });
        }
      });
      // size panes: oscillator panes fixed height, main pane takes the rest
      const panes = this.chart.panes();
      const total = this.$el.clientHeight;
      const sub = (p.panes || []).map((x) => x.height || 120);
      const subSum = sub.reduce((a, b) => a + b, 0);
      if (panes.length > 1) {
        const scale = subSum > total * 0.55 ? (total * 0.55) / subSum : 1;
        const apply = () => {
          const ps = this.chart.panes();
          sub.forEach((h, i) => ps[i + 1] && ps[i + 1].setHeight(Math.round(h * scale)));
          ps[0].setHeight(Math.max(120, Math.round(total - subSum * scale)));
        };
        apply();
        requestAnimationFrame(() => requestAnimationFrame(apply));
      }
      if (main && p.markers && p.markers.length) {
        this.markers = createSeriesMarkers(main, p.markers.slice().sort((a, b) => a.time - b.time));
      }
      if (main) for (const pl of p.price_lines || []) {
        main.createPriceLine({ price: pl.price, color: pl.color, lineWidth: 1, lineStyle: LineStyle.Dashed,
                               axisLabelVisible: true, title: pl.title || "" });
      }
      if (keepRange) this.chart.timeScale().setVisibleLogicalRange(keepRange);
      else if (p.fit !== false) this.chart.timeScale().fitContent();
    },
  },
};
