import { store, CHART_UPDATE_INTERVAL } from './store.js';
import { t } from './i18n.js';
import { getTempUnitSymbol } from './utils.js';

export function updateChart() {
    const now = Date.now();
    if (now - store.lastChartUpdate < CHART_UPDATE_INTERVAL) return;
    
    const chartContainer = document.getElementById('temp-chart');
    if (!chartContainer || chartContainer.offsetParent === null) return;
    
    store.lastChartUpdate = now;
    
    fetch('/api/history?hours=24')
        .then(r => r.json())
        .then(data => {
            if (!data.has_data) return;
            
            const series = [
                {
                    name: t('chart.max_hdd_temp', 'Max HDD Temp'),
                    data: data.timestamps.map((ts, i) => ({
                        x: new Date(ts).getTime(),
                        y: data.temps[i]
                    }))
                },
                {
                    name: t('chart.avg_pwm', 'Avg PWM'),
                    data: data.timestamps.map((ts, i) => ({
                        x: new Date(ts).getTime(),
                        y: data.pwm[i]
                    }))
                }
            ];
            
            if (!store.chart) {
                store.chart = new ApexCharts(chartContainer, {
                    chart: {
                        type: 'line',
                        height: 250,
                        background: 'transparent',
                        foreColor: '#9ca3af',
                        toolbar: { show: false },
                        zoom: { enabled: false },
                        animations: {
                            enabled: true,
                            easing: 'easeinout',
                            speed: 800
                        }
                    },
                    theme: { mode: 'dark' },
                    stroke: {
                        curve: 'smooth',
                        width: [2, 1.5],
                        dashArray: [0, 5]
                    },
                    colors: ['#ff2d55', '#00f0ff'],
                    fill: {
                        type: 'gradient',
                        gradient: {
                            shade: 'dark',
                            type: 'vertical',
                            opacityFrom: 0.3,
                            opacityTo: 0
                        }
                    },
                    markers: {
                        size: 0,
                        hover: { size: 4 }
                    },
                    grid: {
                        borderColor: '#1a1f2e',
                        strokeDashArray: 4
                    },
                    xaxis: {
                        type: 'datetime',
                        labels: {
                            style: { colors: '#6b7280' }
                        }
                    },
                    yaxis: [
                        {
                            title: { text: getTempUnitSymbol(), style: { color: '#ff2d55' } },
                            labels: { style: { colors: '#6b7280' } }
                        },
                        {
                            opposite: true,
                            title: { text: '%', style: { color: '#00f0ff' } },
                            labels: { style: { colors: '#6b7280' } },
                            min: 0,
                            max: 100
                        }
                    ],
                    legend: {
                        position: 'top',
                        labels: { colors: '#9ca3af' }
                    },
                    tooltip: {
                        theme: 'dark',
                        x: { format: 'HH:mm' }
                    }
                });
                
                store.chart.render();
            } else {
                store.chart.updateSeries(series);
            }
        })
        .catch(err => console.error('Chart error:', err));
}

// Update chart every 60 seconds
setInterval(updateChart, 60000);
