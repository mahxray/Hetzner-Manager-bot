"""In-memory monitoring charts; invoked on a worker thread, without pyplot global state."""
from .i18n import tr
from datetime import datetime, timezone
import io
import math


def render_chart(metric, metrics):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.dates import DateFormatter

    figure = Figure(figsize=(9, 4.5), dpi=130, layout='constrained')
    FigureCanvasAgg(figure)
    series_count = max(1, len(metrics.get('time_series', {})))
    # Disk bandwidth and IOPS use different units and must not share an axis.
    axes = figure.subplots(series_count if metric == 'disk' else 1, 1, squeeze=False).ravel()
    if metric == 'disk':
        figure.set_size_inches(9, max(4.5, 2.6 * series_count))
    has_data = False
    for series_index, (name, series) in enumerate(metrics.get('time_series', {}).items()):
        axis = axes[series_index] if metric == 'disk' else axes[0]
        points = []
        for timestamp, raw in series.get('values', []):
            try:
                value = float(raw)
                if math.isfinite(value):
                    points.append((datetime.fromtimestamp(float(timestamp), timezone.utc), value))
            except (ValueError, TypeError, OverflowError):
                continue
        if points:
            axis.plot([p[0] for p in points], [p[1] for p in points], label=name, linewidth=1.4)
            has_data = True
        axis.set_xlabel('UTC')
        axis.set_ylabel({'cpu': '%', 'network': 'bits / second', 'disk': name}.get(metric, metric))
        axis.xaxis.set_major_formatter(DateFormatter('%H:%M', tz=timezone.utc))
        axis.grid(alpha=0.2)
        if points:
            axis.legend(fontsize=8)
    if not has_data:
        raise ValueError(tr('داده\u200cای برای رسم نمودار این بازه وجود ندارد.'))
    axes[0].set_title(f'{metric.upper()} · last hour')
    stream = io.BytesIO()
    figure.savefig(stream, format='png')
    return stream.getvalue()
